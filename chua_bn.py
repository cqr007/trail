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
# 🔥 终局适配器：BinanceTestnetFix
# 1. 拦截多余请求
# 2. 劫持所有域名，强制指向测试网
# 3. 拦截不支持的接口
# ==========================================
class BinanceTestnetFix(ccxt.binance):
    def describe(self):
        config = super().describe()
        # 强制定义测试网基础 URL
        testnet_url = 'https://testnet.binancefuture.com/fapi/v1'
        
        config['urls']['api'] = {
            'public': testnet_url,
            'private': testnet_url,
            
            # 合约接口 V1 / V2 / V3 全部指向测试网对应路径
            'fapiPublic': 'https://testnet.binancefuture.com/fapi/v1',
            'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
            'fapiPrivateV2': 'https://testnet.binancefuture.com/fapi/v2',
            'fapiPrivateV3': 'https://testnet.binancefuture.com/fapi/v3',
            
            # 杂项接口强指到测试网 (防止报错)
            'sapi': testnet_url, 
            'dapiPublic': testnet_url, 
            'dapiPrivate': testnet_url,
            'eapiPublic': testnet_url, 
            'eapiPrivate': testnet_url,
        }
        config['has']['fetchCurrencies'] = False
        config['has']['fetchMarginPairs'] = False
        config['options']['fetchMarginPairs'] = False
        return config

    # --- 🛡️ 防火墙：拦截所有正式网请求，强制重定向到测试网 ---
    def sign(self, path, api='public', method='GET', params={}, headers=None, body=None):
        request = super().sign(path, api, method, params, headers, body)
        # 强制替换域名
        if 'fapi.binance.com' in request['url']:
            request['url'] = request['url'].replace('fapi.binance.com', 'testnet.binancefuture.com')
        if 'api.binance.com' in request['url']:
            request['url'] = request['url'].replace('api.binance.com', 'testnet.binancefuture.com')
        return request

    # --- 拦截 杂项接口 (返回空数据防止报错) ---
    def sapiGetMarginAllPairs(self, params={}): return []
    def sapiGetMarginIsolatedAllPairs(self, params={}): return []
    def sapiGetCapitalConfigGetall(self, params={}): return []
    def dapiPublicGetExchangeInfo(self, params={}):
        return {'symbols': [], 'timezone': 'UTC', 'serverTime': 0, 'rateLimits': [], 'exchangeFilters': []}
    def eapiPublicGetExchangeInfo(self, params={}):
        return {'symbols': [], 'timezone': 'UTC', 'serverTime': 0, 'rateLimits': [], 'exchangeFilters': []}

class BinanceTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        # 设置全局网络超时时间 (30秒)
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

        # --- 网络自检 ---
        if "proxies" in config:
            self.check_proxy_connection(config['proxies'])

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
            if "proxies" in config:
                exchange_config['proxies'] = config['proxies']

            # ✅ 使用终极修正版适配器
            self.exchange = BinanceTestnetFix(exchange_config)
            
            # 手术级修复 (Double Check)
            empty_list = lambda *args, **kwargs: []
            empty_struct = lambda *args, **kwargs: {'symbols': [], 'timezone': 'UTC', 'serverTime': 0, 'rateLimits': [], 'exchangeFilters': []}
            
            self.exchange.sapiGetMarginAllPairs = empty_list
            self.exchange.sapiGetMarginIsolatedAllPairs = empty_list
            self.exchange.sapiGetCapitalConfigGetall = empty_list
            self.exchange.dapiPublicGetExchangeInfo = empty_struct
            self.exchange.eapiPublicGetExchangeInfo = empty_struct

            self.logger.warning("⚠️⚠️⚠️ 已强制运行在：币安合约测试网 (Testnet) ⚠️⚠️⚠️")
            
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

    def check_proxy_connection(self, proxies):
        """检查代理连通性"""
        self.logger.info(f"🔍 正在检查代理配置: {proxies}")
        if '127.0.0.1' in str(proxies) or 'localhost' in str(proxies):
            self.logger.error("❌❌❌ 错误: 在 Docker 中代理地址不能设为 127.0.0.1！")
            self.logger.error("   请在 config.json 中将代理 IP 改为你的 NAS 局域网 IP (例如 192.168.1.5)")
            return

        try:
            # 尝试通过代理访问 Google
            test_url = "https://www.google.com"
            resp = requests.get(test_url, proxies=proxies, timeout=5)
            if resp.status_code == 200:
                self.logger.info("✅ 代理连接测试通过！网络通畅。")
        except Exception as e:
            self.logger.error(f"❌ 代理连接测试失败: {e}")
            self.logger.error("   Docker 容器无法通过代理上网，请检查防火墙或 IP 设置。")

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
            # 这里不打印 log，由 trail 统一处理
            raise e 

    # --- 修复版：平仓逻辑 ---
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
            # ✅ 修复逻辑：双向持仓不能加 reduceOnly，单向持仓必须加
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

    # --- 修复版：带防封熔断机制的监控循环 ---
    def trail(self):
        """核心监控循环"""
        self.logger.info(f"🚀 启动监控 (目标间隔: {self.monitor_interval}s)...")
        
        if not self.watchdog_started:
            t = threading.Thread(target=self._watchdog_loop, daemon=True)
            t.start()
            self.watchdog_started = True

        idle_count = 0
        error_streak = 0 # 连续错误计数器
        
        while True:
            self.last_heartbeat = time.time()
            cycle_start_time = time.time()

            try:
                positions = self.get_positions_and_prices()
                
                # 成功获取数据，重置错误计数
                if positions is not None:
                    error_streak = 0
                
                if positions is None:
                    # 获取失败时 get_positions_and_prices 可能会抛异常，这里作为兜底
                    # 但通常异常会被下面的 except 捕获
                    pass
                    
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

            # === 异常处理与防封机制 ===
            except ccxt.DDoSProtection as e:
                self.logger.error(f"🛑 触发 DDoS 保护 (限频): {e}")
                self.logger.warning("😴 强制休眠 2 分钟等待解封...")
                time.sleep(120)
            except ccxt.RateLimitExceeded as e:
                self.logger.error(f"🛑 触发 API 限流: {e}")
                self.logger.warning("😴 强制休眠 1 分钟...")
                time.sleep(60)
            except Exception as e:
                error_streak += 1
                err_str = str(e)
                self.logger.error(f"❌ 监控错误 (连续第{error_streak}次): {e}")
                
                # 检测 HTTP 418/429/1003 等被封禁错误
                if '418' in err_str or '429' in err_str or '-1003' in err_str:
                    self.logger.critical(f"🛑🛑🛑 检测到 IP 被封禁或限频! 强制休眠 5 分钟...")
                    time.sleep(300)
                    error_streak = 0
                
                # 如果连续报错超过 5 次 (非封禁类)，也休息一下防止变封禁
                if error_streak >= 5:
                    self.logger.warning("🛑 连续报错次数过多，强制休眠 60 秒以防封禁...")
                    time.sleep(60)
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
            
            bot = BinanceTradingBot(bot_config, feishu_webhook=feishu_url)
            bot.trail()
        else:
            print("❌ 致命错误: config.json 中找不到 'binance' 配置块")
            
    except FileNotFoundError:
        print("❌ 错误: 找不到 config.json 文件")
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
