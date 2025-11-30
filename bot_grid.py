import ccxt
import time
import sqlite3
import logging
import requests
import os
import sys
from dotenv import load_dotenv
from datetime import datetime

# ==========================================
# CLASSE DE GERENCIAMENTO DO GRID
# ==========================================

class GridBot:
    def __init__(self):
        self._setup_logging()
        self._load_config()
        self._init_db()
        self._connect_exchange()
        self.market_precision = None
        self.min_amount = None
        self.min_cost = 10

    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler("grid_bot.log"),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger("GridBot")

    def _load_config(self):
        load_dotenv()
        self.API_KEY = os.getenv('BINANCE_API_KEY')
        self.SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
        self.TG_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
        self.SIMULATION = os.getenv('MODO_SIMULACAO', 'true').lower() == 'true'
        
        # Configurações do Grid
        self.SYMBOL = os.getenv('SYMBOL', 'BTC/USDT')
        self.LOWER_PRICE = float(os.getenv('GRID_LOWER_PRICE', 50000))
        self.UPPER_PRICE = float(os.getenv('GRID_UPPER_PRICE', 70000))
        self.GRID_LEVELS = int(os.getenv('GRID_LEVELS', 10))
        self.INVESTMENT_PER_GRID = float(os.getenv('AMOUNT_PER_GRID_USDT', 15))
        
        # Criar conexão global SQLite
        self.DB_NAME = "grid_data.db"
        self.conn = sqlite3.connect(self.DB_NAME, timeout=15, check_same_thread=False)
        self.cursor = self.conn.cursor()

    def _connect_exchange(self):
        if not self.API_KEY or not self.SECRET_KEY:
            self.logger.error("Credenciais da API não encontradas.")
            sys.exit(1)

        self.exchange = ccxt.binance({
            'apiKey': self.API_KEY,
            'secret': self.SECRET_KEY,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        
        self.logger.info("Carregando mercados da Binance...")
        self.telegram_send("Carregando mercados da Binance...")
        self.markets = self.exchange.load_markets()
        
        # Carregar precisões do par para evitar erros de API
        market = self.markets[self.SYMBOL]

        self.min_amount = market['limits']['amount']['min']
        self.min_cost   = market['limits']['cost']['min']

        if self.min_cost is None:
            self.min_cost = 10

        self.logger.info(f"Conectado! Par: {self.SYMBOL} | Min Cost: {self.min_cost}")
        self.telegram_send(f"Conectado! Par: {self.SYMBOL} | Min Cost: {self.min_cost}")

    def _init_db(self):
        """Cria tabelas necessárias"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_grids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grid_index INTEGER,
                order_id TEXT,
                price REAL,
                side TEXT,
                amount REAL,
                status TEXT,
                updated_at TEXT
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS profits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profit_usdt REAL,
                timestamp TEXT
            )
        ''')

        self.conn.commit()

    def telegram_send(self, message):

        try:
            token = os.getenv('TELEGRAM_TOKEN')
            chat_id = os.getenv('TELEGRAM_CHAT_ID')
            url = f"https://api.telegram.org/bot{token}/sendMessage"

            requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)
        except Exception as e:
            self.logger.error(f"Erro Telegram: {e}")

    # ==========================
    # LÓGICA DO GRID
    # ==========================

    def calculate_grid_lines(self):
        step = (self.UPPER_PRICE - self.LOWER_PRICE) / self.GRID_LEVELS
        prices = [self.LOWER_PRICE + (i * step) for i in range(self.GRID_LEVELS + 1)]
        return prices, step

    def initialize_grid(self):
        """Executado somente quando não há ordens ativas"""
        self.cursor.execute("SELECT count(*) FROM active_grids WHERE status='OPEN'")
        active_orders = self.cursor.fetchone()[0]
        
        if active_orders > 0:
            self.logger.info(f"Reiniciando com {active_orders} ordens existentes...")
            self.telegram_send(f"Reiniciando com {active_orders} ordens existentes...")
            return

        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        current_price = ticker['last']

        grid_prices, step = self.calculate_grid_lines()

        self.telegram_send(f"🤖 GRID INICIADO\nPreço Atual: {current_price}")

        for i, price in enumerate(grid_prices):
            if abs(price - current_price) / current_price < 0.002:
                continue

            if price < current_price:
                self.place_order(price, 'BUY', i)

    def place_order(self, price, side, grid_index):
        amount_btc = self.INVESTMENT_PER_GRID / price
        
        amount_final = self.exchange.amount_to_precision(self.SYMBOL, amount_btc)
        price_final = self.exchange.price_to_precision(self.SYMBOL, price)
            
        cost = float(amount_final) * float(price_final)

        # ===============================
        # VALIDAÇÃO DO SALDO NA BINANCE
        # ===============================
        free_usdt = self.get_free_balance_usdt()
        if free_usdt < cost:
            self.logger.warning(f"Saldo insuficiente: precisa {cost:.2f} USDT, mas tem {free_usdt:.2f} USDT. Ordem ignorada.")
            self.telegram_send(f"Saldo insuficiente: precisa {cost:.2f} USDT, mas tem {free_usdt:.2f} USDT. Ordem ignorada.")
            return

        # if cost < self.min_cost:
        #     self.logger.warning(f"Ordem ignorada: valor {cost} menor que mínimo.")
        #     return
        # ===============================

        order_id = f"SIM_{int(time.time()*1000)}"

        if not self.SIMULATION:
            try:
                if side == "BUY":
                    order = self.exchange.create_limit_buy_order(self.SYMBOL, amount_final, price_final)
                else:
                    order = self.exchange.create_limit_sell_order(self.SYMBOL, amount_final, price_final)
                order_id = order["id"]
                
                self.telegram_send(f"📌 Ordem REAL {side} criada\nPreço: {price_final}\nQtd: {amount_final}")
        
            except Exception as e:
                self.logger.error(f"Erro ao criar ordem real: {e}")
                self.telegram_send(f"Erro ao criar ordem real: {e}")
                return
        else:
            self.logger.info(f"[SIM] Ordem {side} criada em {price_final}")
            self.telegram_send(f"[SIM] Ordem {side} criada em {price_final}")
        self.cursor.execute('''
            INSERT INTO active_grids (grid_index, order_id, price, side, amount, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (grid_index, order_id, float(price_final), side, float(amount_final), 'OPEN', datetime.now()))
        self.conn.commit()

    def check_orders(self):
        """Verifica ordens e repõe grid"""
        self.cursor.execute("SELECT * FROM active_grids WHERE status='OPEN'")
        open_orders = self.cursor.fetchall()

        grid_prices, step = self.calculate_grid_lines()

        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        curr = ticker['last']

        for row in open_orders:
            order_id = row[2]
            price = row[3]
            side = row[4]
            amount = row[5]
            grid_index = row[1]

            # SIMULAÇÃO
            filled = (
                (side == 'BUY'  and curr <= price) or
                (side == 'SELL' and curr >= price)
            )

            if filled:
                self.cursor.execute("UPDATE active_grids SET status='FILLED' WHERE id=?", (row[0],))
                self.conn.commit()

                if side == "BUY":
                    new_price = price + step
                    self.place_order(new_price, "SELL", grid_index + 1)

                else:  # SELL
                    new_price = price - step
                    profit = (price - (price - step)) * amount
                    self.cursor.execute(
                        "INSERT INTO profits (profit_usdt, timestamp) VALUES (?, ?)",
                        (profit, datetime.now())
                    )
                    self.conn.commit()

                    self.place_order(new_price, "BUY", grid_index - 1)

    def get_free_balance_usdt(self):
        try:
            balance = self.exchange.fetch_balance()

            return balance['free']['USDT']
        except Exception as e:
            self.logger.error(f"Erro ao obter saldo: {e}")
            self.telegram_send(f"Erro ao obter saldo: {e}")
            return 0

    def run(self):
        self.initialize_grid()
        self.logger.info("Monitorando o Grid...")
        self.telegram_send("Monitorando o Grid...")
        while True:
            try:
                self.check_orders()
                time.sleep(10)
            except Exception as e:
                self.logger.error(f"Erro no loop principal: {e}")
                self.telegram_send(f"Erro no loop principal: {e}")
                time.sleep(5)


# ==========================================
# EXECUÇÃO
# ==========================================
if __name__ == "__main__":
    bot = GridBot()
    bot.run()
