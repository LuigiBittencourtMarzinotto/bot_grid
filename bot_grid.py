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
# CLASSE DE GERENCIAMENTO DO GRID V3
# ==========================================

class GridBot:
    def __init__(self):
        self._setup_logging()
        self._load_config()
        self._init_db()
        self._connect_exchange()
        self.logger.info("Inicializando lógica do GRID V3...")
        self.telegram_send("🚀 GRID V3 iniciado.")
        self.logger.info(f"SIMULATION = {self.SIMULATION}")
        self.telegram_send(f"SIMULATION = {self.SIMULATION}")

    # --------------------------------------
    # LOGGING
    # --------------------------------------
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

    # --------------------------------------
    # CONFIG / ENV
    # --------------------------------------
    def _load_config(self):
        # Força carregar .env no mesmo diretório
        load_dotenv(dotenv_path='.env', override=True)

        self.API_KEY = os.getenv('BINANCE_API_KEY')
        self.SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
        self.TG_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

        # Corrigido: default agora é 'false'
        self.SIMULATION = str(os.getenv('MODO_SIMULACAO', 'false')).lower() == 'true'

        # Configurações do Grid (base)
        self.SYMBOL = os.getenv('SYMBOL', 'BTC/USDT')

        # Range base vindo do .env (usado para grid dinâmico)
        self.BASE_LOWER_PRICE = float(os.getenv('GRID_LOWER_PRICE', 50000))
        self.BASE_UPPER_PRICE = float(os.getenv('GRID_UPPER_PRICE', 70000))
        self.GRID_LEVELS = int(os.getenv('GRID_LEVELS', 10))
        self.INVESTMENT_PER_GRID = float(os.getenv('AMOUNT_PER_GRID_USDT', 15))

        # Range e step derivados
        self.RANGE_SIZE = self.BASE_UPPER_PRICE - self.BASE_LOWER_PRICE
        if self.RANGE_SIZE <= 0:
            raise ValueError("GRID_UPPER_PRICE deve ser MAIOR que GRID_LOWER_PRICE no .env")

        self.grid_step = self.RANGE_SIZE / self.GRID_LEVELS

        # Valores que podem ser recalculados dinamicamente
        self.LOWER_PRICE = self.BASE_LOWER_PRICE
        self.UPPER_PRICE = self.BASE_UPPER_PRICE

        # Criar conexão global SQLite
        self.DB_NAME = "grid_data.db"
        self.conn = sqlite3.connect(self.DB_NAME, timeout=15, check_same_thread=False)
        self.cursor = self.conn.cursor()

        # Base e quote do par (ex: BTC / USDT)
        try:
            self.BASE_ASSET, self.QUOTE_ASSET = self.SYMBOL.split('/')
        except ValueError:
            self.logger.error(f"Símbolo inválido: {self.SYMBOL}. Esperado formato BASE/QUOTE (ex: BTC/USDT).")
            sys.exit(1)

    # --------------------------------------
    # EXCHANGE
    # --------------------------------------
    def _connect_exchange(self):
        if not self.API_KEY or not self.SECRET_KEY:
            self.logger.error("Credenciais da API não encontradas. Verifique o .env.")
            sys.exit(1)

        self.exchange = ccxt.binance({
            'apiKey': self.API_KEY,
            'secret': self.SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True,
                'fetchCurrencies': False,
            }
        })

        # Desativa hard o fetch de currencies
        self.exchange.options['fetchCurrencies'] = False

        self.logger.info("Carregando mercados da Binance...")
        self.telegram_send("Carregando mercados da Binance...")

        try:
            self.markets = self.exchange.load_markets()
        except Exception as e:
            self.logger.error(f"Erro ao carregar mercados: {e}")
            self.telegram_send(f"Erro ao carregar mercados: {e}")
            sys.exit(1)

        market = self.markets[self.SYMBOL]

        self.min_amount = market['limits']['amount']['min']
        self.min_cost   = market['limits']['cost']['min']

        if self.min_cost is None:
            self.min_cost = 10

        self.logger.info(f"Conectado! Par: {self.SYMBOL} | Min Cost: {self.min_cost}")
        self.telegram_send(f"Conectado! Par: {self.SYMBOL} | Min Cost: {self.min_cost}")

    # --------------------------------------
    # DB
    # --------------------------------------
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

    # --------------------------------------
    # TELEGRAM
    # --------------------------------------
    def telegram_send(self, message):
        try:
            token = self.TG_TOKEN
            chat_id = self.TG_CHAT_ID
            if not token or not chat_id:
                return
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)
        except Exception as e:
            self.logger.error(f"Erro Telegram: {e}")

    # --------------------------------------
    # GRID CORE
    # --------------------------------------
    def recalc_dynamic_grid(self, current_price: float):
        """
        Centraliza o grid no preço atual, mantendo o RANGE definido no .env
        """
        half_range = self.RANGE_SIZE / 2
        self.LOWER_PRICE = current_price - half_range
        self.UPPER_PRICE = current_price + half_range

        self.logger.info(
            f"GRID dinâmico recalculado: LOWER={self.LOWER_PRICE:.2f}, "
            f"UPPER={self.UPPER_PRICE:.2f}, STEP={self.grid_step:.2f}"
        )
        self.telegram_send(
            f"📊 GRID reajustado\nLOWER={self.LOWER_PRICE:.2f}\nUPPER={self.UPPER_PRICE:.2f}\nSTEP={self.grid_step:.2f}"
        )

    def calculate_grid_lines(self):
        """
        Calcula os preços de cada linha do grid com base em LOWER_PRICE, grid_step
        """
        prices = [self.LOWER_PRICE + (i * self.grid_step) for i in range(self.GRID_LEVELS + 1)]
        return prices

    # --------------------------------------
    # RECUPERAÇÃO RETROATIVA
    # --------------------------------------
    def recover_missing_orders(self):
        """
        Ao reiniciar o bot, verifica ordens FILLED e recria a contraparte que faltar.
        - BUY FILLED sem SELL -> cria SELL (BUY -> SELL)
        - SELL FILLED sem BUY -> cria BUY (SELL -> BUY)
        """
        self.cursor.execute("SELECT id, grid_index, order_id, price, side, amount, status FROM active_grids")
        rows = self.cursor.fetchall()

        if not rows:
            self.logger.info("Nenhum registro em active_grids para recuperar.")
            return

        self.logger.info("Verificando ordens para recuperação retroativa...")
        existing_map = {(r[1], r[4]): r for r in rows}  # (grid_index, side) -> row

        for row in rows:
            _id, grid_index, order_id, price, side, amount, status = row

            if status != 'FILLED':
                continue

            # BUY FILLED deve ter SELL correspondente
            if side == 'BUY':
                target_index = grid_index + 1
                if (target_index, 'SELL') not in existing_map:
                    new_price = price + self.grid_step
                    self.logger.info(
                        f"Recuperando SELL perdida: BUY FILLED id={_id}, "
                        f"grid={grid_index} -> novo grid={target_index}, price={new_price:.2f}"
                    )
                    self.telegram_send(
                        f"♻️ Recuperando SELL perdida\ngrid={grid_index} -> {target_index}\nprice={new_price:.2f}"
                    )
                    self.place_order(new_price, "SELL", target_index)

            # SELL FILLED deve ter BUY correspondente
            elif side == 'SELL':
                target_index = grid_index - 1
                if (target_index, 'BUY') not in existing_map:
                    new_price = price - self.grid_step
                    self.logger.info(
                        f"Recuperando BUY perdida: SELL FILLED id={_id}, "
                        f"grid={grid_index} -> novo grid={target_index}, price={new_price:.2f}"
                    )
                    self.telegram_send(
                        f"♻️ Recuperando BUY perdida\ngrid={grid_index} -> {target_index}\nprice={new_price:.2f}"
                    )
                    self.place_order(new_price, "BUY", target_index)

    # --------------------------------------
    # INICIALIZAÇÃO DO GRID
    # --------------------------------------
    def initialize_grid(self):
        """
        - Primeiro, recupera grid perdido (retroativo)
        - Depois, se não houver OPEN, cria um novo grid dinâmico
        """
        # Recupera ordens faltantes com base nas FILLED
        self.recover_missing_orders()

        # Verifica se já existem ordens OPEN
        self.cursor.execute("SELECT count(*) FROM active_grids WHERE status='OPEN'")
        active_orders = self.cursor.fetchone()[0]

        if active_orders > 0:
            self.logger.info(f"Reiniciando com {active_orders} ordens OPEN existentes. Não criará novo grid.")
            self.telegram_send(f"Reiniciando com {active_orders} ordens OPEN existentes.")
            return

        # Nenhuma ordem open -> novo grid dinâmico
        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        current_price = ticker['last']

        # Recalcula grid em torno do preço atual
        self.recalc_dynamic_grid(current_price)

        grid_prices = self.calculate_grid_lines()

        self.telegram_send(f"🤖 GRID INICIADO\nPreço Atual: {current_price}")

        for i, price in enumerate(grid_prices):
            # Evita linha colada demais no preço atual
            if abs(price - current_price) / current_price < 0.002:
                continue

            if price < current_price:
                self.place_order(price, 'BUY', i)

    # --------------------------------------
    # ORDENS
    # --------------------------------------
    def place_order(self, price, side, grid_index):
        """
        Cria ordem REAL ou SIMULADA + grava no SQLite.
        Agora faz checagem correta de saldo:
        - BUY -> checa saldo da quote (USDT)
        - SELL -> checa saldo da base (BTC)
        """
        amount_base = self.INVESTMENT_PER_GRID / price

        # Ajusta precisões
        amount_final = float(self.exchange.amount_to_precision(self.SYMBOL, amount_base))
        price_final = float(self.exchange.price_to_precision(self.SYMBOL, price))

        if amount_final <= 0:
            self.logger.warning(f"Quantidade calculada inválida para {side} em {price_final}. Ignorando ordem.")
            return

        cost = amount_final * price_final

        # SALDO
        if side == 'BUY':
            free_quote = self.get_free_balance(self.QUOTE_ASSET)
            if free_quote < cost:
                msg = (
                    f"Saldo insuficiente de {self.QUOTE_ASSET}: precisa {cost:.2f}, "
                    f"tem {free_quote:.2f}. Ordem BUY ignorada."
                )
                self.logger.warning(msg)
                self.telegram_send(msg)
                return
        else:  # SELL
            free_base = self.get_free_balance(self.BASE_ASSET)
            if free_base < amount_final:
                msg = (
                    f"Saldo insuficiente de {self.BASE_ASSET}: precisa {amount_final:.8f}, "
                    f"tem {free_base:.8f}. Ordem SELL ignorada."
                )
                self.logger.warning(msg)
                self.telegram_send(msg)
                return

        order_id = f"SIM_{int(time.time()*1000)}"

        if not self.SIMULATION:
            try:
                if side == "BUY":
                    order = self.exchange.create_limit_buy_order(self.SYMBOL, amount_final, price_final)
                else:
                    order = self.exchange.create_limit_sell_order(self.SYMBOL, amount_final, price_final)
                order_id = order["id"]

                self.telegram_send(
                    f"📌 Ordem REAL {side} criada\nPreço: {price_final}\nQtd: {amount_final}"
                )
                self.logger.info(f"Ordem REAL {side} criada: id={order_id}, price={price_final}, amount={amount_final}")
            except Exception as e:
                self.logger.error(f"Erro ao criar ordem real: {e}")
                self.telegram_send(f"Erro ao criar ordem real: {e}")
                return
        else:
            self.logger.info(f"[SIM] Ordem {side} criada em {price_final} (amount={amount_final})")
            self.telegram_send(f"[SIM] Ordem {side} criada em {price_final} (amount={amount_final})")

        # Salva no banco como OPEN
        self.cursor.execute('''
            INSERT INTO active_grids (grid_index, order_id, price, side, amount, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (grid_index, order_id, float(price_final), side, float(amount_final), 'OPEN', datetime.now()))
        self.conn.commit()

    # --------------------------------------
    # VERIFICAÇÃO DE ORDENS
    # --------------------------------------
    def check_orders(self):
        """
        Verifica ordens OPEN e executa lógica de grid:
        - BUY preenchida -> marca FILLED, cria SELL acima
        - SELL preenchida -> marca FILLED, registra lucro, cria BUY abaixo
        """
        self.cursor.execute("SELECT * FROM active_grids WHERE status='OPEN'")
        open_orders = self.cursor.fetchall()

        if not open_orders:
            self.logger.info("Nenhuma ordem OPEN para verificar.")
            return

        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        curr = ticker['last']
        self.logger.info(f"Preço atual {self.SYMBOL}: {curr}")

        for row in open_orders:
            row_id = row[0]
            grid_index = row[1]
            order_id = row[2]
            price = row[3]
            side = row[4]
            amount = row[5]

            filled = (
                (side == 'BUY' and curr <= price) or
                (side == 'SELL' and curr >= price)
            )

            if not filled:
                continue

            # Marca como FILLED
            self.cursor.execute(
                "UPDATE active_grids SET status='FILLED', updated_at=? WHERE id=?",
                (datetime.now(), row_id)
            )
            self.conn.commit()

            self.logger.info(f"Ordem {side} id={order_id} em {price} marcada como FILLED.")
            self.telegram_send(f"✅ Ordem {side} FILLED\nPreço: {price}\nGrid index: {grid_index}")

            if side == "BUY":
                # Cria SELL acima
                new_price = price + self.grid_step
                self.place_order(new_price, "SELL", grid_index + 1)

            else:  # SELL
                # Calcula lucro aproximado de 1 step
                buy_price_est = price - self.grid_step
                profit = (price - buy_price_est) * amount

                self.cursor.execute(
                    "INSERT INTO profits (profit_usdt, timestamp) VALUES (?, ?)",
                    (profit, datetime.now())
                )
                self.conn.commit()

                self.logger.info(f"Lucro registrado: {profit:.4f} USDT.")
                self.telegram_send(f"💰 Lucro: {profit:.4f} USDT")

                # Cria BUY abaixo
                new_price = price - self.grid_step
                self.place_order(new_price, "BUY", grid_index - 1)

    # --------------------------------------
    # SALDOS
    # --------------------------------------
    def get_free_balance(self, asset: str) -> float:
        try:
            balance = self.exchange.fetch_balance()
            return float(balance['free'].get(asset, 0))
        except Exception as e:
            self.logger.error(f"Erro ao obter saldo de {asset}: {e}")
            self.telegram_send(f"Erro ao obter saldo de {asset}: {e}")
            return 0.0

    # --------------------------------------
    # LOOP PRINCIPAL
    # --------------------------------------
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
