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

# ==========================================
# 🔥 核武器级修复：自定义适配器类
# 直接继承 ccxt.binance 并重写那些捣乱的方法
# ==========================================
class BinanceTestnetFix(ccxt.binance):
    def describe(self):
        config = super().describe()
        # 1. 强制锁死 URL 到合约测试网
        config['urls']['api'] = {
            'public': 'https://testnet.binancefuture.com/fapi/v1',
            'private': 'https://testnet.binancefuture.com/fapi/v1',
            'fapiPublic': 'https://testnet.binancefuture.com/fapi/v1',
            'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
            'fapiPrivateV2': 'https://testnet.binancefuture.com/fapi/v2',
            # 即使这里指错了，下面的重写方法也会拦截调用，双重保险
            'sapi': 'https://testnet.binancefuture.com/fapi/v1', 
        }
        # 2. 强制禁用功能标志
        config['has']['fetchCurrencies'] = False
        config['has']['fetchMarginPairs'] = False
        config['options']['fetchMarginPairs'] = False
        return config

    # 🔥 重写方法 1：拦截全仓杠杆交易对请求
    def sapiGetMarginAllPairs(self, params={}):
        return []

    # 🔥 重写方法 2：拦截逐仓杠杆交易对请求 (你刚才报错的元凶)
    def sapiGetMarginIsolatedAllPairs(self, params={}):
        return []

    # 🔥 重写方法 3：拦截现货资产配置请求
    def sapiGetCapitalConfigGetall(self, params={}):
        return []

class BinanceTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        # 设置全局网络超时时间 (30秒，防止测试网卡顿)
        socket.setdefaulttimeout(30)

        # 1. 策略参数加载
        self.leverage = float(config.get("leverage", 20)) 
        self.stop_loss_pct = config["stop_loss_pct"]
        
        # 移动止盈参数
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]

        # 部分平仓比例配置
        self.hard_stop_close_ratio = config.get("hard_stop_close_ratio", 1.0)
        self.low_trail_close_ratio = config.get("low_trail_close_ratio", 1.0)
        self.first_trail_close_ratio = config.get("first_trail_close_ratio", 1.0)
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
                'timeout': 30000,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,
                }
            }
            # 如果配置了代理
            if "proxies" in config:
                exchange_config['proxies'] = config['proxies']

            # ✅ 使用我们自定义的 "BinanceTestnetFix" 类
            # 这个类已经从底层代码级别 "阉割" 掉了所有现货/杠杆接口
            self.exchange = BinanceTestnetFix(exchange_config)
            
            self.logger.warning("⚠️⚠️⚠️ 已启用终极适配器：币安合约测试网 (Testnet) ⚠️⚠️⚠️")
            
            # 预加载市场信息
            self.logger.info("⏳ 正在加载币安市场信息...")
            self.exchange.load_markets()
            self.logger.info("✅ 币安交易连接建立成功 (测试网)")
            
        except Exception as e:
            self.logger.error(f"❌ 币安连接初始化失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            raise e

        # 用于存储每个币种的最高收益率状态
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
            raw_positions = self.exchange.fetch_positions()
            
            api_duration = time.time() - t_start
            if api_duration > 2.0:
                self.logger.warning(f"⚠️ 网络请求耗时过长: {api_duration:.2f}秒")

            active_positions = []
            
            for pos in raw_positions:
                symbol = pos['symbol']
                info = pos['info']
                # 兼容不同版本的返回结构
                raw_size = float(info.get('positionAmt', pos.get('contracts', 0)))
                
                if raw_size == 0:
                    continue

                # 判断持仓方向
                pos_side_raw = info.get('positionSide', 'BOTH')
                if pos_side_raw == 'LONG':
                    logic_side = 'LONG'
                elif pos_side_raw == 'SHORT':
                    logic_side = 'SHORT'
                else:
                    logic_side = 'LONG' if raw_size > 0 else 'SHORT'

                entry_price = float(pos.get('entryPrice', info.get('entryPrice', 0)))
                # 优先使用 markPrice
                current_price = float(pos.get('markPrice', info.get('markPrice', 0)))
                unrealized_pnl = float(pos.get('unrealizedPnl', info.get('unrealizedPnl', 0)))

                if entry_price == 0 or current_price == 0:
                    continue

                # 计算收益率
                notional = abs(raw_size) * entry_price
                margin = notional / self.leverage
                
                if margin > 0:
                    profit_pct = (unrealized_pnl / margin) * 100
                else:
                    profit_pct = 0

                active_positions.append({
                    "symbol": symbol,
                    "side": logic_side,
                    "pos_side_api": pos_side_raw, 
                    "size": abs(raw_size),
                    "raw_size": raw_size,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "profit_pct": profit_pct,
                    "pnl_usdc": unrealized_pnl,
                    "unique_key": f"{symbol}_{pos_side_raw}"
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
            amount_str = self.amount_to_precision(symbol, size_to_close)
            amount_float = float(amount_str)
            
            if amount_float <= 0:
                self.logger.warning(f"⚠️ {symbol} 计算出的平仓数量为 0，跳过")
                return

            action_type = "部分减仓" if is_partial else "全仓止盈/损"
            self.logger.info(f"正在执行 {symbol} {action_type}: 数量 {amount_str}, 方向 {logic_side} ({reason})")
            
            trade_side = 'sell' if logic_side == 'LONG' else 'buy'
            
            params = {'reduceOnly': True}
            if pos_side_api in ['LONG', 'SHORT']:
                params['positionSide'] = pos_side_api
            
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
            
            if is_partial:
                self.trailing_states[unique_key] = current_profit_pct
                self.logger.info(f"🔄 {symbol} 剩余仓位状态重置，以当前收益 ({current_profit_pct:.2f}%) 为基准继续监控")
            else:
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

                        # --- 档位与比例判断逻辑 ---
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
                            
                            # 2. 判断是否部分平仓
                            is_partial = (ratio < 0.99) and ((total_size - size_to_close) > (total_size * 0.05))
                            
                            if not is_partial:
                                size_to_close = total_size

                            self.close_position(
                                pos, 
                                size_to_close, 
                                reason=trigger_msg, 
                                is_partial=is_partial, 
                                current_profit_pct=profit_pct
                            )
                            continue 
                            
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
