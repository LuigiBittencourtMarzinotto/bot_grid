import ccxt
import pandas as pd
import numpy as np
import time
import sqlite3
import logging
import requests
import os
import sys
from dotenv import load_dotenv
from datetime import datetime
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

# ==========================================
# CLASSE TREND BOT (ESTRATÉGIA SUPER_TREND + ADX)
# ==========================================

class TrendBot:
    def __init__(self):
        self._setup_logging()
        self._load_config()
        self._init_db()
        self._connect_exchange()
        self.running = True

    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - [TREND] %(message)s',
            handlers=[
                logging.FileHandler("trend_bot_v2.log"),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger("TrendBot")

    def _load_config(self):
        load_dotenv()
        self.API_KEY = os.getenv('BINANCE_API_KEY')
        self.SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
        self.TG_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
        self.SIMULATION = os.getenv('MODO_SIMULACAO', 'true').lower() == 'true'
        
        self.SYMBOL = os.getenv('SYMBOL', 'BTC/USDT')
        self.TIMEFRAME = os.getenv('TREND_TIMEFRAME', '1h')
        self.RISK_PER_TRADE = float(os.getenv('TREND_RISK_PER_TRADE', 0.10))
        
        # --- PARÂMETROS DA ESTRATÉGIA ---
        self.SUPERTREND_PERIOD = 10
        self.SUPERTREND_MULTIPLIER = 3.0
        self.ADX_THRESHOLD = 25  # Só opera se a força da tendência for maior que 25
        
        self.DB_NAME = "trend_data.db"
        self.SIM_BALANCE = 1000.0 

    def _connect_exchange(self):
        try:
            self.exchange = ccxt.binance({
                'apiKey': self.API_KEY,
                'secret': self.SECRET_KEY,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
            self.exchange.load_markets()
            self.logger.info(f"Conectado à Binance: {self.SYMBOL}")
        except Exception as e:
            self.logger.error(f"Erro Conexão: {e}")
            sys.exit(1)

    def _init_db(self):
        conn = sqlite3.connect(self.DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS position_state (
                id INTEGER PRIMARY KEY,
                in_position BOOLEAN,
                entry_price REAL,
                quantity REAL,
                stop_loss REAL,
                highest_price REAL,
                entry_time TEXT
            )
        ''')
        cursor.execute('SELECT count(*) FROM position_state')
        if cursor.fetchone()[0] == 0:
            cursor.execute('INSERT INTO position_state VALUES (1, 0, 0.0, 0.0, 0.0, 0.0, "")')
        conn.commit()
        conn.close()

    def telegram_send(self, message):
        if not self.TG_TOKEN or not self.TG_CHAT_ID: return
        try:
            url = f"https://api.telegram.org/bot{self.TG_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": self.TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            self.logger.error(f"Erro Telegram: {e}")

    # ==========================
    # CÁLCULO DE INDICADORES (SUPERTREND + ADX)
    # ==========================

    def calculate_supertrend(self, df):
        # 1. ATR
        atr_indicator = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=self.SUPERTREND_PERIOD)
        df['atr'] = atr_indicator.average_true_range()

        # 2. Basic Bands
        hl2 = (df['high'] + df['low']) / 2
        df['basic_upper'] = hl2 + (self.SUPERTREND_MULTIPLIER * df['atr'])
        df['basic_lower'] = hl2 - (self.SUPERTREND_MULTIPLIER * df['atr'])

        # 3. Final Bands (Lógica Iterativa)
        # Precisamos iterar para garantir a regra de "não recuar" do SuperTrend
        # Isso é um pouco mais lento que vetorizado, mas essencial para precisão.
        final_upper = [0.0] * len(df)
        final_lower = [0.0] * len(df)
        supertrend = [0.0] * len(df)
        in_uptrend = [True] * len(df)

        for i in range(1, len(df)):
            # Cálculo Upper
            if df['basic_upper'].iloc[i] < final_upper[i-1] or df['close'].iloc[i-1] > final_upper[i-1]:
                final_upper[i] = df['basic_upper'].iloc[i]
            else:
                final_upper[i] = final_upper[i-1]

            # Cálculo Lower
            if df['basic_lower'].iloc[i] > final_lower[i-1] or df['close'].iloc[i-1] < final_lower[i-1]:
                final_lower[i] = df['basic_lower'].iloc[i]
            else:
                final_lower[i] = final_lower[i-1]

            # Definição da Tendência
            if in_uptrend[i-1]:
                if df['close'].iloc[i] < final_lower[i-1]:
                    in_uptrend[i] = False # Virou para Baixa
                else:
                    in_uptrend[i] = True
            else:
                if df['close'].iloc[i] > final_upper[i-1]:
                    in_uptrend[i] = True # Virou para Alta
                else:
                    in_uptrend[i] = False
            
            # Valor do SuperTrend para o gráfico
            if in_uptrend[i]:
                supertrend[i] = final_lower[i]
            else:
                supertrend[i] = final_upper[i]

        df['SuperTrend'] = supertrend
        df['In_Uptrend'] = in_uptrend
        return df

    def process_data(self):
        try:
            ohlcv = self.exchange.fetch_ohlcv(self.SYMBOL, self.TIMEFRAME, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # 1. EMA 200 (Filtro Macro)
            df['EMA_200'] = EMAIndicator(close=df["close"], window=200).ema_indicator()
            
            # 2. ADX (Filtro de Força)
            adx = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
            df['ADX'] = adx.adx()
            
            # 3. SuperTrend (Sinal de Entrada/Saída)
            df = self.calculate_supertrend(df)
            
            return df
        except Exception as e:
            self.logger.error(f"Erro dados: {e}")
            return None

    # ==========================
    # GESTÃO DE ESTADO
    # ==========================
    def get_state(self):
        conn = sqlite3.connect(self.DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM position_state WHERE id=1')
        row = cursor.fetchone()
        conn.close()
        return dict(row)

    def update_state(self, **kwargs):
        conn = sqlite3.connect(self.DB_NAME)
        cursor = conn.cursor()
        cols = ", ".join([f"{k}=?" for k in kwargs.keys()])
        vals = list(kwargs.values())
        sql = f"UPDATE position_state SET {cols} WHERE id=1"
        cursor.execute(sql, vals)
        conn.commit()
        conn.close()

    # ==========================
    # EXECUÇÃO
    # ==========================
    def execute_buy(self, price, stop_price):
        balance = self.SIM_BALANCE if self.SIMULATION else self.exchange.fetch_balance()['USDT']['free']
        cost = balance * self.RISK_PER_TRADE
        amount = cost / price
        
        amount_final = self.exchange.amount_to_precision(self.SYMBOL, amount)
        price_final = self.exchange.price_to_precision(self.SYMBOL, price)

        if float(amount_final) * float(price_final) < 10:
            self.logger.warning("Saldo insuficiente.")
            return

        if not self.SIMULATION:
            try:
                order = self.exchange.create_market_buy_order(self.SYMBOL, amount_final)
                price_final = float(order.get('average', price_final))
            except Exception as e:
                self.logger.error(f"Erro Compra: {e}")
                return

        self.update_state(
            in_position=1, 
            entry_price=float(price_final), 
            quantity=float(amount_final), 
            stop_loss=stop_price, # O Stop inicial é a linha do SuperTrend
            highest_price=float(price_final),
            entry_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        msg = f"🚀 **COMPRA (SuperTrend)**\nPreço: {price_final}\nStop Inicial: {stop_price:.2f}"
        self.logger.info(msg.replace('*','').replace('\n',' '))
        self.telegram_send(msg)

    def execute_sell(self, price, reason, quantity):
        if not self.SIMULATION:
            try:
                self.exchange.create_market_sell_order(self.SYMBOL, quantity)
            except Exception as e:
                self.logger.error(f"Erro Venda: {e}")
                return

        state = self.get_state()
        profit = (price - state['entry_price']) / state['entry_price'] * 100
        
        self.update_state(in_position=0, entry_price=0.0, quantity=0.0, stop_loss=0.0, highest_price=0.0, entry_time="")
        
        if self.SIMULATION:
            self.SIM_BALANCE += (quantity * price) - (quantity * state['entry_price'])

        emoji = "✅" if profit > 0 else "🔻"
        msg = f"{emoji} **VENDA ({reason})**\nPreço: {price}\nResultado: {profit:.2f}%"
        self.logger.info(msg.replace('*','').replace('\n',' '))
        self.telegram_send(msg)

    # ==========================
    # LOOP PRINCIPAL
    # ==========================
    def run(self):
        self.logger.info("🔥 Bot Trend (SuperTrend + ADX) Iniciado!")
        self.telegram_send("🔥 **BOT TREND V2 (SuperTrend)** Iniciado")
        
        while self.running:
            try:
                df = self.process_data()
                if df is None: 
                    time.sleep(10)
                    continue

                curr = df.iloc[-1]
                prev = df.iloc[-2]
                price = float(curr['close'])
                state = self.get_state()

                # LOG DE MONITORAMENTO (A cada 1 minuto)
                if int(time.time()) % 60 == 0:
                    status = "COMPRADO" if state['in_position'] else "LIQUIDO"
                    trend_str = "ALTA" if curr['In_Uptrend'] else "BAIXA"
                    self.logger.info(f"[{status}] Preço: {price:.2f} | Tendência: {trend_str} | ADX: {curr['ADX']:.2f}")

                # --- MODO COMPRADO ---
                if state['in_position']:
                    # 1. Saída Pelo SuperTrend (Inversão de Tendência)
                    # Se o SuperTrend ficar VERMELHO (False) ou preço cruzar linha
                    if not curr['In_Uptrend']: 
                        self.execute_sell(price, "Inversão de Tendência", state['quantity'])
                    
                    # 2. Saída por Stop Loss (Segurança)
                    elif price < state['stop_loss']:
                         self.execute_sell(price, "Stop Loss Tocado", state['quantity'])
                    
                    # 3. Atualizar Stop Móvel (Trailing)
                    # Se o SuperTrend subir, nós subimos o stop loss para a linha dele
                    elif curr['SuperTrend'] > state['stop_loss']:
                        self.update_state(stop_loss=curr['SuperTrend'])
                        self.logger.info(f"🔒 Stop ajustado para linha SuperTrend: {curr['SuperTrend']:.2f}")

                # --- MODO LÍQUIDO (Buscando Compra) ---
                else:
                    # SINAL DE COMPRA:
                    # 1. SuperTrend virou para ALTA (Candle atual verde, anterior era vermelho OU cruzou)
                    sinal_compra = curr['In_Uptrend'] and not prev['In_Uptrend']
                    
                    # 2. Se já estava verde, mas preço tocou na linha e subiu (Reentrada)
                    # (Simplificado: vamos focar na virada de mão para ser mais seguro)
                    
                    # FILTROS:
                    tendencia_macro = price > curr['EMA_200'] # Opcional: só compra se tiver acima da média de 200
                    forca_tendencia = curr['ADX'] > self.ADX_THRESHOLD

                    if sinal_compra:
                        if forca_tendencia:
                            self.execute_buy(price, curr['SuperTrend'])
                        else:
                            self.logger.info(f"⚠️ Sinal SuperTrend ignorado: ADX fraco ({curr['ADX']:.2f})")

                time.sleep(10)

            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                self.logger.error(f"Loop erro: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = TrendBot()
    bot.run()