#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安合约 SOLUSDT 实时数据获取、RSI 计算和自动交易
使用 python-binance 获取实时K线数据，计算15分钟级别的RSI指标，并在超卖时自动下单

check_and_trade  check_adx_filter 核心函数
"""

import pandas as pd
import numpy as np
import talib
from datetime import datetime, timedelta
from binance import Client, ThreadedWebsocketManager
import time
import os
import json
import yaml
from dotenv import load_dotenv
from loguru import logger
import sys

#加载.env文件
load_dotenv()

class RSITracker:
    def __init__(self, api_key=None, api_secret=None, config_file='config.yaml'):
        """初始化 RSI 追踪器和交易系统"""
        # 加载配置文件
        self.config = self.load_config(config_file)
        
        # 交易参数配置
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.api_secret = api_secret or os.getenv('BINANCE_API_SECRET')
        self.testnet = self.config['trading']['testnet']
        
        # 初始化币安客户端
        if self.api_key and self.api_secret:
            self.client = Client(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet
            )
            self.trading_enabled = True
            print("✅ 交易功能已启用")
        else:
            self.client = Client()
            self.trading_enabled = False
            print("⚠️ 仅监控模式（未配置API密钥）")
        
        # 从配置文件加载交易配置
        self.symbol = self.config['trading']['symbol']
        self.interval = self.get_interval_constant(self.config['trading']['interval'])
        self.rsi_period = self.config['rsi']['period']
        self.rsi_oversold = self.config['rsi']['oversold']
        self.rsi_overbought = self.config['rsi']['overbought']

        # 从配置文件加载ADX配置
        self.adx_period = self.config['adx']['period']
        self.adx_trend_threshold = self.config['adx']['trend_threshold']
        self.adx_sideways_threshold = self.config['adx']['sideways_threshold']
        self.adx_enable_filter = self.config['adx']['enable_filter']
        self.adx_filter_mode = self.config['adx']['filter_mode']
        self.position_ratio = self.config['position']['ratio']
        self.leverage = self.config['position']['leverage']
        self.stop_loss_pct = self.config['risk_management']['stop_loss_pct']
        self.take_profit_pct = self.config['risk_management']['take_profit_pct']
        
        # 存储K线数据的DataFrame
        self.klines_df = pd.DataFrame()

        # 技术指标存储
        self.current_rsi = None
        self.current_adx = None
        self.adx_period = 14  # ADX计算周期

        # 交易状态追踪
        self.last_order_time = 0
        self.order_cooldown = self.config['trading_limits']['order_cooldown']
        self.current_position = None
        self.position_history = []
        
        # 高级止盈止损配置
        self.trailing_stop_enabled = self.config['trailing_stop']['enabled']
        self.trailing_stop_distance = self.config['trailing_stop']['distance']
        self.max_position_time = self.config['risk_management']['max_position_time']
        self.emergency_stop_loss = self.config['risk_management']['emergency_stop_loss']
        
        # 风险控制配置
        self.max_daily_trades = self.config['trading_limits']['max_daily_trades']
        self.max_daily_loss = self.config['trading_limits']['max_daily_loss']
        self.max_position_ratio = self.config['position']['max_ratio']
        self.daily_stats = {'date': None, 'trades': 0, 'pnl': 0}
        
        # 设置日志
        self.setup_logging()
        
        self.print_config_summary()
        
        # 杠杆风险提示
        if self.leverage > 3:
            print(f"⚠️ 风险提示: 当前杠杆倍数为 {self.leverage}x，属于高风险交易！")
            print("   建议密切关注市场波动和仓位管理")
        
        print("-" * 50)

    def setup_logging(self):
        """设置loguru日志记录"""
        # 创建logs目录
        if not os.path.exists('logs'):
            os.makedirs('logs')

        # 从配置文件获取日志设置
        log_config = self.config['logging']
        log_level = log_config['level'].upper()

        # 移除所有现有处理器
        logger.remove()

        # 设置日志格式
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        )

        # 文件处理器
        log_filename = datetime.now().strftime(log_config['file_format'].replace('%Y%m%d', '%Y%m%d'))
        logger.add(
            log_filename,
            level=log_level,
            format=log_format,
            rotation="00:00",  # 每天午夜轮转
            retention="7 days",  # 保留7天
            encoding='utf-8'
        )

        # 控制台处理器（如果启用）
        if log_config.get('console_output', True):
            logger.add(
                sys.stdout,
                level=log_level,
                format=log_format,
                colorize=True
            )

        # 添加结构化日志处理器（JSON格式）
        logger.add(
            "logs/rsi_trading_structured.log",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
            rotation="00:00",
            retention="30 days",
            serialize=True  # JSON序列化
        )

        # 添加交易专用日志
        logger.add(
            "logs/trade_actions.log",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | TRADE | {message}",
            rotation="00:00",
            retention="90 days",
            filter=lambda record: "TRADE" in record["extra"] or "trade" in record["message"].lower()
        )

        # 添加错误专用日志
        logger.add(
            "logs/errors.log",
            level="ERROR",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}\n{exception}",
            rotation="00:00",
            retention="30 days",
            backtrace=True,
            diagnose=True
        )

        # 绑定类实例到logger
        self.logger = logger.bind(classname="RSITracker")

        # 记录系统启动信息
        self.logger.info("🚀 RSI交易系统启动")
        self.logger.info(f"📊 日志级别: {log_level}")
        self.logger.info(f"📁 日志文件: {log_filename}")
        self.logger.info(f"🔧 交易模式: {'实盘交易' if self.trading_enabled else '监控模式'}")
        self.logger.info(f"📈 交易对: {self.symbol}")
        self.logger.info(f"⚙️ 杠杆倍数: {self.leverage}x")

    def check_daily_limits(self):
        """检查每日交易限制"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 重置每日统计（新的一天）
        if self.daily_stats['date'] != today:
            self.daily_stats = {'date': today, 'trades': 0, 'pnl': 0}
            self.logger.info(f"重置每日统计: {today}", extra={"date": today, "reset_stats": True})
        
        # 检查交易次数限制
        if self.daily_stats['trades'] >= self.max_daily_trades:
            self.logger.warning(f"已达到每日最大交易次数: {self.max_daily_trades}",
                              extra={"trade_limit": self.max_daily_trades, "current_trades": self.daily_stats['trades']})
            return False

        # 检查亏损限制
        if self.daily_stats['pnl'] <= -self.max_daily_loss:
            self.logger.warning(f"已达到每日最大亏损: {self.max_daily_loss} USDT",
                              extra={"max_loss": self.max_daily_loss, "current_loss": self.daily_stats['pnl']})
            return False
        
        return True

    def save_trade_history(self):
        """保存交易历史到文件"""
        try:
            # 保存到JSON文件
            with open(f'logs/trade_history_{datetime.now().strftime("%Y%m")}.json', 'w', encoding='utf-8') as f:
                # 转换时间戳为可读格式
                history_for_save = []
                for trade in self.position_history:
                    trade_copy = trade.copy()
                    trade_copy['entry_time_str'] = datetime.fromtimestamp(trade['entry_time']).strftime('%Y-%m-%d %H:%M:%S')
                    trade_copy['exit_time_str'] = datetime.fromtimestamp(trade['exit_time']).strftime('%Y-%m-%d %H:%M:%S')
                    history_for_save.append(trade_copy)
                
                json.dump(history_for_save, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            self.logger.error(f"保存交易历史失败: {e}", extra={"error": str(e), "error_type": type(e).__name__})

    def get_historical_klines(self, limit=None):
        """获取历史K线数据"""
        if limit is None:
            limit = self.config['data']['historical_limit']
            
        try:
            print("正在获取历史K线数据...")
            klines = self.client.futures_klines(
                symbol=self.symbol,
                interval=self.interval,
                limit=limit
            )
            
            # 转换为DataFrame
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])
            
            # 数据类型转换
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert('Asia/Shanghai')
            numeric_columns = ['open', 'high', 'low', 'close', 'volume']
            for col in numeric_columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            self.klines_df = df
            print(f"获取到 {len(df)} 条历史K线数据")
            
            # 计算初始RSI和ADX
            self.calculate_rsi()
            self.calculate_adx()

            return True
            
        except Exception as e:
            print(f"获取历史数据失败: {e}")
            return False

    def calculate_rsi(self, current_price=None):
        """计算RSI指标（包含实时价格）"""
        if len(self.klines_df) < self.rsi_period:
            print("数据不足，无法计算RSI")
            return None
        
        try:
            # 获取历史收盘价
            close_prices = self.klines_df['close'].values.astype(float).tolist()
            
            # 如果提供了当前价格，将其作为最新的数据点
            if current_price is not None:
                close_prices.append(float(current_price))
            
            # 确保有足够的数据点
            if len(close_prices) < self.rsi_period:
                return None
            
            # 使用最新的价格序列计算RSI
            price_array = np.array(close_prices)
            rsi = talib.RSI(price_array, timeperiod=self.rsi_period)



            # 获取最新的RSI值
            latest_rsi = rsi[-1] if not np.isnan(rsi[-1]) else None

            # 更新实例变量
            self.current_rsi = latest_rsi

            return latest_rsi
            
        except Exception as e:
            print(f"计算RSI失败: {e}")
            return None

    def calculate_adx(self, current_price=None):
        """计算ADX指标（平均趋向指数）"""
        if len(self.klines_df) < self.adx_period:
            print("数据不足，无法计算ADX")
            return None

        try:
            # 使用历史高低收价格计算ADX
            high_prices = self.klines_df['high'].values.astype(float).tolist()
            low_prices = self.klines_df['low'].values.astype(float).tolist()
            close_prices = self.klines_df['close'].values.astype(float).tolist()

            # 如果提供了当前价格，将其作为最新的数据点
            if current_price is not None:
                # 需要同时提供高低收价格，这里使用当前价格作为近似值
                high_prices.append(float(current_price))
                low_prices.append(float(current_price))
                close_prices.append(float(current_price))

            # 确保有足够的数据点
            if len(high_prices) < self.adx_period or len(low_prices) < self.adx_period or len(close_prices) < self.adx_period:
                return None

            # 使用TA-Lib计算ADX
            high_array = np.array(high_prices)
            low_array = np.array(low_prices)
            close_array = np.array(close_prices)

            adx = talib.ADX(high_array, low_array, close_array, timeperiod=self.adx_period)

            # 获取最新的ADX值
            latest_adx = adx[-1] if not np.isnan(adx[-1]) else None

            # 更新实例变量
            self.current_adx = latest_adx

            return latest_adx

        except Exception as e:
            print(f"计算ADX失败: {e}")
            self.current_adx = None
            return None

    def check_adx_filter(self):
        """检查ADX过滤器条件"""
        if not self.adx_enable_filter or self.current_adx is None:
            return True  # 如果未启用过滤器或ADX数据不足，允许交易

        if self.adx_filter_mode == 'trend':
            # 强趋势模式：只有在ADX > 趋势阈值时才允许交易
            allow_trade = self.current_adx > self.adx_trend_threshold
            reason = "强趋势环境" if allow_trade else "非强趋势环境"
        elif self.adx_filter_mode == 'sideways':
            # 震荡模式：只有在ADX < sideways_threshold时才允许交易
            allow_trade = self.current_adx < self.adx_sideways_threshold
            reason = "震荡环境" if allow_trade else "非震荡环境"
        else:
            # 未知模式，允许交易
            allow_trade = True
            reason = "过滤器模式未知"

        if not allow_trade:
            print(f"ADX过滤器阻止交易: {reason} (ADX: {self.current_adx:.2f})")

        return allow_trade

    def get_account_balance(self):
        """获取账户余额"""
        if not self.trading_enabled:
            return None
        
        try:
            account = self.client.futures_account()
            usdt_balance = 0
            
            for balance in account['assets']:
                if balance['asset'] == 'USDT':
                    usdt_balance = float(balance['availableBalance'])
                    break
            
            return usdt_balance
        except Exception as e:
            print(f"获取账户余额失败: {e}")
            return None

    def calculate_order_size(self, price, balance):
        """计算下单数量（考虑杠杆倍数）"""
        if balance is None or balance <= 0:
            return 0
        
        self.logger.info(f"计算下单数量: {balance} USDT, 下单比例: {self.position_ratio*100}%, 杠杆: {self.leverage}x",
                        extra={"balance": balance, "position_ratio": self.position_ratio, "leverage": self.leverage})

        # 计算可用于开仓的保证金，但不能超过最大持仓比例
        max_margin_allowed = balance * self.max_position_ratio
        requested_margin = balance * self.position_ratio
        margin_amount = min(requested_margin, max_margin_allowed)

        if requested_margin > max_margin_allowed:
            self.logger.warning(f"请求保证金 {requested_margin:.2f} 超过最大限制 {max_margin_allowed:.2f}，使用限制值",
                              extra={"requested_margin": requested_margin, "max_allowed": max_margin_allowed})
        
        # 考虑杠杆倍数计算名义价值
        notional_value = margin_amount * self.leverage
        
        # 计算数量（名义价值 / 价格）
        quantity = notional_value / price
        
        # 币安期货 SOL 最小下单量是 0.01
        min_qty = 0.01
        if quantity < min_qty:
            return 0
        
        
        self.logger.info(f"保证金: {margin_amount} USDT, 名义价值: {notional_value:.2f} USDT, 数量: {quantity:.2f} SOL",
                        extra={"margin": margin_amount, "notional_value": notional_value, "quantity": quantity, "price": price})
        
        # 保留2位小数
        return round(quantity, 2)

    def place_buy_order(self, price, quantity):
        """下限价买单"""
        if not self.trading_enabled or quantity <= 0:
            return None
        
        try:
            # 计算止盈止损价格
            stop_loss_price = round(price * (1 - self.stop_loss_pct), 4)
            take_profit_price = round(price * (1 + self.take_profit_pct), 4)
            
            # 计算保证金和名义价值信息
            notional_value = quantity * price
            margin_required = notional_value / self.leverage
            
            print(f"🟢 准备下单:")
            print(f"   数量: {quantity} SOL")
            print(f"   价格: {price}")
            print(f"   杠杆: {self.leverage}x")
            print(f"   名义价值: {notional_value:.2f} USDT")
            print(f"   保证金: {margin_required:.2f} USDT")
            print(f"   止损: {stop_loss_price}")
            print(f"   止盈: {take_profit_price}")
            
            # 记录交易日志
            self.logger.bind(trade=True).info(f"开仓订单 - 数量: {quantity}, 价格: {price}, 杠杆: {self.leverage}x, 止损: {stop_loss_price}, 止盈: {take_profit_price}",
                                              extra={"TRADE": "BUY", "quantity": quantity, "price": price, "leverage": self.leverage,
                                                     "stop_loss": stop_loss_price, "take_profit": take_profit_price, "notional_value": notional_value})
            
            # 设置杠杆倍数
            try:
                self.client.futures_change_leverage(symbol=self.symbol, leverage=self.leverage)
                print(f"✅ 杠杆设置为 {self.leverage}x")
            except Exception as e:
                print(f"⚠️ 设置杠杆失败: {e}")
            
            # 下限价买单
            order = self.client.futures_create_order(
                symbol=self.symbol,
                side='BUY',
                type='LIMIT',
                timeInForce='GTC',
                price=price,
                quantity=quantity
            )
            # order = self.client.futures_create_order(
            #     symbol=self.symbol,
            #     side='BUY',
            #     type='LIMIT',  # 改为限价单
            #     timeInForce='GTC',  # 直到取消
            #     price=price,  # 指定限价
            #     quantity=quantity
            # )
            
            # # 设置止损单 (Stop Market)
            # stop_loss_order = self.client.futures_create_order(
            #     symbol=self.symbol,
            #     side='SELL',
            #     type='STOP_MARKET',
            #     quantity=quantity,
            #     stopPrice=stop_loss_price,
            #     reduceOnly=True
            # )
            
            # # 设置止盈单 (Take Profit Market)
            # take_profit_order = self.client.futures_create_order(
            #     symbol=self.symbol,
            #     side='SELL',
            #     type='TAKE_PROFIT_MARKET',
            #     quantity=quantity,
            #     stopPrice=take_profit_price,
            #     reduceOnly=True
            # )
            
            # print(f"✅ 止损单已设置: {stop_loss_order['orderId']}")
            # print(f"✅ 止盈单已设置: {take_profit_order['orderId']}")
            
            print(f"✅ 买单已提交: {order['orderId']}")
            self.logger.bind(trade=True).info(f"开仓订单成功: {order['orderId']}",
                                              extra={"TRADE": "BUY_SUCCESS", "order_id": order['orderId'], "quantity": quantity, "price": price})

            # 更新每日统计
            self.daily_stats['trades'] += 1
            
            # 记录下单时间
            self.last_order_time = time.time()
            
            # 保存订单信息用于后续止盈止损
            self.current_position = {
                'order_id': order['orderId'],
                'entry_price': price,
                'quantity': quantity,
                'leverage': self.leverage,
                'notional_value': notional_value,
                'margin_used': margin_required,
                'stop_loss': stop_loss_price,
                'take_profit': take_profit_price,
                'timestamp': time.time(),
                'highest_price': price,  # 记录最高价格用于移动止损
                'trailing_stop_price': stop_loss_price,  # 移动止损价格
                'type': 'long' # 记录持仓类型
            }
            
            return order
            
        except Exception as e:
            print(f"下单失败: {e}")
            return None

    def place_sell_order(self, price, quantity):
        """下限价卖单"""
        if not self.trading_enabled or quantity <= 0:
            return None
        
        try:
            # 计算止盈止损价格
            stop_loss_price = round(price * (1 + self.stop_loss_pct), 4)
            take_profit_price = round(price * (1 - self.take_profit_pct), 4)
            
            # 计算保证金和名义价值信息
            notional_value = quantity * price
            margin_required = notional_value / self.leverage
            
            print(f"🟡 准备开空仓:")
            print(f"   数量: {quantity} SOL")
            print(f"   价格: {price}")
            print(f"   杠杆: {self.leverage}x")
            print(f"   名义价值: {notional_value:.2f} USDT")
            print(f"   保证金: {margin_required:.2f} USDT")
            print(f"   止损: {stop_loss_price}")
            print(f"   止盈: {take_profit_price}")
            
            # 记录交易日志
            self.logger.bind(trade=True).info(f"开空仓订单 - 数量: {quantity}, 价格: {price}, 杠杆: {self.leverage}x, 止损: {stop_loss_price}, 止盈: {take_profit_price}",
                                              extra={"TRADE": "SELL", "quantity": quantity, "price": price, "leverage": self.leverage,
                                                     "stop_loss": stop_loss_price, "take_profit": take_profit_price, "notional_value": notional_value})
            
            # 设置杠杆倍数
            try:
                self.client.futures_change_leverage(symbol=self.symbol, leverage=self.leverage)
                print(f"✅ 杠杆设置为 {self.leverage}x")
            except Exception as e:
                print(f"⚠️ 设置杠杆失败: {e}")
            
            # 下限价卖单
            order = self.client.futures_create_order(
                symbol=self.symbol,
                side='SELL',
                type='LIMIT',
                timeInForce='GTC',
                price=price,
                quantity=quantity
            )
            # order = self.client.futures_create_order(
            #     symbol=self.symbol,
            #     side='SELL',
            #     type='LIMIT',  # 改为限价单
            #     timeInForce='GTC',  # 直到取消
            #     price=price,  # 指定限价
            #     quantity=quantity
            # )
            
            # # 设置止损单 (Stop Market)
            # stop_loss_order = self.client.futures_create_order(
            #     symbol=self.symbol,
            #     side='BUY',
            #     type='STOP_MARKET',
            #     quantity=quantity,
            #     stopPrice=stop_loss_price,
            #     reduceOnly=True
            # )
            
            # # 设置止盈单 (Take Profit Market)
            # take_profit_order = self.client.futures_create_order(
            #     symbol=self.symbol,
            #     side='BUY',
            #     type='TAKE_PROFIT_MARKET',
            #     quantity=quantity,
            #     stopPrice=take_profit_price,
            #     reduceOnly=True
            # )
            
            # print(f"✅ 止损单已设置: {stop_loss_order['orderId']}")
            # print(f"✅ 止盈单已设置: {take_profit_order['orderId']}")
            
            print(f"✅ 空单已提交: {order['orderId']}")
            self.logger.bind(trade=True).info(f"开空仓订单成功: {order['orderId']}",
                                              extra={"TRADE": "SELL_SUCCESS", "order_id": order['orderId'], "quantity": quantity, "price": price})

            # 更新每日统计
            self.daily_stats['trades'] += 1
            
            # 记录下单时间
            self.last_order_time = time.time()
            
            # 保存订单信息用于后续止盈止损
            self.current_position = {
                'order_id': order['orderId'],
                'entry_price': price,
                'quantity': quantity,
                'leverage': self.leverage,
                'notional_value': notional_value,
                'margin_used': margin_required,
                'stop_loss': stop_loss_price,
                'take_profit': take_profit_price,
                'timestamp': time.time(),
                'lowest_price': price,  # 记录最低价格用于移动止损（做空）
                'trailing_stop_price': stop_loss_price,  # 移动止损价格
                'type': 'short' # 记录持仓类型
            }
            
            return order
            
        except Exception as e:
            print(f"开空仓失败: {e}")
            return None

    def close_position(self, reason, current_price):
        """平仓操作"""
        if not self.trading_enabled or not self.current_position:
            return None
        
        try:
            quantity = self.current_position['quantity']
            entry_price = self.current_position['entry_price']
            position_type = self.current_position.get('type', 'long')  # 默认为多仓
            
            print(f"🔴 平仓原因: {reason}")
            print(f"   数量: {quantity} SOL")
            print(f"   开仓价: {entry_price}")
            print(f"   平仓价: {current_price}")
            
            # 计算盈亏（包含杠杆效应）
            if position_type == 'long':
                # 做多：价格上涨盈利，价格下跌亏损
                price_change_pct = (current_price - entry_price) / entry_price
                pnl = (current_price - entry_price) * quantity  # 名义盈亏
            else:
                # 做空：价格下跌盈利，价格上涨亏损
                price_change_pct = (entry_price - current_price) / entry_price
                pnl = (entry_price - current_price) * quantity  # 名义盈亏
            
            pnl_pct = price_change_pct * 100  # 价格变动百分比
            leveraged_pnl_pct = price_change_pct * self.leverage * 100  # 杠杆后的盈亏百分比
            
            print(f"   名义盈亏: {pnl:.4f} USDT ({pnl_pct:+.2f}%)")
            print(f"   杠杆盈亏: {leveraged_pnl_pct:+.2f}% (相对保证金)")
            
            # 记录交易日志
            self.logger.bind(trade=True).info(f"平仓订单 - 原因: {reason}, 开仓价: {entry_price}, 平仓价: {current_price}, 盈亏: {pnl:.4f} USDT",
                                              extra={"TRADE": "CLOSE", "reason": reason, "entry_price": entry_price, "exit_price": current_price,
                                                     "pnl": pnl, "pnl_pct": pnl_pct, "leveraged_pnl_pct": leveraged_pnl_pct})
            
            # 根据持仓方向设置平仓的 `side` 参数
            side = 'SELL' if position_type == 'long' else 'BUY'
            
            # 执行平仓订单（限价单）
            order = self.client.futures_create_order(
                symbol=self.symbol,
                side=side,
                type='LIMIT',
                timeInForce='GTC',
                price=current_price,
                quantity=quantity
            )
            
            print(f"✅ 平仓订单已提交: {order['orderId']}")
            self.logger.bind(trade=True).info(f"平仓订单成功: {order['orderId']}",
                                              extra={"TRADE": "CLOSE_SUCCESS", "order_id": order['orderId'], "reason": reason, "pnl": pnl})

            # 更新每日统计
            self.daily_stats['pnl'] += pnl
            
            # 记录交易历史
            trade_record = {
                'entry_time': self.current_position['timestamp'],
                'exit_time': time.time(),
                'entry_price': entry_price,
                'exit_price': current_price,
                'quantity': quantity,
                'leverage': self.leverage,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'leveraged_pnl_pct': leveraged_pnl_pct,
                'reason': reason,
                'duration': time.time() - self.current_position['timestamp']
            }
            self.position_history.append(trade_record)
            
            # 保存交易历史
            self.save_trade_history()
            
            # 清除当前持仓
            self.current_position = None
            
            return order
            
        except Exception as e:
            print(f"平仓失败: {e}")
            return None

    def check_stop_loss_take_profit(self, current_price):
        """检查止盈止损条件"""
        if not self.trading_enabled or not self.current_position:
            return
        
        # 添加调试信息
        print(f"🔍 检查止盈止损 - 当前价格: {current_price:.4f}")
        
        entry_price = self.current_position['entry_price']
        stop_loss = self.current_position['stop_loss']
        take_profit = self.current_position['take_profit']
        position_time = time.time() - self.current_position['timestamp']
        position_type = self.current_position.get('type', 'long')  # 默认为做多
        
        if position_type == 'long':
            # 做多逻辑
            # 更新最高价格用于移动止损
            if current_price > self.current_position['highest_price']:
                self.current_position['highest_price'] = current_price
                
                # 更新移动止损价格
                if self.trailing_stop_enabled:
                    new_trailing_stop = current_price * (1 - self.trailing_stop_distance)
                    if new_trailing_stop > self.current_position['trailing_stop_price']:
                        self.current_position['trailing_stop_price'] = new_trailing_stop
                        print(f"📈 移动止损更新: {new_trailing_stop:.4f}")
            
            # 检查紧急止损（优先级最高）
            emergency_stop_price = entry_price * (1 - self.emergency_stop_loss)
            if current_price <= emergency_stop_price:
                self.close_position(f"紧急止损触发 (跌幅{self.emergency_stop_loss*100}%)", current_price)
                return
            
            # 检查移动止损
            if self.trailing_stop_enabled and current_price <= self.current_position['trailing_stop_price']:
                self.close_position("移动止损触发", current_price)
                return
            
            # 检查固定止损
            print(f"   止损检查: 当前价格 {current_price:.4f} vs 止损价格 {stop_loss:.4f}")
            if current_price <= stop_loss:
                print(f"🛑 止损触发! 当前价格 {current_price:.4f} <= 止损价格 {stop_loss:.4f}")
                self.close_position("固定止损触发", current_price)
                return
            
            # 检查止盈
            print(f"   止盈检查: 当前价格 {current_price:.4f} vs 止盈价格 {take_profit:.4f}")
            if current_price >= take_profit:
                print(f"🎯 止盈触发! 当前价格 {current_price:.4f} >= 止盈价格 {take_profit:.4f}")
                self.close_position("止盈目标达成", current_price)
                return
                
        elif position_type == 'short':
            # 做空逻辑
            # 更新最低价格用于移动止损
            if current_price < self.current_position.get('lowest_price', float('inf')):
                self.current_position['lowest_price'] = current_price
                
                # 更新移动止损价格（做空时价格下跌，止损价格也下跌）
                if self.trailing_stop_enabled:
                    new_trailing_stop = current_price * (1 + self.trailing_stop_distance)
                    if new_trailing_stop < self.current_position['trailing_stop_price']:
                        self.current_position['trailing_stop_price'] = new_trailing_stop
                        print(f"📉 移动止损更新: {new_trailing_stop:.4f}")
            
            # 检查紧急止损（做空时价格上涨触发止损）
            emergency_stop_price = entry_price * (1 + self.emergency_stop_loss)
            if current_price >= emergency_stop_price:
                self.close_position(f"紧急止损触发 (涨幅{self.emergency_stop_loss*100}%)", current_price)
                return
            
            # 检查移动止损
            if self.trailing_stop_enabled and current_price >= self.current_position['trailing_stop_price']:
                self.close_position("移动止损触发", current_price)
                return
            
            # 检查固定止损（做空时价格上涨触发止损）
            print(f"   止损检查: 当前价格 {current_price:.4f} vs 止损价格 {stop_loss:.4f}")
            if current_price >= stop_loss:
                print(f"🛑 止损触发! 当前价格 {current_price:.4f} >= 止损价格 {stop_loss:.4f}")
                self.close_position("固定止损触发", current_price)
                return
            
            # 检查止盈（做空时价格下跌触发止盈）
            print(f"   止盈检查: 当前价格 {current_price:.4f} vs 止盈价格 {take_profit:.4f}")
            if current_price <= take_profit:
                print(f"🎯 止盈触发! 当前价格 {current_price:.4f} <= 止盈价格 {take_profit:.4f}")
                self.close_position("止盈目标达成", current_price)
                return
        
        # 检查最大持仓时间
        if position_time >= self.max_position_time:
            self.close_position("超过最大持仓时间", current_price)
            return

    def check_and_trade(self, current_price, current_rsi):
        """检查交易信号并执行交易"""
        if not self.trading_enabled:
            return
        
        # 检查是否有持仓，有持仓时不开新仓
        if self.current_position:
            print(f"🔄 有持仓,不开新仓")
            return  # 有持仓时不开新仓
        
        current_time = time.time()
        
        # 检查冷却时间
        time_since_last_order = current_time - self.last_order_time
        print(f"⏰ 距离上次下单时间: {time_since_last_order:.1f}秒 (冷却时间: {self.order_cooldown}秒)")
        
        if time_since_last_order < self.order_cooldown:
            remaining_cooldown = self.order_cooldown - time_since_last_order
            print(f"⏳ 仍在冷却中，剩余 {remaining_cooldown:.1f} 秒")
            return
        
        # 检查RSI超卖信号
        if current_rsi < self.rsi_oversold:
            print(f"🎯 检测到RSI超卖信号: {current_rsi:.2f}")

            # 检查ADX过滤器
            if not self.check_adx_filter():
                print("🚫 ADX过滤器阻止做多交易")
                return

            # 检查每日交易限制
            if not self.check_daily_limits():
                print("⚠️ 已达到每日交易限制，跳过下单")
                return
            
            # 获取账户余额
            balance = self.get_account_balance()
            if balance is None:
                print("无法获取账户余额")
                return
            
            print(f"💰 账户余额: {balance:.2f} USDT")
            
            # 计算下单数量
            quantity = self.calculate_order_size(current_price, balance)
            if quantity <= 0:
                print("余额不足或数量过小，跳过下单")
                return
            
            # 执行下单
            order = self.place_buy_order(current_price, quantity)
            if order:
                print(f"🎉 交易已执行!")
                self.logger.bind(trade=True).info(f"新交易执行 - RSI: {current_rsi:.2f}, 价格: {current_price}",
                                                  extra={"TRADE": "NEW_TRADE", "rsi": current_rsi, "price": current_price, "type": "BUY"})
        else:
            print(f"📊 RSI未达到超卖条件，不执行交易")

        # 检查RSI超买信号
        if current_rsi > self.rsi_overbought:
            print(f"🎯 检测到RSI超买信号: {current_rsi:.2f}")

            # 检查ADX过滤器
            if not self.check_adx_filter():
                print("🚫 ADX过滤器阻止做空交易")
                return

            # 检查每日交易限制
            if not self.check_daily_limits():
                print("⚠️ 已达到每日交易限制，跳过下单")
                return
            
            # 获取账户余额
            balance = self.get_account_balance()
            if balance is None:
                print("无法获取账户余额")
                return
            
            print(f"💰 账户余额: {balance:.2f} USDT")
            
            # 计算下单数量
            quantity = self.calculate_order_size(current_price, balance)
            if quantity <= 0:
                print("余额不足或数量过小，跳过下单")
                return
            
            # 执行下单
            order = self.place_sell_order(current_price, quantity)
            if order:
                print(f"🎉 交易已执行!")
                self.logger.bind(trade=True).info(f"新交易执行 - RSI: {current_rsi:.2f}, 价格: {current_price}",
                                                  extra={"TRADE": "NEW_TRADE", "rsi": current_rsi, "price": current_price, "type": "SELL"})
        else:
            print(f"📊 RSI未达到超买条件，不执行交易")

    def print_latest_data(self, current_price=None, is_realtime=False):
        """打印最新的数据和RSI"""
        if len(self.klines_df) == 0:
            return
        
        latest = self.klines_df.iloc[-1]
        
        # 计算RSI和ADX，包含实时价格
        if current_price is not None and is_realtime:
            latest_rsi = self.calculate_rsi(current_price)
            latest_adx = self.calculate_adx(current_price)
            display_price = current_price
            price_label = "实时价格"
        else:
            latest_rsi = self.calculate_rsi()
            latest_adx = self.calculate_adx()
            display_price = float(latest['close'])
            price_label = "收盘价"
        
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 确保时间戳有时区信息，如果没有则添加
        if latest['timestamp'].tz is None:
            kline_time = latest['timestamp'].tz_localize('UTC').tz_convert('Asia/Shanghai').strftime("%Y-%m-%d %H:%M:%S")
        else:
            kline_time = latest['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
        
        # 优化的数据显示格式
        data_type = '实时' if is_realtime else '历史'
        print(f"\n{'='*80}")
        print(f"📊 {data_type}市场数据 [{current_time}]")
        print(f"{'='*80}")

        # K线基本信息
        print(f"⏰ K线时间: {kline_time}")
        print(f"{'─'*40}")

        # 价格信息 - 表格格式
        print("💰 价格信息:")
        print(f"   开盘价: {float(latest['open']):>8.4f} USDT")
        print(f"   最高价: {float(latest['high']):>8.4f} USDT")
        print(f"   最低价: {float(latest['low']):>8.4f} USDT")
        print(f"   {price_label}: {float(display_price):>8.4f} USDT")

        # 成交量信息（仅历史数据）
        if not is_realtime:
            print(f"   成交量: {float(latest['volume']):>8.2f}")

        # RSI分析
        print(f"{'─'*40}")
        if latest_rsi is not None:
            # RSI值和状态
            rsi_status = "🟢 超卖" if latest_rsi < self.rsi_oversold else ("🔴 超买" if latest_rsi > self.rsi_overbought else "🟡 中性")
            rsi_color = "🟢" if latest_rsi < self.rsi_oversold else ("🔴" if latest_rsi > self.rsi_overbought else "🟡")

            print(f"📈 RSI(6)指标:")
            print(f"   数值: {latest_rsi:>8.2f}")
            print(f"   状态: {rsi_status} (阈值: {self.rsi_oversold}-{self.rsi_overbought})")

            # RSI信号强度指示器
            if latest_rsi < self.rsi_oversold:
                signal_strength = "强" if latest_rsi < self.rsi_oversold - 5 else "中"
                print(f"   信号: 🟢 买入信号 ({signal_strength}) - RSI超卖区域")
            elif latest_rsi > self.rsi_overbought:
                signal_strength = "强" if latest_rsi > self.rsi_overbought + 5 else "中"
                print(f"   信号: 🔴 卖出信号 ({signal_strength}) - RSI超买区域")
            else:
                neutral_pct = (latest_rsi - self.rsi_oversold) / (self.rsi_overbought - self.rsi_oversold) * 100
                print(f"   信号: 🟡 观望 - 中性区域 ({neutral_pct:.0f}%)")

            # 优先检查现有持仓的止盈止损（无论RSI信号如何）
            if self.trading_enabled and self.current_position:
                self.check_stop_loss_take_profit(float(display_price))

            # 执行交易信号
            if latest_rsi > self.rsi_overbought or latest_rsi < self.rsi_oversold:
                print(f"{'─'*40}")
                print("🚀 执行交易检查...")
                self.check_and_trade(float(display_price), latest_rsi)
        else:
            print("⏳ RSI指标: 计算中...")
            print("   等待足够的历史数据...")

        # ADX分析
        print(f"{'─'*40}")
        if latest_adx is not None:
            # ADX值和状态分析
            adx_status = "🟢 强趋势" if latest_adx > self.adx_trend_threshold else ("🟡 中性" if latest_adx > self.adx_sideways_threshold else "🔴 弱趋势/震荡")
            adx_color = "🟢" if latest_adx > self.adx_trend_threshold else ("🟡" if latest_adx > self.adx_sideways_threshold else "🔴")

            print(f"📊 ADX指标 ({self.adx_period}周期):")
            print(f"   数值: {latest_adx:>8.2f}")
            print(f"   状态: {adx_status} (趋势阈值: {self.adx_trend_threshold})")

            # ADX信号强度指示器
            if latest_adx > self.adx_trend_threshold:
                print(f"   信号: 🟢 强趋势环境 - 适合趋势跟随策略")
            elif latest_adx < self.adx_sideways_threshold:
                print(f"   信号: 🔴 震荡环境 - 适合区间交易策略")
            else:
                print(f"   信号: 🟡 中性环境 - 谨慎交易")

            # ADX过滤器状态
            if self.adx_enable_filter:
                filter_active = (
                    (self.adx_filter_mode == 'trend' and latest_adx > self.adx_trend_threshold) or
                    (self.adx_filter_mode == 'sideways' and latest_adx < self.adx_sideways_threshold)
                )
                filter_status = "✅ 激活" if filter_active else "❌ 未激活"
                print(f"   过滤器: {filter_status} (模式: {self.adx_filter_mode})")
        else:
            print("⏳ ADX指标: 计算中...")
            print("   等待足够的历史数据...")

        print(f"{'='*80}")

        # 显示交易状态
        if self.trading_enabled:
            if self.current_position:
                entry_price = self.current_position['entry_price']
                current_price_val = current_price or float(latest['close'])
                quantity = self.current_position['quantity']
                
                # 计算盈亏（包含杠杆效应）
                position_type = self.current_position.get('type', 'long')
                if position_type == 'long':
                    # 做多：价格上涨盈利，价格下跌亏损
                    price_change_pct = (current_price_val - entry_price) / entry_price
                    unrealized_pnl = (current_price_val - entry_price) * quantity  # 名义盈亏
                else:
                    # 做空：价格下跌盈利，价格上涨亏损
                    price_change_pct = (entry_price - current_price_val) / entry_price
                    unrealized_pnl = (entry_price - current_price_val) * quantity  # 名义盈亏
                
                unrealized_pnl_pct = price_change_pct * 100  # 价格变动百分比
                leveraged_pnl_pct = price_change_pct * self.leverage * 100  # 杠杆后的盈亏百分比
                
                # 计算名义价值和保证金
                notional_value = quantity * current_price_val  # 当前名义价值
                margin_used = notional_value / self.leverage  # 使用的保证金
                
                position_duration = (time.time() - self.current_position['timestamp']) / 3600  # 小时
                
                # 持仓信息显示优化
                print(f"\n{'─'*60}")
                print(f"📈 当前持仓信息 ({'多头' if position_type == 'long' else '空头'})")
                print(f"{'─'*60}")

                # 基本持仓信息
                print("🏷️  基本信息:")
                print(f"   数量: {quantity:>8.2f} SOL")
                print(f"   杠杆: {self.leverage:>8d}x")
                print(f"   持仓时间: {position_duration:>6.1f} 小时")

                # 价格信息
                print("💰 价格信息:")
                print(f"   开仓价: {entry_price:>8.4f} USDT")
                print(f"   当前价: {current_price_val:>8.4f} USDT")
                if position_type == 'long':
                    print(f"   最高价: {self.current_position.get('highest_price', entry_price):>8.4f} USDT")
                else:
                    print(f"   最低价: {self.current_position.get('lowest_price', entry_price):>8.4f} USDT")

                # 资金信息
                print("💵 资金信息:")
                print(f"   名义价值: {notional_value:>8.2f} USDT")
                print(f"   保证金: {margin_used:>10.2f} USDT")

                # 盈亏信息
                pnl_color = "🟢" if unrealized_pnl >= 0 else "🔴"
                print(f"{pnl_color} 盈亏信息:")
                print(f"   名义盈亏: {unrealized_pnl:>+8.4f} USDT ({unrealized_pnl_pct:+.2f}%)")
                print(f"   杠杆盈亏: {leveraged_pnl_pct:+8.2f}% (相对保证金)")

                # 风险管理
                print("🛡️  风险管理:")
                print(f"   固定止损: {self.current_position['stop_loss']:>8.4f} USDT")
                print(f"   移动止损: {self.current_position['trailing_stop_price']:>8.4f} USDT")
                print(f"   止盈目标: {self.current_position['take_profit']:>8.4f} USDT")
                print(f"{'─'*60}")
            else:
                cooldown_remaining = max(0, self.order_cooldown - (time.time() - self.last_order_time))
                if cooldown_remaining > 0:
                    cooldown_minutes = cooldown_remaining / 60
                    cooldown_color = "🟢" if cooldown_minutes < 5 else "🟡" if cooldown_minutes < 10 else "🔴"
                    print(f"\n{cooldown_color} 下单冷却中: {cooldown_minutes:.1f} 分钟后可交易")
                else:
                    print(f"\n🟢 交易就绪: 可以进行新交易")
            
            # 优化后的交易统计显示
            if self.position_history:
                total_trades = len(self.position_history)
                profitable_trades = sum(1 for trade in self.position_history if trade['pnl'] > 0)
                losing_trades = total_trades - profitable_trades
                total_pnl = sum(trade['pnl'] for trade in self.position_history)
                win_rate = (profitable_trades / total_trades) * 100

                print(f"\n{'─'*50}")
                print("📊 历史交易统计")
                print(f"{'─'*50}")

                # 总体统计
                pnl_color = "🟢" if total_pnl >= 0 else "🔴"
                win_rate_color = "🟢" if win_rate >= 50 else "🟡" if win_rate >= 30 else "🔴"

                print(f"📈 总体表现:")
                print(f"   总交易次数: {total_trades:>8d} 次")
                print(f"   {win_rate_color} 胜率: {win_rate:>10.1f}% ({profitable_trades}胜/{losing_trades}负)")
                print(f"   {pnl_color} 总盈亏: {total_pnl:>+8.4f} USDT")

                # 计算平均每笔交易的盈亏
                avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
                print(f"   平均盈亏: {avg_pnl:>+8.4f} USDT/笔")

            # 优化后的今日统计显示
            if self.daily_stats['date']:
                print(f"\n{'─'*50}")
                print("📅 今日交易统计")
                print(f"{'─'*50}")

                trades_today = self.daily_stats['trades']
                pnl_today = self.daily_stats['pnl']
                remaining_loss = self.max_daily_loss + pnl_today

                # 今日表现
                today_color = "🟢" if pnl_today >= 0 else "🔴"
                print("🎯 今日表现:")
                print(f"   交易次数: {trades_today:>8d}/{self.max_daily_trades} 次")
                print(f"   {today_color} 今日盈亏: {pnl_today:>+8.4f} USDT")

                # 风险控制状态
                risk_status = "🟢 正常" if remaining_loss > 10 else "🟡 警告" if remaining_loss > 0 else "🔴 超限"
                print("🛡️  风险状态:")
                print(f"   {risk_status} 剩余风险额度: {remaining_loss:>6.2f} USDT")

                if trades_today >= self.max_daily_trades:
                    print("   ⚠️  已达到每日最大交易次数限制")
                elif remaining_loss <= 0:
                    print("   🚨 风险额度已用尽，暂停交易")

                print(f"{'─'*50}")

        print(f"\n{'═'*80}")
        print("✨ 数据更新完成")
        print(f"{'═'*80}")

    def get_trade_summary(self):
        """获取交易摘要统计"""
        if not self.position_history:
            return "暂无交易记录"
        
        total_trades = len(self.position_history)
        profitable_trades = sum(1 for trade in self.position_history if trade['pnl'] > 0)
        losing_trades = total_trades - profitable_trades
        
        total_pnl = sum(trade['pnl'] for trade in self.position_history)
        total_profit = sum(trade['pnl'] for trade in self.position_history if trade['pnl'] > 0)
        total_loss = sum(trade['pnl'] for trade in self.position_history if trade['pnl'] < 0)
        
        win_rate = (profitable_trades / total_trades) * 100 if total_trades > 0 else 0
        avg_profit = total_profit / profitable_trades if profitable_trades > 0 else 0
        avg_loss = total_loss / losing_trades if losing_trades > 0 else 0
        
        avg_duration = sum(trade['duration'] for trade in self.position_history) / total_trades / 3600  # 小时
        
        summary = f"""
=== 交易统计摘要 ===
总交易次数: {total_trades}
盈利交易: {profitable_trades} 次
亏损交易: {losing_trades} 次
胜率: {win_rate:.1f}%
总盈亏: {total_pnl:.4f} USDT
平均盈利: {avg_profit:.4f} USDT
平均亏损: {avg_loss:.4f} USDT
平均持仓时间: {avg_duration:.1f} 小时
==================
"""
        return summary

    def start_websocket_stream(self):
        """启动WebSocket实时数据流"""
        print("启动实时数据流...")
        
        # 首先获取历史数据
        if not self.get_historical_klines():
            print("无法获取历史数据，退出程序")
            return
        
        # 打印初始数据
        self.print_latest_data()
        
        print("开始接收实时K线数据...")
        print("正在建立WebSocket连接...")
        print("按 Ctrl+C 停止程序\n")
        
        # 创建WebSocket管理器
        proxy = self.config['network'].get('proxy', None)
        twm = ThreadedWebsocketManager(
            api_key=self.api_key, 
            api_secret=self.api_secret,
            https_proxy=proxy
        )
        
        try:
            print("启动WebSocket管理器...")
            twm.start()
            print("✅ WebSocket管理器启动成功")
            
            print("创建K线数据流...")
            # 启动K线数据流
            stream_name = twm.start_kline_futures_socket(
                callback=self.process_kline_data,
                symbol=self.symbol,
                interval=self.interval
            )
            print(f"✅ K线数据流已创建: {stream_name}")
            
            # 等待连接建立
            print("等待WebSocket连接建立...")
            time.sleep(3)
            print("✅ 连接已建立，开始监听数据...")
            
            # 添加定期检查机制
            last_update_time = time.time()
            check_interval = 60  # 每60秒检查一次
            
            # 保持程序运行
            while True:
                time.sleep(1)
                
                # 定期检查连接状态
                current_time = time.time()
                if current_time - last_update_time > check_interval:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 连接正常，等待新的K线数据...")
                    last_update_time = current_time
                
        except KeyboardInterrupt:
            print("\n程序被用户中断")
        except Exception as e:
            print(f"WebSocket连接错误: {e}")
            print("尝试重新连接...")
        finally:
            print("正在关闭WebSocket连接...")
            try:
                twm.stop()
                print("✅ WebSocket连接已关闭")
            except:
                pass

    def process_kline_data(self, msg):
        """处理接收到的K线数据"""
        try:
            # print(msg)
            kline_data = msg['k']
            
            # 显示K线状态
            is_closed = kline_data['x']  # K线是否已完成
            # print(is_closed)
            current_time = datetime.now().strftime('%H:%M:%S')
            current_price = float(kline_data['c'])
            
            if is_closed:
                print(f"\n🎉 [{current_time}] 新的15分钟K线已完成!")
                print("📊 更新历史K线数据...")
                
                # 创建新的K线数据
                new_kline = {
                    'timestamp': pd.to_datetime(kline_data['t'], unit='ms').tz_localize('UTC').tz_convert('Asia/Shanghai'),
                    'open': float(kline_data['o']),
                    'high': float(kline_data['h']),
                    'low': float(kline_data['l']),
                    'close': float(kline_data['c']),
                    'volume': float(kline_data['v']),
                    'close_time': kline_data['T'],
                    'quote_asset_volume': float(kline_data['q']),
                    'number_of_trades': kline_data['n'],
                    'taker_buy_base_asset_volume': float(kline_data['V']),
                    'taker_buy_quote_asset_volume': float(kline_data['Q']),
                    'ignore': kline_data['B']
                }
                
                # 添加到DataFrame
                new_df = pd.DataFrame([new_kline])
                self.klines_df = pd.concat([self.klines_df, new_df], ignore_index=True)
                
                # 保持数据量不超过配置的最大数据点数量（避免内存占用过大）
                max_data_points = self.config['data']['max_data_points']
                if len(self.klines_df) > max_data_points:
                    self.klines_df = self.klines_df.tail(max_data_points).reset_index(drop=True)
                
                # 显示更新后的历史数据RSI
                self.print_latest_data(is_realtime=False)
            else:
                # 实时计算和显示RSI（每次收到数据都更新）
                if not hasattr(self, 'last_realtime_update'):
                    self.last_realtime_update = 0
                
                current_timestamp = time.time()
                # 根据配置更新实时RSI显示（减少屏幕刷新频率）
                update_interval = self.config['data']['realtime_update_interval']
                if current_timestamp - self.last_realtime_update >= update_interval:
                    # 计算并显示实时RSI
                    self.print_latest_data(current_price=current_price, is_realtime=True)
                    self.last_realtime_update = current_timestamp
                
        except Exception as e:
            print(f"处理K线数据时出错: {e}")
            import traceback
            traceback.print_exc()

    def test_stop_loss_take_profit(self):
        """测试止盈止损逻辑"""
        print("🧪 测试止盈止损逻辑...")
        
        # 模拟一个做多持仓
        self.current_position = {
            'entry_price': 100.0,
            'stop_loss': 99.7,  # 100 * (1 - 0.003)
            'take_profit': 100.3,  # 100 * (1 + 0.003)
            'quantity': 1.0,
            'leverage': 10,
            'timestamp': time.time(),
            'highest_price': 100.0,
            'trailing_stop_price': 99.7,
            'type': 'long'
        }
        
        print(f"模拟持仓: 开仓价 {self.current_position['entry_price']}, 止损 {self.current_position['stop_loss']}, 止盈 {self.current_position['take_profit']}")
        
        # 测试止盈
        test_price = 100.4  # 超过止盈价格
        print(f"\n测试止盈 - 价格: {test_price}")
        self.check_stop_loss_take_profit(test_price)
        
        # 重置持仓
        self.current_position = {
            'entry_price': 100.0,
            'stop_loss': 99.7,
            'take_profit': 100.3,
            'quantity': 1.0,
            'leverage': 10,
            'timestamp': time.time(),
            'highest_price': 100.0,
            'trailing_stop_price': 99.7,
            'type': 'long'
        }
        
        # 测试止损
        test_price = 99.5  # 低于止损价格
        print(f"\n测试止损 - 价格: {test_price}")
        self.check_stop_loss_take_profit(test_price)
        
        # 清除测试持仓
        self.current_position = None
        print("✅ 测试完成")

    def run(self):
        """运行程序"""
        try:
            # 启动WebSocket数据流
            self.start_websocket_stream()
        except KeyboardInterrupt:
            print("\n程序已停止")

    def load_config(self, config_file):
        """加载YAML配置文件"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            print(f"✅ 配置文件加载成功: {config_file}")
            return config
        except FileNotFoundError:
            print(f"⚠️ 配置文件 {config_file} 不存在，使用默认配置")
            return self.get_default_config()
        except Exception as e:
            print(f"❌ 加载配置文件失败: {e}，使用默认配置")
            return self.get_default_config()

    def get_default_config(self):
        """获取默认配置"""
        return {
            'trading': {
                'symbol': 'SOLUSDT',
                'interval': '15m',
                'testnet': False
            },
            'rsi': {
                'period': 6,
                'oversold': 22,
                'overbought': 78
            },
            'adx': {
                'period': 14,
                'trend_threshold': 25,
                'sideways_threshold': 20,
                'enable_filter': True,
                'filter_mode': 'trend'
            },
            'position': {
                'ratio': 0.9,
                'leverage': 10,
                'max_ratio': 1.0
            },
            'risk_management': {
                'stop_loss_pct': 0.005,
                'take_profit_pct': 0.005,
                'emergency_stop_loss': 0.10,
                'max_position_time': 604800
            },
            'trailing_stop': {
                'enabled': False,
                'distance': 0.02
            },
            'trading_limits': {
                'order_cooldown': 600,
                'max_daily_trades': 10,
                'max_daily_loss': 50
            },
            'logging': {
                'level': 'INFO',
                'file_format': 'logs/rsi_trading_%Y%m%d.log',
                'console_output': True
            },
            'data': {
                'historical_limit': 100,
                'max_data_points': 200,
                'realtime_update_interval': 1
            },
            'network': {
                'proxy': 'http://192.168.21.84:8800',
                'websocket_timeout': 30,
                'reconnect_attempts': 3
            },
            'notifications': {
                'enabled': False,
                'telegram_bot_token': '',
                'telegram_chat_id': '',
                'email_enabled': False,
                'email_smtp_server': '',
                'email_username': '',
                'email_password': '',
                'email_recipients': []
            },
            'advanced': {
                'enable_short_trading': True,
                'enable_long_trading': True,
                'min_order_quantity': 0.01,
                'price_precision': 4,
                'quantity_precision': 2,
                'enable_test_mode': False
            }
        }

    def get_interval_constant(self, interval_str):
        """将字符串时间间隔转换为币安客户端常量"""
        interval_map = {
            '1m': Client.KLINE_INTERVAL_1MINUTE,
            '3m': Client.KLINE_INTERVAL_3MINUTE,
            '5m': Client.KLINE_INTERVAL_5MINUTE,
            '15m': Client.KLINE_INTERVAL_15MINUTE,
            '30m': Client.KLINE_INTERVAL_30MINUTE,
            '1h': Client.KLINE_INTERVAL_1HOUR,
            '2h': Client.KLINE_INTERVAL_2HOUR,
            '4h': Client.KLINE_INTERVAL_4HOUR,
            '6h': Client.KLINE_INTERVAL_6HOUR,
            '8h': Client.KLINE_INTERVAL_8HOUR,
            '12h': Client.KLINE_INTERVAL_12HOUR,
            '1d': Client.KLINE_INTERVAL_1DAY,
            '3d': Client.KLINE_INTERVAL_3DAY,
            '1w': Client.KLINE_INTERVAL_1WEEK,
            '1M': Client.KLINE_INTERVAL_1MONTH
        }
        return interval_map.get(interval_str, Client.KLINE_INTERVAL_15MINUTE)

    def print_config_summary(self):
        """打印配置摘要"""
        print(f"初始化 RSI 交易系统")
        print(f"交易对: {self.symbol}")
        print(f"时间周期: {self.config['trading']['interval']}")
        print(f"RSI 周期: {self.rsi_period}")
        print(f"RSI 超卖阈值: {self.rsi_oversold}")
        print(f"RSI 超买阈值: {self.rsi_overbought}")
        print(f"ADX 周期: {self.adx_period}")
        print(f"ADX 趋势阈值: {self.adx_trend_threshold}")
        print(f"ADX 震荡阈值: {self.adx_sideways_threshold}")
        print(f"ADX 过滤器: {'启用' if self.adx_enable_filter else '禁用'} ({self.adx_filter_mode}模式)")
        print(f"每次下单比例: {self.position_ratio*100}%")
        print(f"杠杆倍数: {self.leverage}x")
        print(f"止损: {self.stop_loss_pct*100}%")
        print(f"止盈: {self.take_profit_pct*100}%")
        print(f"移动止损: {'启用' if self.trailing_stop_enabled else '禁用'}")
        print(f"移动止损距离: {self.trailing_stop_distance*100}%")
        print(f"最大持仓时间: {self.max_position_time/3600}小时")
        print(f"紧急止损: {self.emergency_stop_loss*100}%")
        print(f"每日最大交易次数: {self.max_daily_trades}")
        print(f"每日最大亏损: {self.max_daily_loss} USDT")
        print(f"最大持仓比例: {self.max_position_ratio*100}%")


def main():
    """主函数"""
    import argparse
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='RSI交易系统')
    parser.add_argument('--config', '-c', default='config.yaml', 
                       help='配置文件路径 (默认: config.yaml)')
    parser.add_argument('--test', action='store_true', 
                       help='运行止盈止损测试')
    args = parser.parse_args()
    
    print("=" * 70)
    print("  币安合约 SOLUSDT 15分钟 RSI 实时交易系统")
    print("=" * 70)
    
    # API密钥配置说明
    print("\n📋 配置说明:")
    print("1. 设置环境变量:")
    print("   set BINANCE_API_KEY=your_api_key")
    print("   set BINANCE_API_SECRET=your_api_secret")
    print("2. 或者直接在代码中传入API密钥")
    print("3. 未配置API密钥将运行在监控模式\n")
    
    # 检查API密钥
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    
    if api_key and api_secret:
        print("✅ 检测到API密钥，交易功能已启用")
    else:
        print("⚠️ 未检测到API密钥，运行在监控模式")
    
    print("\n" + "="*70)
    
    # 创建RSI追踪器
    tracker = RSITracker(config_file=args.config)
    
    # 检查是否要运行测试
    if args.test:
        print("🧪 运行止盈止损测试...")
        tracker.test_stop_loss_take_profit()
        return
    
    # 正常运行
    tracker.run()


if __name__ == "__main__":
    main()