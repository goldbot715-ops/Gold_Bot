import asyncio
import pandas as pd
import ta
import time
from datetime import datetime, timedelta
import colorama
from colorama import Fore, Style
import firebase_admin
from firebase_admin import credentials, db
import os
import numpy as np
import json
import websockets
import aiohttp

# --- Configuration ---
CONFIG = {
    # --- Telegram Settings ---
    "telegram_token": os.environ.get("TELEGRAM_TOKEN"),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    
    # --- Tiingo API Settings ---
    "tiingo_api_token": os.environ.get("TIINGO_API_KEY"),
    "tiingo_websocket_url": "wss://api.tiingo.com/fx",

    "ema_bos_strategy": {
        "fast_ema_period": 50,
        "slow_ema_period": 200,
        "swing_lookback_period": 10,
        "risk_reward_ratio": 2.0,
    },
    "active_strategy": "advanced",  # Options: "bos", "advanced"

    # --- Advanced Strategy (MACD + RSI + ATR) ---
    "advanced_strategy": {
        "trend_timeframe": "4h",
        "trend_ema_period": 200,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "rsi_period": 14,
        "rsi_buy_threshold": 50,
        "rsi_sell_threshold": 50,
        "atr_period": 14,
        "atr_sl_multiplier": 2.0,  # Gold needs more breathing room
        "atr_tp_multiplier": 4.0
    },
    "firebase_db_url": os.environ.get("FIREBASE_DB_URL"),
    "firebase_credentials_json": os.environ.get("FIREBASE_CREDENTIALS_JSON"),
    "firebase_credentials_path": os.getenv(
        "FIREBASE_CREDENTIALS_PATH",
        "gold-bot-715-firebase-adminsdk-fbsvc-6a64a4c35a.json"
    ),

    # --- Proxy Settings (Optional) ---
    "proxy_url": os.environ.get("PROXY_URL"), # Example: "http://user:pass@host:port"
}

colorama.init(autoreset=True)

