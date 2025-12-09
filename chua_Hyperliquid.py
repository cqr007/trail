# -*- coding: utf-8 -*-
import time
import logging
import requests
import json
import math
from logging.handlers import TimedRotatingFileHandler

# Hyperliquid 依赖
from eth_account.signers.local import LocalAccount
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

class MultiAssetTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        # 策略参数
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

        # 初始化日志
        self.setup_logger()

        # Hyperliquid 连接配置
        self.wallet_address = config["wallet_address"]
        self.private_key = config["private_key"]
        
        try:
            self.account = LocalAccount(key=self.private_key, address=self.wallet_address)
            # 默认连接主网
            self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
            self.exchange = Exchange(self.account, constants.MAINNET_API_URL, account_address=self.wallet_address)
            self.logger.info("Hyperliquid 交易所连接成功")
        except Exception as e:
            self.logger.error(f"Hyperliquid 连接失败: {e}")
            raise e

        # 用于存储每个币种的最高收益率状态 { "BTC": 25.5, ... }
        self.trailing_states = {}

    def setup_logger(self):
        self.logger = logging.getLogger("HyperliquidBot")
        self.logger.setLevel(logging.INFO)
        handler = TimedRotatingFileHandler("trading_bot.log", when="midnight", interval=1, backupCount=7)
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
            requests.post(self.feishu_webhook, json=payload)
        except Exception as e:
            self.logger.error(f"飞书报警发送失败: {e}")

    def get_positions_and_prices(self):
        """获取当前持仓和所有币种的最新价格"""
        try:
            # 获取用户状态（包含持仓）
            user_state = self.info.user_state(self.wallet_address)
            positions_raw = user_state.get('assetPositions', [])
            
            # 获取全市场中间价（比轮询效率高）
            all_mids = self.info.all_mids()
            
            active_positions = []
            
            for item in positions_raw:
                pos = item['position']
                coin = pos['coin']
                size = float(pos['szi'])
                
                if size == 0:
                    continue
                    
                entry_price = float(pos['entryPx'])
                # Hyperliquid API返回的 unrealizedPnl 是 USDC 金额，不是百分比
                unrealized_pnl_val = float(pos['unrealizedPnl'])
                
                # 获取当前价格
                current_price = float(all_mids.get(coin, 0))
                if current_price == 0:
                    continue

                # 计算方向
                side = "LONG" if size > 0 else "SHORT"
                
                # 手动计算盈亏百分比 (ROI %) = (未结盈亏 / 保证金) * 100
                # 估算保证金 = (数量 * 入场价) / 杠杆
                margin = (abs(size) * entry_price) / self.leverage
                if margin > 0:
                    profit_pct = (unrealized_pnl_val / margin) * 100
                else:
                    profit_pct = 0

                active_positions.append({
                    "symbol": coin,
                    "side": side,
                    "size": abs(size), # 统一使用绝对值
                    "raw_size": size,  # 原始带符号数量
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
            
            # Hyperliquid 平仓其实就是反向开单
            # 如果当前是 LONG (买入), 平仓就是 SELL (卖出)
            is_buy = True if side == "SHORT" else False
            
            # 发送市价单 (reduce_only=True 确保只减仓)
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
                
                # 平仓后清除该币种的最高收益记录
                if symbol in self.trailing_states:
                    del self.trailing_states[symbol]
            else:
                self.logger.error(f"❌ {symbol} 平仓失败: {result}")
                
        except Exception as e:
            self.logger.error(f"平仓异常 {symbol}: {e}")
            self.send_feishu_alert(f"⚠️ 平仓异常 {symbol}: {e}")

    def trail(self):
        """核心监控循环"""
        self.logger.info(f"启动监控 (间隔: {self.monitor_interval}s)...")
        
        while True:
            try:
                positions = self.get_positions_and_prices()
                
                if not positions:
                    # 如果没有持仓，清空所有状态，防止因为重启导致的旧状态残留
                    self.trailing_states.clear()
                
                for pos in positions:
                    symbol = pos['symbol']
                    profit_pct = pos['profit_pct']
                    side = pos['side']
                    size = pos['size']
                    
                    # 过滤黑名单
                    if symbol in self.blacklist:
                        continue

                    # 更新最高收益率逻辑
                    # 如果内存中没有记录，或者当前收益创新高，则更新
                    if symbol not in self.trailing_states:
                        self.trailing_states[symbol] = profit_pct
                    else:
                        if profit_pct > self.trailing_states[symbol]:
                            self.trailing_states[symbol] = profit_pct
                    
                    highest_profit = self.trailing_states[symbol]

                    # 判定当前所处的止盈档位
                    current_tier = "未达标"
                    if highest_profit >= self.second_trail_profit_threshold:
                        current_tier = "第二档移动止盈"
                    elif highest_profit >= self.first_trail_profit_threshold:
                        current_tier = "第一档移动止盈"
                    elif highest_profit >= self.low_trail_profit_threshold:
                        current_tier = "低收益回撤保护"

                    # --- 止盈/止损 逻辑判断 ---

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

                    # 3. 第二档移动止盈 (更紧的止盈)
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
                        
                    # 打印状态 (可选，避免日志过多可以调高阈值或注释)
                    if profit_pct > 5 or profit_pct < -5:
                        self.logger.info(f"监控中: {symbol} | 方向: {side} | 盈亏: {profit_pct:.2f}% | 最高: {highest_profit:.2f}% | 档位: {current_tier}")

            except Exception as e:
                self.logger.error(f"监控循环发生错误: {e}")
            
            time.sleep(self.monitor_interval)

if __name__ == '__main__':
    try:
        with open('config.json', 'r') as f:
            config_data = json.load(f)
            
        bot = MultiAssetTradingBot(config_data)
        bot.trail()
    except FileNotFoundError:
        print("错误: 找不到 config.json 文件，请先创建配置文件。")
    except Exception as e:
        print(f"程序启动失败: {e}")