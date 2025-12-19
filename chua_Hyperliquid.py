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
        
        # --- [新增] 部分平仓比例配置 (默认为 1.0 即 100% 全平) ---
        # 例如设置为 0.5 代表平掉当前持仓的 50%
        self.hard_stop_close_ratio = config.get("hard_stop_close_ratio", 1.0) # 硬止损通常全平
        self.low_trail_close_ratio = config.get("low_trail_close_ratio", 1.0) # 低收益保护通常全平
        self.first_trail_close_ratio = config.get("first_trail_close_ratio", 1.0) # 第一档推荐设置 0.5
        self.second_trail_close_ratio = config.get("second_trail_close_ratio", 1.0) # 第二档通常全平
        
        self.feishu_webhook = feishu_webhook
        self.blacklist = set(config.get("blacklist", []))
        self.monitor_interval = monitor_interval

        # 2. 初始化日志
        self.setup_logger()

        # 3. 看门狗相关变量
        self.last_heartbeat = time.time()
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

    def _watchdog_loop(self):
        self.logger.info("🐕 看门狗线程已启动 (超时阈值: 60秒)")
        while True:
            time.sleep(5)
            gap = time.time() - self.last_heartbeat
            if gap > 60:
                self.logger.error(f"💀 检测到主程序卡死 (已阻塞 {gap:.1f} 秒)，正在强制重启...")
                os._exit(1)

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

    # --- [修改] 增加 partial 参数和状态重置逻辑 ---
    def close_position(self, symbol, size_to_close, side, reason="", is_partial=False, current_profit_pct=0.0):
        try:
            # 精度处理：保留5位小数防止 API 报错，具体精度视币种而定，这里取通用值
            size_to_close = round(size_to_close, 5)
            if size_to_close <= 0:
                self.logger.warning(f"⚠️ {symbol} 计算出的平仓数量为 0，跳过")
                return

            action_type = "部分减仓" if is_partial else "全仓止盈/损"
            self.logger.info(f"正在执行 {symbol} {action_type}: 数量 {size_to_close}, 方向 {side} ({reason})")
            
            is_buy = True if side == "SHORT" else False
            
            result = self.exchange.market_open(
                name=symbol,
                is_buy=is_buy,
                sz=size_to_close,
                slippage=0.02
            )
            
            if result['status'] == 'ok':
                msg = f"✅ {symbol} {action_type}成功! 数量: {size_to_close}, 原因: {reason}"
                self.logger.info(msg)
                self.send_feishu_alert(msg)
                
                # --- 状态管理核心逻辑 ---
                if is_partial:
                    # 如果是部分平仓，不能删除 key，否则程序会认为这是新开的仓位。
                    # 必须把 highest_profit 重置为当前 profit，
                    # 让剩余仓位从当前价格开始重新计算 Trail，防止下一秒立即再次触发。
                    self.trailing_states[symbol] = current_profit_pct
                    self.logger.info(f"🔄 {symbol} 剩余仓位状态重置，以当前收益 ({current_profit_pct:.2f}%) 为基准继续监控")
                else:
                    # 如果是全平，删除状态
                    if symbol in self.trailing_states:
                        del self.trailing_states[symbol]
            else:
                self.logger.error(f"❌ {symbol} 平仓失败: {result}")
                
        except Exception as e:
            self.logger.error(f"平仓异常 {symbol}: {e}")
            self.send_feishu_alert(f"⚠️ 平仓异常 {symbol}: {e}")

    def trail(self):
        """核心监控循环"""
        self.logger.info(f"🚀 启动监控 (目标间隔: {self.monitor_interval}s)...")
        
        if not self.watchdog_started:
            t = threading.Thread(target=self._watchdog_loop, daemon=True)
            t.start()
            self.watchdog_started = True

        idle_count = 0
        
        while True:
            self.last_heartbeat = time.time()
            cycle_start_time = time.time()

            try:
                positions = self.get_positions_and_prices()
                
                if positions is None:
                    self.logger.warning("⚠️ 数据获取失败，暂停判断")
                    
                elif not positions:
                    self.trailing_states.clear()
                    if idle_count % 15 == 0:
                        self.logger.info(f"💓 监控运行中... 当前无持仓")
                    idle_count += 1
                
                else:
                    idle_count = 0
                    for pos in positions:
                        symbol = pos['symbol']
                        profit_pct = pos['profit_pct']
                        side = pos['side']
                        total_size = pos['size'] # 当前总持仓量
                        
                        if symbol in self.blacklist:
                            continue

                        # 初始化或更新最高收益
                        if symbol not in self.trailing_states:
                            self.trailing_states[symbol] = profit_pct
                        else:
                            if profit_pct > self.trailing_states[symbol]:
                                self.trailing_states[symbol] = profit_pct
                        
                        highest_profit = self.trailing_states[symbol]
                        
                        # --- 档位与比例判断 ---
                        current_tier = "未达标"
                        trigger_msg = ""
                        ratio = 0.0
                        
                        # 判断逻辑：从高到低判断
                        if highest_profit >= self.second_trail_profit_threshold:
                            current_tier = "第二档移动止盈"
                            trail_stop_loss = highest_profit * (1 - self.higher_trail_stop_loss_pct)
                            if profit_pct <= trail_stop_loss:
                                ratio = self.second_trail_close_ratio
                                trigger_msg = f"触发第二档移动止盈 (最高: {highest_profit:.2f}%)"

                        elif highest_profit >= self.first_trail_profit_threshold:
                            current_tier = "第一档移动止盈"
                            trail_stop_loss = highest_profit * (1 - self.trail_stop_loss_pct)
                            if profit_pct <= trail_stop_loss:
                                ratio = self.first_trail_close_ratio
                                trigger_msg = f"触发第一档移动止盈 (最高: {highest_profit:.2f}%)"

                        elif highest_profit >= self.low_trail_profit_threshold:
                            current_tier = "低收益回撤保护"
                            trail_stop_loss = highest_profit * (1 - self.low_trail_stop_loss_pct)
                            if profit_pct <= trail_stop_loss:
                                ratio = self.low_trail_close_ratio
                                trigger_msg = f"触发低收益保护 (最高: {highest_profit:.2f}%)"
                        
                        # 硬止损检查 (优先级最高，如果还没触发上面的，就检查这个)
                        if not trigger_msg and profit_pct <= -self.stop_loss_pct:
                            ratio = self.hard_stop_close_ratio
                            trigger_msg = f"触发硬止损 (当前: {profit_pct:.2f}%)"
                            current_tier = "硬止损"

                        # --- 执行平仓逻辑 ---
                        if trigger_msg and ratio > 0:
                            # 1. 计算需要平仓的数量
                            size_to_close = total_size * ratio
                            
                            # 2. 判断是否是部分平仓 (如果比例<1 且 计算出的量小于总持仓)
                            # 注意：如果 ratio 是 0.999... 或者 float 误差，最好用 >= 0.99 来判定为全平
                            is_partial = (ratio < 0.99) and (size_to_close < total_size)
                            
                            # 3. 如果是部分平仓，size_to_close 不能太小，否则 API 报错
                            # 这里简单处理，如果剩下的太少（比如小于5%），干脆全平
                            if is_partial and (total_size - size_to_close) < (total_size * 0.05):
                                is_partial = False
                                size_to_close = total_size
                            
                            self.close_position(
                                symbol, 
                                size_to_close, 
                                side, 
                                reason=trigger_msg, 
                                is_partial=is_partial,
                                current_profit_pct=profit_pct
                            )
                            continue # 处理完该币种后跳过，进入下一个币种或下一轮
                            
                        self.logger.info(f"监控中: {symbol} | 仓位: {total_size} | 盈亏: {profit_pct:.2f}% | 最高: {highest_profit:.2f}% | 档位: {current_tier}")

            except Exception as e:
                self.logger.error(f"监控循环发生错误: {e}")
            
            self.last_heartbeat = time.time()

            elapsed = time.time() - cycle_start_time 
            sleep_time = self.monitor_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time) 

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
            print("❌ 致命错误: config.json 中找不到 'hyperliquid' 配置块")
            
    except FileNotFoundError:
        print("❌ 错误: 找不到 config.json 文件")
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