class GoldTradingBot:
    def __init__(self, config):
        self.config = config
        self.websocket = None
        self.db = self._initialize_firebase()
        self.active_signal = {}
        self.symbol = 'xauusd'
        self.ohlc_data = {}
        self.minute_candles = {}
        self.last_candle_timestamps = {}
        self.latest_price = 0.0
        self.last_history_cleanup = datetime.now()
        self.minute_candle_history = {}
        self.last_15m_candle_time = None
        
        # State variables for the Trend Resumption logic
        self.trend_state = 'NEUTRAL' # 'BULLISH', 'BEARISH', 'BULLISH_RETRACEMENT', 'BEARISH_RETRACEMENT'
        self.last_ema_fast = 0.0
        self.last_ema_slow = 0.0
        self.last_firebase_live_price_update = datetime.now()
        self._sync_active_signal_from_firebase()

    def _sync_active_signal_from_firebase(self):
        """
        Startup par Firebase se check karta hai agar koi 'HOLD' status wala signal mojood hai.
        Agar hai, to use self.active_signal mein load karta hai taake bot use monitor kar sake.
        """
        if not self.db:
            return

        try:
            print(f"{Fore.CYAN}Syncing active signals from Firebase...")
            signals = self.db.child('signals').child(self.symbol).get()
            
            if signals:
                for key, data in signals.items():
                    if data.get('status') == 'HOLD':
                        data['firebase_key'] = key
                        self.active_signal = data
                        print(f"{Fore.GREEN}Resumed monitoring active signal: {data['type']} (Entry: {data['entry_price']})")
                        return # Sirf ek active signal monitor karte hain
            
            print(f"{Fore.YELLOW}No active signals found in Firebase to resume.")
        except Exception as e:
            print(f"{Fore.RED}Error syncing signals from Firebase: {e}")

    def _format_symbol_for_display(self, symbol):
        return symbol[:3].upper() + '/' + symbol[3:].upper()

    async def _send_telegram_alert(self, message):
        """Sends an alert message to Telegram."""
        token = self.config.get('telegram_token')
        chat_id = self.config.get('telegram_chat_id')
        if not token or not chat_id:
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        print(f"{Fore.RED}Telegram alert failed: {await response.text()}")
        except Exception as e:
            print(f"{Fore.RED}Error sending Telegram alert: {e}")

    def _initialize_firebase(self):
        """
        Firebase Realtime Database se connection banata hai.
        Pehle Railway environment variable (FIREBASE_CREDENTIALS_JSON) check karta hai.
        """
        try:
            if not firebase_admin._apps:
                # 1. Railway Environment Variable Check
                cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
                db_url = self.config.get('firebase_db_url')

                if cred_json:
                    try:
                        print(f"{Fore.CYAN}Initializing Firebase from environment variable (FIREBASE_CREDENTIALS_JSON)...")
                        # JSON string ko dict me convert karo agar string hai
                        cred_info = json.loads(cred_json) if isinstance(cred_json, str) else cred_json
                        cred = credentials.Certificate(cred_info)
                        
                        firebase_admin.initialize_app(cred, {'databaseURL': db_url})
                        print(f"{Fore.GREEN}Successfully connected to Firebase Realtime Database (via Env Var).")
                        return db.reference('/')
                    except Exception as env_e:
                        print(f"{Fore.RED}Error initializing from FIREBASE_CREDENTIALS_JSON: {env_e}")

                # 2. Local File Path Fallback
                cred_path = self.config.get('firebase_credentials_path')
                if cred_path and os.path.exists(cred_path):
                    print(f"{Fore.CYAN}Initializing Firebase from local file: {cred_path}")
                    cred = credentials.Certificate(cred_path)
                    firebase_admin.initialize_app(cred, {'databaseURL': db_url})
                    print(f"{Fore.GREEN}Successfully connected to Firebase (via File).")
                    return db.reference('/')
                else:
                    print(f"{Fore.RED}Firebase initialization failed: No valid credentials found (Env Var or File).")
                    return None
            
            return db.reference('/')
        except Exception as e: 
            print(f"{Fore.RED}Critical error in Firebase initialization: {e}")
            return None

    async def _connect_and_subscribe(self):
        """
        Tiingo WebSocket se connect hota hai aur Gold (XAUUSD) subscribe karta hai.
        Heartbeat (ping/pong) aur exponential backoff ke saath stable reconnection handle karta hai.
        """
        base_token = self.config['tiingo_api_token']
        if not base_token:
            print(f"{Fore.RED}[CRITICAL] TIINGO_API_KEY environment variable is not set!")
            return

        uri = f"{self.config['tiingo_websocket_url']}?token={base_token}"
        
        backoff = 5
        max_backoff = 40  # 5s -> 10s -> 20s -> 40s

        while True:
            try:
                # ping_interval aur ping_timeout add kiya heartbeat ke liye
                async with websockets.connect(
                    uri, 
                    ping_interval=20, 
                    ping_timeout=20,
                    close_timeout=10
                ) as websocket:
                    self.websocket = websocket
                    print(f"{Fore.GREEN}Successfully connected to Tiingo WebSocket for Gold.")
                    
                    # Reset backoff on success
                    backoff = 5
                    
                    await self._subscribe_to_gold()
                    await self._handle_websocket_messages()

            except (websockets.exceptions.ConnectionClosed, ConnectionError) as e:
                source = "Server" if hasattr(e, 'rcvd') and e.rcvd else "Client"
                code = getattr(e, 'code', 'Unknown')
                reason = getattr(e, 'reason', 'No reason provided')
                
                print(f"{Fore.RED}[DISCONNECT] {source} closed the connection. Code: {code}, Reason: {reason}")
                print(f"{Fore.YELLOW}Reconnecting in {backoff} seconds (Exponential Backoff)...")
                
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

            except Exception as e:
                print(f"{Fore.RED}[CRITICAL ERROR] WebSocket connection failed: {e}")
                print(f"{Fore.YELLOW}Attempting recovery in {backoff} seconds...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _subscribe_to_gold(self):
        """
        Tiingo ko subscription message bhejta hai.
        """
        if not self.websocket:
            return

        print(f"{Fore.CYAN}Subscribing to {self._format_symbol_for_display(self.symbol)} ticks.")
        subscription_message = {
            "eventName": "subscribe",
            "authorization": self.config['tiingo_api_token'],
            "eventData": { "tickers": [self.symbol] }
        }
        await self.websocket.send(json.dumps(subscription_message))

    def _update_minute_candle(self, tick_data):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        current_minute_dt = now.replace(second=0, microsecond=0)
        
        if self.symbol not in self.minute_candle_history:
            self.minute_candle_history[self.symbol] = []
        
        if self.symbol not in self.minute_candles or self.minute_candles[self.symbol]['timestamp'] < current_minute_dt:
            if self.symbol in self.minute_candles:
                self.minute_candle_history[self.symbol].append(self.minute_candles[self.symbol])
                if len(self.minute_candle_history[self.symbol]) > 1000:
                    self.minute_candle_history[self.symbol] = self.minute_candle_history[self.symbol][-1000:]
            
            self.minute_candles[self.symbol] = {
                'timestamp': current_minute_dt,
                'open': tick_data['price'], 'high': tick_data['price'], 'low': tick_data['price'],
                'close': tick_data['price'], 'volume': 0 
            }
        else:
            candle = self.minute_candles[self.symbol]
            candle['high'] = max(candle['high'], tick_data['price'])
            candle['low'] = min(candle['low'], tick_data['price'])
            candle['close'] = tick_data['price']

    def _build_15m_candle(self):
        if self.symbol not in self.minute_candles: return
        last_minute_candle = self.minute_candles[self.symbol]
        key_15m = f"{self.symbol}_15"
        
        if key_15m not in self.ohlc_data: self.ohlc_data[key_15m] = pd.DataFrame()
        
        if last_minute_candle['timestamp'].minute % 15 == 0 and \
           (key_15m not in self.last_candle_timestamps or self.last_candle_timestamps[key_15m] < last_minute_candle['timestamp']):
            
            if self.last_15m_candle_time != last_minute_candle['timestamp']:
                self.last_15m_candle_time = last_minute_candle['timestamp']
                
                if self.symbol in self.minute_candle_history and len(self.minute_candle_history[self.symbol]) >= 15:
                    minute_candles = self.minute_candle_history[self.symbol][-15:]
                    fifteen_min_candle = {
                        'timestamp': minute_candles[0]['timestamp'],
                        'open': minute_candles[0]['open'],
                        'high': max(c['high'] for c in minute_candles),
                        'low': min(c['low'] for c in minute_candles),
                        'close': minute_candles[-1]['close'],
                        'volume': sum(c['volume'] for c in minute_candles)
                    }
                    new_candle_df = pd.DataFrame([fifteen_min_candle])
                    self.ohlc_data[key_15m] = pd.concat([self.ohlc_data[key_15m], new_candle_df], ignore_index=True)
                    if len(self.ohlc_data[key_15m]) > 500: self.ohlc_data[key_15m] = self.ohlc_data[key_15m].iloc[-500:]
                    self.last_candle_timestamps[key_15m] = last_minute_candle['timestamp']
                    
                    if not self.active_signal:
                        self._check_for_signal(fifteen_min_candle['close'])

    async def _handle_websocket_messages(self):
        """
        WebSocket se har tick message read karta hai aur indicators aggregate karta hai.
        Disconnect hone par gracefully return karta hai taake outer loop reconnect kar sake.
        """
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    if data.get("service") == "fx" and "data" in data and data.get("messageType") == "A":
                        tick_data = data['data']
                        if len(tick_data) >= 6:
                            symbol = tick_data[1]
                            if symbol == self.symbol:
                                bid = tick_data[4]; ask = tick_data[5]
                                price = (bid + ask) / 2
                                self.latest_price = price
                                self._update_minute_candle({'price': price, 'timestamp': datetime.now()})
                                
                                # Signal check aur Firebase sync
                                if self.active_signal:
                                    self._check_tp_sl(price)
                                    self._update_signal_price_in_firebase(price)
                                else:
                                    now = datetime.now()
                                    if (now - self.last_firebase_live_price_update).total_seconds() > 60:
                                        self._update_live_price_in_firebase(price)
                                        self.last_firebase_live_price_update = now
                                
                                self._build_15m_candle()
                except json.JSONDecodeError: 
                    pass
                except Exception as e: 
                    print(f"{Fore.RED}[ERROR] Error processing message: {e}")
        except websockets.exceptions.ConnectionClosed:
            # Outer loop will handle this
            raise
        except Exception as e:
            print(f"{Fore.RED}[CRITICAL] Error in message handler: {e}")
            raise

    # --- Firebase and TP/SL methods ---
    def _update_signal_price_in_firebase(self, current_price):
        if not self.active_signal: return
        signal_data = self.active_signal; firebase_key = signal_data.get('firebase_key')
        if not firebase_key: return
        try:
            update_data = {"current_price": current_price, "last_updated": datetime.now().isoformat()}
            signal_ref = self.db.child('signals').child(self.symbol).child(firebase_key); signal_ref.update(update_data)
        except Exception as e: print(f"{Fore.RED}Error updating signal price: {e}")

    def _update_live_price_in_firebase(self, current_price):
        try:
            update_data = {"price": current_price, "timestamp": datetime.now().isoformat()}
            price_ref = self.db.child('live_prices').child(self.symbol); price_ref.update(update_data)
        except Exception as e: pass

    def _check_tp_sl(self, current_price):
        if not self.active_signal: return
        signal_data = self.active_signal; tp_hit, sl_hit = False, False
        signal_type = signal_data.get('type', '')
        
        if "Buy" in signal_type:
            if current_price >= signal_data['tp']: tp_hit = True
            elif current_price <= signal_data['sl']: sl_hit = True
        elif "Sell" in signal_type:
            if current_price <= signal_data['tp']: tp_hit = True
            elif current_price >= signal_data['sl']: sl_hit = True
        
        if tp_hit or sl_hit:
            result = f"{Fore.GREEN}TP HIT" if tp_hit else f"{Fore.RED}SL HIT"
            print(f"\n{Fore.MAGENTA}{'='*40}\n{Fore.CYAN}{self._format_symbol_for_display(self.symbol)} | {signal_data['type']} Closed\n{result} at {current_price:.5f}\n{Fore.MAGENTA}{'='*40}\n")
            update_data = {"status": "TP_HIT" if tp_hit else "SL_HIT", "closed_at": datetime.now().isoformat(), "close_price": current_price, "close_reason": "TP_HIT" if tp_hit else "SL_HIT"}
            self._update_signal_in_firebase(signal_data.get('firebase_key'), update_data)
            self._save_to_history(signal_data, current_price, tp_hit)
            self.active_signal = {}
            self._update_live_price_in_firebase(current_price)

    def _save_to_history(self, signal_data, close_price, tp_hit):
        if not self.db: return
        try:
            history_ref = self.db.child('history')
            history_entry = {"symbol": self.symbol, "type": signal_data['type'], "condition": signal_data['condition'], "entry_price": signal_data['entry_price'], "tp": signal_data['tp'], "sl": signal_data['sl'], "close_price": close_price, "status": "TP_HIT" if tp_hit else "SL_HIT", "open_timestamp": signal_data.get('timestamp', datetime.now().isoformat()), "close_timestamp": datetime.now().isoformat(), "profit_loss_pct": ((close_price - signal_data['entry_price']) / signal_data['entry_price']) * 100}
            history_ref.push(history_entry)
        except Exception as e: print(f"{Fore.RED}Error saving to history: {e}")

    def _cleanup_history(self):
        if not self.db: return
        try:
            history_ref = self.db.child('history'); three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
            all_history = history_ref.get()
            if not all_history: return
            for key, entry in all_history.items():
                if entry.get('close_timestamp', '') < three_days_ago:
                    history_ref.child(key).delete()
            self.last_history_cleanup = datetime.now()
        except Exception as e: print(f"{Fore.RED}Error during cleanup: {e}")

    # --- FINAL & REFINED: Trend Resumption with Multiple BOS Entries ---
    def _generate_ema_bos_signal(self):
        config = self.config['ema_bos_strategy']
        key = f"{self.symbol}_15"
        
        if key not in self.ohlc_data or len(self.ohlc_data[key]) < config['slow_ema_period'] + config['swing_lookback_period']:
            return None

        df = self.ohlc_data[key].copy()
        df['ema_fast'] = ta.trend.ema_indicator(df['close'], window=config['fast_ema_period'])
        df['ema_slow'] = ta.trend.ema_indicator(df['close'], window=config['slow_ema_period'])
        df['swing_high'] = df['high'].rolling(window=config['swing_lookback_period'], center=True).max()
        df['swing_low'] = df['low'].rolling(window=config['swing_lookback_period'], center=True).min()
        
        if len(df) < 2: return None
        last_candle = df.iloc[-1]; prev_candle = df.iloc[-2]
        current_ema_fast = last_candle['ema_fast']; current_ema_slow = last_candle['ema_slow']

        # --- 1. TREND DEFINITION (EMA Crossover) ---
        if self.trend_state in ['BEARISH', 'BEARISH_RETRACEMENT', 'NEUTRAL'] and self.last_ema_fast < self.last_ema_slow and current_ema_fast > current_ema_slow:
            self.trend_state = 'BULLISH'
            print(f"{Fore.GREEN}[TREND DEFINITION] EMA Cross: New BULLISH trend established.")
        elif self.trend_state in ['BULLISH', 'BULLISH_RETRACEMENT', 'NEUTRAL'] and self.last_ema_fast > self.last_ema_slow and current_ema_fast < current_ema_slow:
            self.trend_state = 'BEARISH'
            print(f"{Fore.RED}[TREND DEFINITION] EMA Cross: New BEARISH trend established.")

        # --- 2. MARKET STRUCTURE LOGIC ---
        # --- In a BULLISH Trend ---
        if self.trend_state == 'BULLISH':
            last_swing_low_price = df['swing_low'].dropna().iloc[-1]
            if last_candle['close'] < last_swing_low_price:
                self.trend_state = 'BULLISH_RETRACEMENT'
                print(f"{Fore.YELLOW}[STRUCTURE] Bearish ChoCh. Waiting for trend to RESUME with a new BOS.")

        # --- In a BULLISH RETRACEMENT ---
        if self.trend_state == 'BULLISH_RETRACEMENT':
            last_swing_high_price = df['swing_high'].dropna().iloc[-1]
            # This is the BOS in the direction of the main trend (Trend Resumption)
            if last_candle['close'] > last_swing_high_price and last_candle['volume'] > prev_candle['volume']:
                print(f"{Fore.GREEN}[SIGNAL] Bullish BOS. Entering BUY trade.")
                entry_price = last_candle['close']
                
                # *** KEY CHANGE: SL is the most recent swing low ***
                most_recent_swing_low = df['swing_low'].dropna().iloc[-1]
                sl_price = most_recent_swing_low
                
                risk = entry_price - sl_price
                tp_price = entry_price + (risk * config['risk_reward_ratio'])
                
                # State remains BULLISH_RETRACEMENT to allow for more entries
                return {"type": "Buy", "condition": "Bullish Trend Resumption (BOS)", "entry_price": entry_price, "sl": sl_price, "tp": tp_price}

        # --- In a BEARISH Trend ---
        if self.trend_state == 'BEARISH':
            last_swing_high_price = df['swing_high'].dropna().iloc[-1]
            if last_candle['close'] > last_swing_high_price:
                self.trend_state = 'BEARISH_RETRACEMENT'
                print(f"{Fore.YELLOW}[STRUCTURE] Bullish ChoCh. Waiting for trend to RESUME with a new BOS.")

        # --- In a BEARISH RETRACEMENT ---
        if self.trend_state == 'BEARISH_RETRACEMENT':
            last_swing_low_price = df['swing_low'].dropna().iloc[-1]
            # This is the BOS in the direction of the main trend (Trend Resumption)
            if last_candle['close'] < last_swing_low_price and last_candle['volume'] > prev_candle['volume']:
                print(f"{Fore.RED}[SIGNAL] Bearish BOS. Entering SELL trade.")
                entry_price = last_candle['close']

                # *** KEY CHANGE: SL is the most recent swing high ***
                most_recent_swing_high = df['swing_high'].dropna().iloc[-1]
                sl_price = most_recent_swing_high

                risk = sl_price - entry_price
                tp_price = entry_price - (risk * config['risk_reward_ratio'])

                # State remains BEARISH_RETRACEMENT to allow for more entries
                return {"type": "Sell", "condition": "Bearish Trend Resumption (BOS)", "entry_price": entry_price, "sl": sl_price, "tp": tp_price}

        self.last_ema_fast = current_ema_fast; self.last_ema_slow = current_ema_slow
        return None

    def _add_advanced_indicators(self, df):
        """Adds all required indicators for the Advanced Strategy to the DataFrame."""
        adv_cfg = self.config['advanced_strategy']
        # MACD
        macd = ta.trend.MACD(
            df['close'],
            window_fast=adv_cfg['macd_fast'],
            window_slow=adv_cfg['macd_slow'],
            window_sign=adv_cfg['macd_signal']
        )
        df['macd_val'] = macd.macd()
        df['macd_sig'] = macd.macd_signal()
        df['macd_diff'] = macd.macd_diff()
        # RSI
        df['rsi_val'] = ta.momentum.rsi(df['close'], window=adv_cfg['rsi_period'])
        # ATR
        df['atr_val'] = ta.volatility.average_true_range(
            df['high'], df['low'], df['close'], window=adv_cfg['atr_period']
        )
        return df

    def _generate_advanced_signal(self):
        """Generates a signal using the Advanced Strategy (Trend EMA, MACD, RSI, ATR)."""
        adv_cfg = self.config['advanced_strategy']

        # 1. Trend Confirmation (HTF - 4H)
        # Gold HTF aggregation is not explicitly shown, but we can assume it's available or can be fetched
        # For Gold_Bot, it seems to handle 15m primarily. Let's adapt to use what's available.
        key_15m = f"{self.symbol}_15"
        if key_15m not in self.ohlc_data or self.ohlc_data[key_15m] is None:
            return None

        df_15m = self.ohlc_data[key_15m].copy()
        
        # Trend EMA (Long-term)
        df_15m['ema_trend'] = ta.trend.ema_indicator(df_15m['close'], window=adv_cfg['trend_ema_period'])
        df_15m = self._add_advanced_indicators(df_15m)
        
        if len(df_15m) < 3:
            return None

        last = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]

        is_uptrend = (last['close'] > last['ema_trend'])
        is_downtrend = (last['close'] < last['ema_trend'])

        # --- Buy Signal Conditions ---
        if is_uptrend:
            macd_crossed_up = prev['macd_val'] < prev['macd_sig'] and last['macd_val'] > last['macd_sig']
            rsi_is_ok = last['rsi_val'] > adv_cfg['rsi_buy_threshold']
            if macd_crossed_up and rsi_is_ok:
                sl, tp = self._calculate_atr_sl_tp("Buy", last['close'], last['atr_val'])
                return {
                    "type": "Advanced Buy",
                    "condition": f"Uptrend (200 EMA) + MACD Crossover",
                    "entry_price": last['close'],
                    "sl": sl,
                    "tp": tp
                }

        # --- Sell Signal Conditions ---
        if is_downtrend:
            macd_crossed_down = prev['macd_val'] > prev['macd_sig'] and last['macd_val'] < last['macd_sig']
            rsi_is_ok = last['rsi_val'] < adv_cfg['rsi_sell_threshold']
            if macd_crossed_down and rsi_is_ok:
                sl, tp = self._calculate_atr_sl_tp("Sell", last['close'], last['atr_val'])
                return {
                    "type": "Advanced Sell",
                    "condition": f"Downtrend (200 EMA) + MACD Crossover",
                    "entry_price": last['close'],
                    "sl": sl,
                    "tp": tp
                }

        return None

    def _calculate_atr_sl_tp(self, signal_type, entry_price, atr):
        """Calculates ATR-based SL/TP for Gold."""
        adv_cfg = self.config['advanced_strategy']
        sl_mult = adv_cfg['atr_sl_multiplier']
        tp_mult = adv_cfg['atr_tp_multiplier']

        if "Buy" in signal_type:
            sl = entry_price - (atr * sl_mult)
            tp = entry_price + (atr * tp_mult)
        else:
            sl = entry_price + (atr * sl_mult)
            tp = entry_price - (atr * tp_mult)
        return sl, tp

    def _send_signal_to_firebase(self, signal_data):
        if not self.db: return None
        try:
            signal_ref = self.db.child('signals').child(self.symbol)
            new_signal_ref = signal_ref.push({
                "type": signal_data['type'], "condition": signal_data['condition'], "entry_price": signal_data['entry_price'],
                "tp": signal_data['tp'], "sl": signal_data['sl'], "status": "HOLD",
                "timestamp": datetime.now().isoformat(), "current_price": signal_data['entry_price'], "last_updated": datetime.now().isoformat()
            })
            print(f"{Fore.CYAN}Signal sent to Firebase: {signal_data['type']}")
            return new_signal_ref.key
        except Exception as e: print(f"{Fore.RED}Error sending signal: {e}")

    def _update_signal_in_firebase(self, firebase_key, update_data):
        if not self.db or not firebase_key: return
        try: self.db.child('signals').child(self.symbol).child(firebase_key).update(update_data)
        except Exception as e: print(f"{Fore.RED}Error updating signal: {e}")

    def _display_signal(self, signal_data, current_price):
        signal_type = signal_data['type']; color = Fore.GREEN if "Buy" in signal_type else Fore.RED
        print(f"\n{Fore.MAGENTA}{'='*40}\n{color}NEW SIGNAL: {signal_type} | {self._format_symbol_for_display(self.symbol)}\n{Fore.CYAN}Condition: {signal_data['condition']}\n{Fore.YELLOW}Entry: {signal_data['entry_price']:.5f}\n{Fore.GREEN}TP: {signal_data['tp']:.5f}\n{Fore.RED}SL: {signal_data['sl']:.5f}\n{Fore.BLUE}Live: {current_price:.5f}\n{Fore.MAGENTA}{'='*40}\n")

    def _check_for_signal(self, current_price):
        strategy_name = self.config.get('active_strategy', 'bos')
        signal = None
        
        if strategy_name == 'advanced':
            signal = self._generate_advanced_signal()
        elif strategy_name == 'bos':
            signal = self._generate_ema_bos_signal()
        else:
            signal = self._generate_ema_bos_signal()

        if signal:
            firebase_key = self._send_signal_to_firebase(signal)
            signal['firebase_key'] = firebase_key
            signal['timestamp'] = datetime.now().isoformat()
            self.active_signal = signal
            self._display_signal(signal, current_price)

    async def _print_price_updates(self):
        while True:
            await asyncio.sleep(60)
            print(f"\n--- Live {self._format_symbol_for_display(self.symbol)} Update ({datetime.now().strftime('%H:%M:%S')}) ---")
            if self.active_signal:
                signal = self.active_signal; color = Fore.GREEN if "Buy" in signal['type'] else Fore.RED
                print(f"{color}{self._format_symbol_for_display(self.symbol):<12} | HOLD | Entry: {signal['entry_price']:.5f} | TP: {signal['tp']:.5f} | SL: {signal['sl']:.5f} | Live: {self.latest_price:.5f}")
            else:
                if self.latest_price > 0: print(f"{self._format_symbol_for_display(self.symbol):<12} | WAIT FOR SIGNAL | Live: {self.latest_price:.5f}")
                else: print(f"{self._format_symbol_for_display(self.symbol):<12} | WAIT FOR SIGNAL")
            print("-" * 40)
            if (datetime.now() - self.last_history_cleanup).days >= 1: self._cleanup_history()

    async def run(self):
        """
        Main bot run method:
        - Gold ticker set karta hai
        - Startup par history cleanup karta hai
        - Periodic price print task start karta hai (sirf ek baar)
        - WebSocket se connect ho kar infinite loop chalata hai
        """
        print(f"{Fore.GREEN}Starting Gold Trading Bot with Advanced Strategy...")
        
        # Verify API Token
        if not self.config.get('tiingo_api_token'):
            print(f"{Fore.RED}[CRITICAL] TIINGO_API_KEY environment variable is missing!")
            return

        # Startup par history cleanup
        self._cleanup_history()

        # Background task: live price prints (sirf ek baar)
        if not hasattr(self, '_price_update_task_started'):
            asyncio.create_task(self._print_price_updates())
            self._price_update_task_started = True

        # WebSocket connect + message handling (reconnect logic andar hai)
        await self._connect_and_subscribe()

if __name__ == "__main__":
    # Yeh main loop bot ko crash hone par auto-restart karta hai
    while True:
        try:
            bot = GoldTradingBot(CONFIG)
            asyncio.run(bot.run())

        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Bot stopped by user. Exiting...")
            break

        except Exception as e:
            print(f"{Fore.RED}[CRITICAL ERROR] Bot crashed: {e}. Restarting in 30 seconds...")
            time.sleep(30)