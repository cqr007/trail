# -*- coding: utf-8 -*-
import time
import logging
import requests
import json
import math
import os
from logging.handlers import TimedRotatingFileHandler

# Hyperliquid 依赖
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

class MultiAssetTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
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

        # 3. Hyperliquid 连接配置
        self.wallet_address = config["wallet_address"] # 这是你的主账户地址（有钱的那个）
        
        # 自动处理私钥前缀
        raw_key = config["private_key"]
        if raw_key.startswith("0x"):
            raw_key = raw_key[2:]
        self.private_key = raw_key
        
        try:
            # --- 关键修复 1: 正确初始化账户 ---
            self.account = Account.from_key(self.private_key)
            agent_address = self.account.address
            
            # --- 关键修复 2: 明确打印身份关系，防止操作错账户 ---
            self.logger.info("-" * 40)
            self.logger.info(f"🔑 API Agent 地址: {agent_address}")
            self.logger.info(f"🏦 目标主钱包地址: {self.wallet_address}")
            
            if agent_address.lower() == self.wallet_address.lower():
                self.logger.warning("⚠️  警告: 你直接使用了主钱包私钥！建议使用 API Agent 以提高安全性。")
            else:
                self.logger.info("✅ 模式确认: 正在使用 Agent 代理操作主钱包。")
            self.logger.info("-" * 40)
            
            # 默认连接主网
            self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
            
            # --- 关键修复 3: 绑定主钱包地址 ---
            # account_address 必须填 self.wallet_address (主钱包)
            # 否则 Agent 会去操作它自己的空账户
            self.exchange = Exchange(
                self.account, 
                constants.MAINNET_API_URL, 
                account_address=self.wallet_address 
            )
            self.logger.info("✅ Hyperliquid 交易连接建立成功")
            
        except Exception as e:
            self.logger.error(f"❌ Hyperliquid 连接初始化失败: {e}")
            raise e

        # 用于存储每个币种的最高收益率状态 { "BTC": 25.5, ... }
        self.trailing_states = {}

    def setup_logger(self):
        self.logger = logging.getLogger("HyperliquidBot")
        self.logger.setLevel(logging.INFO)
        
        # --- 修正：确保 logs 目录存在，并将日志写入该目录 ---
        if not os.path.exists("logs"):
            os.makedirs("logs")
            
        # 修改路径为 "logs/hyperliquid_bot.log"
        handler = TimedRotatingFileHandler("logs/hyperliquid_bot.log", when="midnight", interval=1, backupCount=7)
        # ------------------------------------------------
        
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.addHandler(console_handler)

    def send_feishu_alert(self, message):
        if not self.feishu_webhook:
            return
        try:
            payload = {"msg_type": "text", "content": {"text": message}}
            requests.post(self.feishu_webhook, json=payload, timeout=5)
        except Exception as e:
            self.logger.error(f"飞书报警发送失败: {e}")

    def get_positions_and_prices(self):
        """获取当前持仓和所有币种的最新价格"""
        try:
            # 获取用户状态（包含持仓）
            # 注意：查询的是主钱包地址 self.wallet_address
            user_state = self.info.user_state(self.wallet_address)
            positions_raw = user_state.get('assetPositions', [])
            
            # 获取全市场中间价
            all_mids = self.info.all_mids()
            
            active_positions = []
            
            for item in positions_raw:
                pos = item['position']
                coin = pos['coin']
                size = float(pos['szi'])
                
                if size == 0:
                    continue
                    
                entry_price = float(pos['entryPx'])
                unrealized_pnl_val = float(pos['unrealizedPnl'])
                
                # 获取当前价格
                current_price = float(all_mids.get(coin, 0))
                if current_price == 0:
                    continue

                # 计算方向
                side = "LONG" if size > 0 else "SHORT"
                
                # 手动计算盈亏百分比
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
            self.logger.error(f"获取持仓或价格失败: {e}")
            return []

    def close_position(self, symbol, size, side, reason=""):
        """平仓函数"""
        try:
            self.logger.info(f"正在平仓 {symbol}: 数量 {size}, 方向 {side} ({reason})")
            
            is_buy = True if side == "SHORT" else False
            
            # 发送市价单平仓
            result = self.exchange.market_open(
                name=symbol,
                is_buy=is_buy,
                sz=size,
                slippage=0.02 # 2% 滑点保护
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
        self.logger.info(f"🚀 启动监控 (间隔: {self.monitor_interval}s)...")
        
        # --- 新增: 空闲计数器，用于在无持仓时打印心跳日志 ---
        idle_count = 0
        
        while True:
            try:
                positions = self.get_positions_and_prices()
                
                if not positions:
                    self.trailing_states.clear()
                    
                    # --- 新增: 心跳检测逻辑 ---
                    # 避免日志刷屏，每 15 个周期（约 60 秒）打印一次存活状态
                    if idle_count % 15 == 0:
                        self.logger.info(f"💓 监控运行中... 当前无持仓 (等待新开仓)")
                    idle_count += 1
                else:
                    # --- 新增: 有持仓时重置计数器 ---
                    idle_count = 0
                
                for pos in positions:
                    symbol = pos['symbol']
                    profit_pct = pos['profit_pct']
                    side = pos['side']
                    size = pos['size']
                    
                    if symbol in self.blacklist:
                        continue

                    # 更新最高收益率
                    if symbol not in self.trailing_states:
                        self.trailing_states[symbol] = profit_pct
                    else:
                        if profit_pct > self.trailing_states[symbol]:
                            self.trailing_states[symbol] = profit_pct
                    
                    highest_profit = self.trailing_states[symbol]

                    # 判定档位
                    current_tier = "未达标"
                    if highest_profit >= self.second_trail_profit_threshold:
                        current_tier = "第二档移动止盈"
                    elif highest_profit >= self.first_trail_profit_threshold:
                        current_tier = "第一档移动止盈"
                    elif highest_profit >= self.low_trail_profit_threshold:
                        current_tier = "低收益回撤保护"

                    # 1. 低收益回撤保护
                    if current_tier == "低收益回撤保护":
                        trail_stop_loss = highest_profit * (1 - self.low_trail_stop_loss_pct)
                        if profit_pct <= trail_stop_loss:
                            self.close_position(symbol, size, side, 
                                f"触发低收益保护 (最高: {highest_profit:.2f}%, 当前: {profit_pct:.2f}%)")
                            continue

                    # 2. 第一档移动止盈
                    elif current_tier == "第一档移动止盈":
                        trail_stop_loss = highest_profit * (1 - self.trail_stop_loss_pct)
                        if profit_pct <= trail_stop_loss:
                            self.close_position(symbol, size, side, 
                                f"触发第一档移动止盈 (最高: {highest_profit:.2f}%, 当前: {profit_pct:.2f}%)")
                            continue

                    # 3. 第二档移动止盈
                    elif current_tier == "第二档移动止盈":
                        trail_stop_loss = highest_profit * (1 - self.higher_trail_stop_loss_pct)
                        if profit_pct <= trail_stop_loss:
                            self.close_position(symbol, size, side, 
                                f"触发第二档移动止盈 (最高: {highest_profit:.2f}%, 当前: {profit_pct:.2f}%)")
                            continue

                    # 4. 硬止损
                    if profit_pct <= -self.stop_loss_pct:
                        self.close_position(symbol, size, side, 
                            f"触发硬止损 (当前: {profit_pct:.2f}%)")
                        continue
                        
                    # 打印状态
                    if profit_pct > 1 or profit_pct < -1:
                        self.logger.info(f"监控中: {symbol} | 方向: {side} | 盈亏: {profit_pct:.2f}% | 最高: {highest_profit:.2f}% | 档位: {current_tier}")

            except Exception as e:
                self.logger.error(f"监控循环发生错误: {e}")
            
            time.sleep(self.monitor_interval)

if __name__ == '__main__':
    try:
        # 强制切换工作目录，解决 PM2 找不到文件的问题
        import os
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
        print(f"当前工作目录: {os.getcwd()}")

        with open('config.json', 'r') as f:
            all_config = json.load(f)
            
        # 智能读取配置：优先读取嵌套的 Hyperliquid 配置
        if 'hyperliquid' in all_config:
            print("💡 正在加载 config.json 中的 [hyperliquid] 配置块...")
            bot_config = all_config['hyperliquid']
            feishu_url = all_config.get('feishu_webhook')
            
            bot = MultiAssetTradingBot(bot_config, feishu_webhook=feishu_url)
            bot.trail()
        else:
            # 兼容扁平化配置
            if 'stop_loss_pct' in all_config:
                print("💡 正在加载扁平化配置...")
                bot = MultiAssetTradingBot(all_config)
                bot.trail()
            else:
                print("❌ 致命错误: config.json 中找不到 'hyperliquid' 配置块")
                print(f"当前可用键值: {list(all_config.keys())}")
            
    except FileNotFoundError:
        print("❌ 错误: 找不到 config.json 文件")
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
