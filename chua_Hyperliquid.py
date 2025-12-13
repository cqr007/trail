# -*- coding: utf-8 -*-
import time
import logging
import requests
import json
import math
import os
import socket
import threading
from logging.handlers import TimedRotatingFileHandler

# Hyperliquid 依赖
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

class MultiAssetTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        # 设置全局网络超时时间为 15 秒
        socket.setdefaulttimeout(15)

        # 1. 策略参数加载
        self.leverage = float(config.get("leverage", 10))
        self.stop_loss_pct = config["stop_loss_pct"]
        
        # 移动止盈参数
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]
        
        self.feishu_webhook = feishu_webhook
        self.blacklist = set(config.get("blacklist", []))
        self.monitor_interval = monitor_interval

        # 2. 初始化日志
        self.setup_logger()

        # 3. 看门狗相关变量
        self.last_heartbeat = time.time() # 上次心跳时间
        self.watchdog_started = False

        # 4. Hyperliquid 连接配置
        self.wallet_address = config["wallet_address"] 
        
        raw_key = config["private_key"]
        if raw_key.startswith("0x"):
            raw_key = raw_key[2:]
        self.private_key = raw_key
        
        try:
            self.account = Account.from_key(self.private_key)
            agent_address = self.account.address
            
            self.logger.info("-" * 40)
            self.logger.info(f"🔑 API Agent 地址: {agent_address}")
            self.logger.info(f"🏦 目标主钱包地址: {self.wallet_address}")
            
            if agent_address.lower() == self.wallet_address.lower():
                self.logger.warning("⚠️  警告: 你直接使用了主钱包私钥！建议使用 API Agent 以提高安全性。")
            else:
                self.logger.info("✅ 模式确认: 正在使用 Agent 代理操作主钱包。")
            self.logger.info("-" * 40)
            
            self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
            self.exchange = Exchange(
                self.account, 
                constants.MAINNET_API_URL, 
                account_address=self.wallet_address 
            )
            self.logger.info("✅ Hyperliquid 交易连接建立成功")
            
        except Exception as e:
            self.logger.error(f"❌ Hyperliquid 连接初始化失败: {e}")
            raise e

        # 用于存储每个币种的最高收益率状态
        self.trailing_states = {}

    def setup_logger(self):
        self.logger = logging.getLogger("HyperliquidBot")
        self.logger.setLevel(logging.INFO)
        
        if not os.path.exists("logs"):
            os.makedirs("logs")
            
        handler = TimedRotatingFileHandler("logs/hyperliquid_bot.log", when="midnight", interval=1, backupCount=7)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.addHandler(console_handler)

    # --- 看门狗线程函数 ---
    def _watchdog_loop(self):
        self.logger.info("🐕 看门狗线程已启动 (超时阈值: 60秒)")
        while True:
            time.sleep(5)
            gap = time.time() - self.last_heartbeat
            
            if gap > 60:
                self.logger.error(f"💀 检测到主程序卡死 (已阻塞 {gap:.1f} 秒)，正在强制重启...")
                os._exit(1)
    # ---------------------------

    def send_feishu_alert(self, message):
        if not self.feishu_webhook:
            return
        try:
            payload = {"msg_type": "text", "content": {"text": message}}
            requests.post(self.feishu_webhook, json=payload, timeout=5)
        except Exception as e:
            self.logger.error(f"飞书报警发送失败: {e}")

    def get_positions_and_prices(self):
        t_start = time.time() 
        try:
            user_state = self.info.user_state(self.wallet_address)
            all_mids = self.info.all_mids()
            
            api_duration = time.time() - t_start
            if api_duration > 2.0:
                self.logger.warning(f"⚠️ 网络请求耗时过长: {api_duration:.2f}秒")

            positions_raw = user_state.get('assetPositions', [])
            active_positions = []
            
            for item in positions_raw:
                pos = item['position']
                coin = pos['coin']
                size = float(pos['szi'])
                
                if size == 0:
                    continue
                    
                entry_price = float(pos['entryPx'])
                unrealized_pnl_val = float(pos['unrealizedPnl'])
                
                current_price = float(all_mids.get(coin, 0))
                if current_price == 0:
                    continue

                side = "LONG" if size > 0 else "SHORT"
                
                margin = (abs(size) * entry_price) / self.leverage
                if margin > 0:
                    profit_pct = (unrealized_pnl_val / margin) * 100
                else:
                    profit_pct = 0

                active_positions.append({
                    "symbol": coin,
                    "side": side,
                    "size": abs(size), 
                    "raw_size": size,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "profit_pct": profit_pct,
                    "pnl_usdc": unrealized_pnl_val
                })
                
            return active_positions
            
        except Exception as e:
            self.logger.error(f"❌ 获取数据失败 (保持状态): {e}")
            return None 

    def close_position(self, symbol, size, side, reason=""):
        try:
            self.logger.info(f"正在平仓 {symbol}: 数量 {size}, 方向 {side} ({reason})")
            
            is_buy = True if side == "SHORT" else False
            
            result = self.exchange.market_open(
                name=symbol,
                is_buy=is_buy,
                sz=size,
                slippage=0.02
            )
            
            if result['status'] == 'ok':
                msg = f"✅ {symbol} 平仓成功! 原因: {reason}"
                self.logger.info(msg)
                self.send_feishu_alert(msg)
                
                if symbol in self.trailing_states:
                    del self.trailing_states[symbol]
            else:
                self.logger.error(f"❌ {symbol} 平仓失败: {result}")
                
        except Exception as e:
            self.logger.error(f"平仓异常 {symbol}: {e}")
            self.send_feishu_alert(f"⚠️ 平仓异常 {symbol}: {e}")

    def trail(self):
        """核心监控循环"""
        self.logger.info(f"🚀 启动监控 (目标间隔: {self.monitor_interval}s, 超时限制: 15s)...")
        
        if not self.watchdog_started:
            t = threading.Thread(target=self._watchdog_loop, daemon=True)
            t.start()
            self.watchdog_started = True

        while True:
            self.last_heartbeat = time.time()
            cycle_start_time = time.time()

            try:
                positions = self.get_positions_and_prices()
                
                if positions is None:
                    # 网络错误，日志已在 get_positions_and_prices 中打印
                    self.logger.warning("⚠️ 数据获取失败，暂停判断 (状态已保护)")
                    
                elif not positions:
                    # 无持仓，强制打印
                    self.trailing_states.clear()
                    self.logger.info(f"💓 监控运行中... 当前无持仓 (等待新开仓)")
                
                else:
                    # 有持仓，强制打印每一轮的状态
                    for pos in positions:
                        symbol = pos['symbol']
                        profit_pct = pos['profit_pct']
                        side = pos['side']
                        size = pos['size']
                        
                        if symbol in self.blacklist:
                            continue

                        if symbol not in self.trailing_states:
                            self.trailing_states[symbol] = profit_pct
                        else:
                            if profit_pct > self.trailing_states[symbol]:
                                self.trailing_states[symbol] = profit_pct
                        
                        highest_profit = self.trailing_states[symbol]

                        current_tier = "未达标"
                        if highest_profit >= self.second_trail_profit_threshold:
                            current_tier = "第二档移动止盈"
                        elif highest_profit >= self.first_trail_profit_threshold:
                            current_tier = "第一档移动止盈"
                        elif highest_profit >= self.low_trail_profit_threshold:
                            current_tier = "低收益回撤保护"

                        if current_tier == "低收益回撤保护":
                            trail_stop_loss = highest_profit * (1 - self.low_trail_stop_loss_pct)
                            if profit_pct <= trail_stop_loss:
                                self.close_position(symbol, size, side, 
                                    f"触发低收益保护 (最高: {highest_profit:.2f}%, 当前: {profit_pct:.2f}%)")
                                continue

                        elif current_tier == "第一档移动止盈":
                            trail_stop_loss = highest_profit * (1 - self.trail_stop_loss_pct)
                            if profit_pct <= trail_stop_loss:
                                self.close_position(symbol, size, side, 
                                    f"触发第一档移动止盈 (最高: {highest_profit:.2f}%, 当前: {profit_pct:.2f}%)")
                                continue

                        elif current_tier == "第二档移动止盈":
                            trail_stop_loss = highest_profit * (1 - self.higher_trail_stop_loss_pct)
                            if profit_pct <= trail_stop_loss:
                                self.close_position(symbol, size, side, 
                                    f"触发第二档移动止盈 (最高: {highest_profit:.2f}%, 当前: {profit_pct:.2f}%)")
                                continue

                        if profit_pct <= -self.stop_loss_pct:
                            self.close_position(symbol, size, side, 
                                f"触发硬止损 (当前: {profit_pct:.2f}%)")
                            continue
                            
                        # --- 修改：强制打印日志，移除所有条件限制 ---
                        self.logger.info(f"监控中: {symbol} | 方向: {side} | 盈亏: {profit_pct:.2f}% | 最高: {highest_profit:.2f}% | 档位: {current_tier}")

            except Exception as e:
                self.logger.error(f"监控循环发生错误: {e}")
            
            self.last_heartbeat = time.time()

            elapsed = time.time() - cycle_start_time 
            sleep_time = self.monitor_interval - elapsed
            
            if sleep_time > 0:
                time.sleep(sleep_time) 
            else:
                self.logger.warning(f"⚡ 本轮耗时 ({elapsed:.2f}s) 超过设定间隔，跳过睡眠")

if __name__ == '__main__':
    try:
        import os
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
        print(f"当前工作目录: {os.getcwd()}")

        with open('config.json', 'r') as f:
            all_config = json.load(f)
            
        if 'hyperliquid' in all_config:
            print("💡 正在加载 config.json 中的 [hyperliquid] 配置块...")
            bot_config = all_config['hyperliquid']
            feishu_url = all_config.get('feishu_webhook')
            
            bot = MultiAssetTradingBot(bot_config, feishu_webhook=feishu_url)
            bot.trail()
        else:
            if 'stop_loss_pct' in all_config:
                print("💡 正在加载扁平化配置...")
                bot = MultiAssetTradingBot(all_config)
                bot.trail()
            else:
                print("❌ 致命错误: config.json 中找不到 'hyperliquid' 配置块")
            
    except FileNotFoundError:
        print("❌ 错误: 找不到 config.json 文件")
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
