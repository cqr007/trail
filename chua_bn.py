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

# 币安依赖
import ccxt

class BinanceTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        # 设置全局网络超时时间
        socket.setdefaulttimeout(15)

        # 1. 策略参数加载
        self.leverage = float(config.get("leverage", 20)) # 币安通常杠杆较高，默认设为20
        self.stop_loss_pct = config["stop_loss_pct"]
        
        # 移动止盈参数
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]

        # 部分平仓比例配置 (支持分批止盈)
        self.hard_stop_close_ratio = config.get("hard_stop_close_ratio", 1.0)
        self.low_trail_close_ratio = config.get("low_trail_close_ratio", 1.0)
        self.first_trail_close_ratio = config.get("first_trail_close_ratio", 1.0) # 建议设为 0.5
        self.second_trail_close_ratio = config.get("second_trail_close_ratio", 1.0)
        
        self.feishu_webhook = feishu_webhook
        self.blacklist = set(config.get("blacklist", []))
        self.monitor_interval = monitor_interval

        # 2. 初始化日志
        self.setup_logger()

        # 3. 看门狗相关变量
        self.last_heartbeat = time.time()
        self.watchdog_started = False

        # 4. 币安连接配置
        try:
            exchange_config = {
                'apiKey': config["apiKey"],
                'secret': config["secret"],
                'timeout': 10000,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future', # 默认合约交易
                    'adjustForTimeDifference': True,
                },
                # ✅ 【关键修改】手动指定测试网 URL，替代 set_sandbox_mode(True)
                # 这样可以绕过 ccxt 的 "not supported" 报错，直接连接合约测试网
                'urls': {
                    'api': {
                        'fapiPublic': 'https://testnet.binancefuture.com/fapi/v1',
                        'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
                        'fapiPrivateV2': 'https://testnet.binancefuture.com/fapi/v2',
                    },
                }
            }
            # 如果配置了代理
            if "proxies" in config:
                exchange_config['proxies'] = config['proxies']

            self.exchange = ccxt.binance(exchange_config)
            
            # ❌ 已删除 self.exchange.set_sandbox_mode(True)，防止触发废弃报错
            self.logger.warning("⚠️⚠️⚠️ 已手动配置为币安合约测试网 (Testnet/Demo) - 请确保 config.json 使用测试网 API Key ⚠️⚠️⚠️")
            
            # 预加载市场信息（用于精度计算）
            self.logger.info("⏳ 正在加载币安市场信息...")
            self.exchange.load_markets()
            self.logger.info("✅ 币安交易连接建立成功 (测试网)")
            
        except Exception as e:
            self.logger.error(f"❌ 币安连接初始化失败: {e}")
            raise e

        # 用于存储每个币种的最高收益率状态
        # Key 格式建议为: "SYMBOL_POSITIONSIDE" (例如 "BTC/USDT_LONG") 以支持双向持仓
        self.trailing_states = {}

    def setup_logger(self):
        self.logger = logging.getLogger("BinanceBot")
        self.logger.setLevel(logging.INFO)
        
        if not os.path.exists("logs"):
            os.makedirs("logs")
            
        handler = TimedRotatingFileHandler("logs/binance_bot.log", when="midnight", interval=1, backupCount=7)
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

    # 辅助函数：处理数量精度
    def amount_to_precision(self, symbol, amount):
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception:
            return str(amount)

    def get_positions_and_prices(self):
        t_start = time.time() 
        try:
            # 获取所有持仓
            # balance = self.exchange.fetch_balance() # 备用方案
            raw_positions = self.exchange.fetch_positions()
            
            api_duration = time.time() - t_start
            if api_duration > 2.0:
                self.logger.warning(f"⚠️ 网络请求耗时过长: {api_duration:.2f}秒")

            active_positions = []
            
            for pos in raw_positions:
                symbol = pos['symbol']
                
                # 兼容不同版本的 ccxt 和 API 返回结构
                # 币安合约通常在 info 里有 positionAmt, positionSide, entryPrice, markPrice
                info = pos['info']
                raw_size = float(info.get('positionAmt', pos.get('contracts', 0)))
                
                if raw_size == 0:
                    continue

                # 关键：判断持仓方向（支持双向持仓）
                # positionSide: 'LONG', 'SHORT', 'BOTH'(单向)
                pos_side_raw = info.get('positionSide', 'BOTH')
                
                # 标准化 side 为 LONG/SHORT 用于逻辑判断
                if pos_side_raw == 'LONG':
                    logic_side = 'LONG'
                elif pos_side_raw == 'SHORT':
                    logic_side = 'SHORT'
                else:
                    # 单向模式下通过数量正负判断
                    logic_side = 'LONG' if raw_size > 0 else 'SHORT'

                entry_price = float(pos.get('entryPrice', info.get('entryPrice', 0)))
                # 优先使用 markPrice 计算盈亏
                current_price = float(pos.get('markPrice', info.get('markPrice', 0)))
                unrealized_pnl = float(pos.get('unrealizedPnl', info.get('unrealizedPnl', 0)))

                if entry_price == 0 or current_price == 0:
                    continue

                # 计算收益率 (使用未结盈亏 / 保证金)
                # 注意：这里我们重新计算一下基于 entryPrice 的涨跌幅作为参考，
                # 或者直接使用交易所返回的 unrealizedPnl。
                # 为了策略统一，这里沿用 Hyperliquid 的逻辑：(Unrealized PnL / Margin) * 100
                # 但币安 Margin 计算较复杂（涉及杠杆），这里简化为：(盈亏 / (名义价值/杠杆))
                
                notional = abs(raw_size) * entry_price
                margin = notional / self.leverage
                
                if margin > 0:
                    profit_pct = (unrealized_pnl / margin) * 100
                else:
                    profit_pct = 0

                active_positions.append({
                    "symbol": symbol,
                    "side": logic_side,           # 逻辑方向: LONG/SHORT
                    "pos_side_api": pos_side_raw, # API原生方向: LONG/SHORT/BOTH (下单用)
                    "size": abs(raw_size),        # 绝对值数量
                    "raw_size": raw_size,         # 原始数量（带正负）
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "profit_pct": profit_pct,
                    "pnl_usdc": unrealized_pnl,
                    "unique_key": f"{symbol}_{pos_side_raw}" # 唯一标识符
                })
                
            return active_positions
            
        except Exception as e:
            self.logger.error(f"❌ 获取数据失败 (保持状态): {e}")
            return None 

    def close_position(self, pos_info, size_to_close, reason="", is_partial=False, current_profit_pct=0.0):
        symbol = pos_info['symbol']
        logic_side = pos_info['side']
        pos_side_api = pos_info['pos_side_api']
        unique_key = pos_info['unique_key']

        try:
            # 1. 精度处理
            amount_str = self.amount_to_precision(symbol, size_to_close)
            # ccxt create_order 需要 float 或 数字字符串，部分交易所对字符串支持更好
            amount_float = float(amount_str)
            
            if amount_float <= 0:
                self.logger.warning(f"⚠️ {symbol} 计算出的平仓数量为 0，跳过")
                return

            action_type = "部分减仓" if is_partial else "全仓止盈/损"
            self.logger.info(f"正在执行 {symbol} {action_type}: 数量 {amount_str}, 方向 {logic_side} ({reason})")
            
            # 2. 确定交易方向 (Side)
            # 平多 -> SELL, 平空 -> BUY
            trade_side = 'sell' if logic_side == 'LONG' else 'buy'
            
            # 3. 构建参数 (ReduceOnly & PositionSide)
            params = {'reduceOnly': True}
            if pos_side_api in ['LONG', 'SHORT']:
                params['positionSide'] = pos_side_api
            
            # 4. 执行下单
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=trade_side,
                amount=amount_float,
                params=params
            )
            
            msg = f"✅ {symbol} {action_type}成功! 数量: {amount_str}, 原因: {reason}"
            self.logger.info(msg)
            self.send_feishu_alert(msg)
            
            # 5. 状态管理
            if is_partial:
                # 部分平仓：重置该持仓的最高收益记录，防止连续触发
                self.trailing_states[unique_key] = current_profit_pct
                self.logger.info(f"🔄 {symbol} 剩余仓位状态重置，以当前收益 ({current_profit_pct:.2f}%) 为基准继续监控")
            else:
                # 全平：删除状态
                if unique_key in self.trailing_states:
                    del self.trailing_states[unique_key]
                
        except Exception as e:
            err_msg = f"❌ 平仓异常 {symbol}: {e}"
            self.logger.error(err_msg)
            self.send_feishu_alert(f"⚠️ {err_msg}")

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
                    self.logger.warning("⚠️ 数据获取失败，暂停判断 (状态已保护)")
                    
                elif not positions:
                    self.trailing_states.clear()
                    if idle_count % 15 == 0:
                        self.logger.info(f"💓 监控运行中... 当前无持仓 (等待新开仓)")
                    idle_count += 1
                
                else:
                    idle_count = 0
                    for pos in positions:
                        symbol = pos['symbol']
                        profit_pct = pos['profit_pct']
                        total_size = pos['size']
                        unique_key = pos['unique_key']
                        
                        if symbol in self.blacklist:
                            continue

                        # 初始化或更新最高收益
                        if unique_key not in self.trailing_states:
                            self.trailing_states[unique_key] = profit_pct
                        else:
                            if profit_pct > self.trailing_states[unique_key]:
                                self.trailing_states[unique_key] = profit_pct
                        
                        highest_profit = self.trailing_states[unique_key]

                        # --- 档位与比例判断逻辑 (与 Hyperliquid 版本一致) ---
                        current_tier = "未达标"
                        trigger_msg = ""
                        ratio = 0.0
                        
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
                        
                        # 硬止损检查
                        if not trigger_msg and profit_pct <= -self.stop_loss_pct:
                            ratio = self.hard_stop_close_ratio
                            trigger_msg = f"触发硬止损 (当前: {profit_pct:.2f}%)"
                            current_tier = "硬止损"
                            
                        # --- 执行平仓逻辑 ---
                        if trigger_msg and ratio > 0:
                            # 1. 计算数量
                            size_to_close = total_size * ratio
                            
                            # 2. 判断是否部分平仓 (预留 5% 容差防止碎股)
                            is_partial = (ratio < 0.99) and ((total_size - size_to_close) > (total_size * 0.05))
                            
                            if not is_partial:
                                size_to_close = total_size # 确保全平时不留尾巴

                            self.close_position(
                                pos, 
                                size_to_close, 
                                reason=trigger_msg, 
                                is_partial=is_partial, 
                                current_profit_pct=profit_pct
                            )
                            continue # 处理完跳过日志打印
                            
                        self.logger.info(f"监控中: {symbol} | 方向: {pos['side']} | 盈亏: {profit_pct:.2f}% | 最高: {highest_profit:.2f}% | 档位: {current_tier}")

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
            
        if 'binance' in all_config:
            print("💡 正在加载 config.json 中的 [binance] 配置块...")
            bot_config = all_config['binance']
            feishu_url = all_config.get('feishu_webhook')
            
            bot = BinanceTradingBot(bot_config, feishu_webhook=feishu_url)
            bot.trail()
        else:
            print("❌ 致命错误: config.json 中找不到 'binance' 配置块")
            
    except FileNotFoundError:
        print("❌ 错误: 找不到 config.json 文件")
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
