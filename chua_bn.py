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
# 🔥 核心修复：BinanceFormalFix (正式网专用)
# ==========================================
class BinanceFormalFix(ccxt.binance):
    def describe(self):
        config = super().describe()
        # 1. 强制禁用所有非合约功能
        config['has']['fetchCurrencies'] = False
        config['has']['fetchMarginPairs'] = False
        config['options']['fetchMarginPairs'] = False
        # 2. 默认设为 future
        config['options']['defaultType'] = 'future'
        return config

    def publicGetExchangeInfo(self, params={}):
        return {
            'timezone': 'UTC',
            'serverTime': int(time.time() * 1000),
            'rateLimits': [],
            'exchangeFilters': [],
            'symbols': [] 
        }

    def sapiGetCapitalConfigGetall(self, params={}): 
        return []

    def sapiGetMarginAllPairs(self, params={}):
        return []

    def sapiGetMarginIsolatedAllPairs(self, params={}):
        return []

    def sign(self, path, api='public', method='GET', params={}, headers=None, body=None):
        request = super().sign(path, api, method, params, headers, body)
        if 'api.binance.com' in request['url']:
            pass
        return request

class BinanceTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        # 设置全局网络超时时间 (60秒，代理可能慢)
        socket.setdefaulttimeout(60)

        # 1. 策略参数加载
        self.leverage = float(config.get("leverage", 20)) 
        self.stop_loss_pct = config["stop_loss_pct"]
        
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]

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

        # --- 网络自检 ---
        self.check_network_connectivity(config.get('proxies'))

        # 4. 币安连接配置 (正式网)
        try:
            exchange_config = {
                'apiKey': config["apiKey"],
                'secret': config["secret"],
                'timeout': 60000, # 增加超时时间到 60s
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,
                },
                'has': {
                    'fetchCurrencies': False, 
                    'fetchMarginPairs': False,
                }
            }
            
            # 🔥🔥🔥 核心修复：强制注入代理 🔥🔥🔥
            # 如果 config.json 没写 proxies，我们从环境变量里抓出来强制塞给 ccxt
            # 这能解决 ccxt 无法自动读取 Docker 环境变量的问题
            if "proxies" in config and config['proxies']:
                exchange_config['proxies'] = config['proxies']
                self.logger.info(f"⚙️ 使用 Config 文件代理配置: {config['proxies']}")
            else:
                # 尝试从环境变量获取
                env_http = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY')
                env_https = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')
                
                if env_http or env_https:
                    # 如果只有 http_proxy，HTTPS 也用它
                    proxy_url = env_http if env_http else env_https
                    forced_proxies = {
                        'http': proxy_url,
                        'https': proxy_url, # 币安是 HTTPS，这一行至关重要
                    }
                    exchange_config['proxies'] = forced_proxies
                    self.logger.info(f"⚙️ 自动注入环境变量代理到 ccxt: {forced_proxies}")
                else:
                    self.logger.warning("⚠️ 警告：Config 和 环境变量 均未发现代理！连接币安可能失败。")

            # ✅ 使用自定义的 "BinanceFormalFix" 类
            self.exchange = BinanceFormalFix(exchange_config)
            
            # 手术级修复
            dummy_list = lambda *args, **kwargs: []
            self.exchange.sapiGetCapitalConfigGetall = dummy_list
            self.exchange.sapiGetMarginAllPairs = dummy_list
            self.exchange.sapiGetMarginIsolatedAllPairs = dummy_list
            self.exchange.publicGetExchangeInfo = lambda *args, **kwargs: {
                'timezone': 'UTC',
                'serverTime': int(time.time() * 1000),
                'rateLimits': [],
                'exchangeFilters': [],
                'symbols': []
            }
            
            self.logger.info("⏳ 正在加载币安合约市场信息 (Futures Only)...")
            self.exchange.load_markets()
            self.logger.info("✅ 币安正式网连接建立成功！")
            
        except Exception as e:
            self.logger.error(f"❌ 币安连接初始化失败: {e}")
            self.logger.error("👉 请检查 API Key 权限、网络代理设置是否正确。")
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

    def check_network_connectivity(self, proxies_from_config):
        """检查网络连通性"""
        proxies_to_use = proxies_from_config
        env_http = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY')
        
        if not proxies_to_use:
            if env_http:
                self.logger.info(f"🔍 检测到 Docker 环境变量代理: {env_http}")
                # 构造临时测试用的 proxies
                proxies_to_use = {"http": env_http, "https": env_http}
            else:
                self.logger.warning("⚠️ 未检测到任何代理配置 (Config/Env 均为空)。")
        else:
            self.logger.info(f"🔍 使用 Config 文件代理: {proxies_to_use}")

        try:
            test_url = "https://www.google.com"
            requests.get(test_url, proxies=proxies_to_use, timeout=5)
            self.logger.info("✅ Google 连通性测试通过")
        except Exception as e:
            self.logger.error(f"❌ Google 连通性测试失败: {e}")
            return

        try:
            self.logger.info("📡 正在尝试直接连接币安合约接口 (fapi.binance.com)...")
            requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", proxies=proxies_to_use, timeout=10)
            self.logger.info("✅ 币安连通性测试通过！(网络没问题)")
        except Exception as e:
            self.logger.error(f"❌ 币安连通性测试失败: {e}")

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

    def amount_to_precision(self, symbol, amount):
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception:
            return str(amount)

    def get_positions_and_prices(self):
        t_start = time.time() 
        try:
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
            raise e 

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
            
            params = {}
            if pos_side_api in ['LONG', 'SHORT']:
                params['positionSide'] = pos_side_api
            else:
                params['reduceOnly'] = True
            
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
        error_streak = 0
        
        while True:
            self.last_heartbeat = time.time()
            cycle_start_time = time.time()

            try:
                positions = self.get_positions_and_prices()
                
                if positions is not None:
                    error_streak = 0
                
                if positions is None:
                    pass
                    
                elif not positions:
                    self.trailing_states.clear()
                    if idle_count % 30 == 0: 
                        self.logger.info(f"💓 监控运行中... 当前无持仓")
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

                        if unique_key not in self.trailing_states:
                            self.trailing_states[unique_key] = profit_pct
                        else:
                            if profit_pct > self.trailing_states[unique_key]:
                                self.trailing_states[unique_key] = profit_pct
                        
                        highest_profit = self.trailing_states[unique_key]

                        # --- 策略逻辑 ---
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
                        
                        # 硬止损
                        if not trigger_msg and profit_pct <= -self.stop_loss_pct:
                            ratio = self.hard_stop_close_ratio
                            trigger_msg = f"触发硬止损 (当前: {profit_pct:.2f}%)"
                            current_tier = "硬止损"
                            
                        if trigger_msg and ratio > 0:
                            size_to_close = total_size * ratio
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

            except ccxt.DDoSProtection as e:
                self.logger.error(f"🛑 触发 DDoS 保护: {e}")
                time.sleep(120)
            except ccxt.RateLimitExceeded as e:
                self.logger.error(f"🛑 触发 API 限流: {e}")
                time.sleep(60)
            except Exception as e:
                error_streak += 1
                self.logger.error(f"❌ 监控错误 (连续第{error_streak}次): {e}")
                
                if error_streak >= 5:
                    self.logger.warning("🛑 连续错误过多，短时休眠 10s ...")
                    time.sleep(10)
                    error_streak = 0
            
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
            
            # ✅ 读取 config.json 最外层的 monitor_interval，默认 4
            interval = all_config.get('monitor_interval', 4)
            print(f"⏱️ 监控轮询间隔已设置为: {interval} 秒")
            
            # ✅ 尝试将 config 中的 proxies (如果存在) 传递给 bot
            if 'proxies' in all_config:
                bot_config['proxies'] = all_config['proxies']
            
            bot = BinanceTradingBot(bot_config, feishu_webhook=feishu_url, monitor_interval=interval)
            bot.trail()
        else:
            print("❌ 致命错误: config.json 中找不到 'binance' 配置块")
            
    except FileNotFoundError:
        print("❌ 错误: 找不到 config.json 文件")
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
