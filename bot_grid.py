import ccxt
import time
import sqlite3
import logging
import requests
import os
import sys
from dotenv import load_dotenv
from datetime import datetime
from datetime import datetime, timedelta


# ==========================================
# CLASSE DE GERENCIAMENTO DO GRID V4
# ==========================================

class GridBot:
    def __init__(self):
        self._setup_logging()
        self._load_config()
        self._init_db()
        self._connect_exchange()
        self.logger.info("Inicializando lógica do GRID V4 (lucro real)...")
        self.telegram_send("🚀 GRID V4 iniciado (lucro real habilitado).")
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

        # Default agora é 'false'
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

        # Valores que podem ser recalculados dinamicamente (grid dinâmico)
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
        # Ordens de grid
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

        # Lucro bruto aproximado (compatibilidade com versão antiga)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS profits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profit_usdt REAL,
                timestamp TEXT
            )
        ''')

        # Execuções reais de ordens (BUY/SELL) para montar ciclos
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS filled_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grid_index INTEGER,
                order_id TEXT,
                side TEXT,
                price REAL,
                amount REAL,
                fee REAL,
                fee_currency TEXT,
                timestamp TEXT,
                used_in_cycle INTEGER DEFAULT 0
            )
        ''')

        # Lucro real por ciclo (BUY -> SELL)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS real_profits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                gross_profit REAL,
                net_profit REAL,
                buy_price REAL,
                sell_price REAL,
                amount REAL,
                buy_fee REAL,
                sell_fee REAL,
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
        Respeitando limites de grid_index (0..GRID_LEVELS).
        """
        self.cursor.execute("SELECT id, grid_index, order_id, price, side, amount, status FROM active_grids")
        rows = self.cursor.fetchall()

        if not rows:
            self.logger.info("Nenhum registro em active_grids para recuperar.")
            return

        self.logger.info("Verificando ordens para recuperação retroativa...")
        existing_map = {(r[1], r[4], r[6]): r for r in rows}  # (grid_index, side, status) -> row

        for row in rows:
            _id, grid_index, order_id, price, side, amount, status = row

            if status != 'FILLED':
                continue

            # BUY FILLED deve ter SELL correspondente
            if side == 'BUY':
                target_index = grid_index + 1
                if target_index > self.GRID_LEVELS:
                    continue

                # evita recriar se já tiver SELL OPEN nesse nível
                if (target_index, 'SELL', 'OPEN') not in existing_map:
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
                if target_index < 0:
                    continue

                if (target_index, 'BUY', 'OPEN') not in existing_map:
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
        Cria apenas as próximas compras abaixo do preço atual (1 ou 2 níveis),
        não cria o grid completo como antes.
        """
        # Recupera ordens faltantes
        self.recover_missing_orders()

        # Se já existe alguma ordem OPEN, não cria novas de início
        self.cursor.execute("SELECT count(*) FROM active_grids WHERE status='OPEN'")
        active_orders = self.cursor.fetchone()[0]

        if active_orders > 0:
            self.logger.info("Reiniciando com ordens abertas — não criando novo grid completo.")
            return

        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        current_price = ticker['last']

        # calcula o grid dinâmico
        self.recalc_dynamic_grid(current_price)

        # cria APENAS a próxima BUY (não o grid inteiro)
        next_buy_price = current_price - self.grid_step

        # cria BUY apenas se houver espaço e saldo
        self.place_order(next_buy_price, "BUY", 0)

        # OPCIONAL: criar segunda linha
        second_buy_price = next_buy_price - self.grid_step
        self.place_order(second_buy_price, "BUY", 1)

        self.telegram_send(
            f"🟦 GRID COMPACTO INICIADO\n"
            f"BUY1 = {next_buy_price:.2f}\n"
            f"BUY2 = {second_buy_price:.2f}"
        )

    # --------------------------------------
    # FUNÇÕES AUXILIARES DE ORDERS (REAL)
    # --------------------------------------
    def _fetch_order_safely(self, order_id: str):
        """
        Busca os dados reais da ordem na Binance, com tratamento de erro.
        """
        try:
            order = self.exchange.fetch_order(order_id, self.SYMBOL)
            return order
        except Exception as e:
            self.logger.error(f"Erro ao buscar detalhes da ordem {order_id}: {e}")
            self.telegram_send(f"Erro ao buscar detalhes da ordem {order_id}: {e}")
            return None

    def _extract_exec_info(self, order, fallback_price: float, fallback_amount: float):
        """
        Extrai preço médio, quantidade e taxa de uma ordem da Binance.
        """
        if not order:
            return fallback_price, fallback_amount, 0.0, self.QUOTE_ASSET

        avg_price = float(order.get("average") or fallback_price or 0.0)
        filled = float(order.get("filled") or fallback_amount or 0.0)

        fee_cost = 0.0
        fee_currency = self.QUOTE_ASSET

        fee = order.get("fee")
        fees = order.get("fees")

        if fee:
            try:
                fee_cost = float(fee.get("cost") or 0.0)
                fee_currency = fee.get("currency") or self.QUOTE_ASSET
            except Exception:
                pass
        elif fees:
            try:
                fee_cost = sum(float(f.get("cost") or 0.0) for f in fees)
                if fees and fees[0].get("currency"):
                    fee_currency = fees[0].get("currency")
            except Exception:
                pass

        return avg_price, filled, fee_cost, fee_currency

    # --------------------------------------
    # ORDENS
    # --------------------------------------
    def place_order(self, price, side, grid_index):
        """
        Cria ordem REAL ou SIMULADA + grava no SQLite.
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
        - BUY preenchida -> marca FILLED, registra execução real, cria SELL acima
        - SELL preenchida -> marca FILLED, registra lucro bruto + lucro líquido real, cria BUY abaixo
        """
        self.cursor.execute("SELECT * FROM active_grids WHERE status='OPEN'")
        open_orders = self.cursor.fetchall()

        # Se não há nenhuma ordem OPEN, reconstruir GRID
        if not open_orders:
            self.logger.warning("Nenhuma ordem OPEN ativa. Reconstruindo GRID automaticamente.")
            self.telegram_send("⚠️ Nenhuma ordem OPEN ativa. GRID será reconstruído.")
            self.initialize_grid()
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

            # Busca detalhes reais da ordem (se não estiver em simulação)
            order_info = None
            if not self.SIMULATION:
                order_info = self._fetch_order_safely(order_id)

            exec_price, exec_amount, exec_fee, exec_fee_currency = self._extract_exec_info(
                order_info, price, amount
            )

            # Registra na tabela filled_orders
            self.cursor.execute(
                '''
                INSERT INTO filled_orders
                (grid_index, order_id, side, price, amount, fee, fee_currency, timestamp, used_in_cycle)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                ''',
                (grid_index, order_id, side, exec_price, exec_amount, exec_fee, exec_fee_currency, datetime.now())
            )
            self.conn.commit()

            # Lógica de continuação do grid
            if side == "BUY":
                # Cria SELL acima (próximo nível)
                next_index = grid_index + 1

                if next_index > self.GRID_LEVELS:
                    self.logger.info(
                        f"Limite superior do GRID atingido no nível {grid_index}. "
                        f"Não será criada SELL acima."
                    )
                    self.telegram_send(
                        f"⚠️ Limite superior do GRID atingido.\n"
                        f"Grid index atual: {grid_index}"
                    )
                else:
                    new_price = price + self.grid_step

                    # Verifica se já existe SELL OPEN nesse nível
                    self.cursor.execute("""
                        SELECT id FROM active_grids
                        WHERE grid_index=? AND side='SELL' AND status='OPEN'
                    """, (next_index,))
                    existing_sell = self.cursor.fetchone()

                    if existing_sell:
                        self.logger.info(f"SELL no nível {next_index} já existente. Nenhuma nova SELL criada.")
                        self.telegram_send(f"ℹ️ SELL do grid {next_index} já está ativa.")
                    else:
                        self.logger.info(f"Criando SELL no nível {next_index}, preço {new_price:.2f}")
                        self.telegram_send(
                            f"📈 Próxima SELL criada\n"
                            f"Preço: {new_price:.2f}\nGrid index: {next_index}"
                        )
                        self.place_order(new_price, "SELL", next_index)

            else:  # SELL
                # 1) Lucro aproximado (bruto, compatibilidade antiga)
                buy_price_est = price - self.grid_step
                profit_est = (price - buy_price_est) * amount

                self.cursor.execute(
                    "INSERT INTO profits (profit_usdt, timestamp) VALUES (?, ?)",
                    (profit_est, datetime.now())
                )
                self.conn.commit()

                self.logger.info(f"Lucro BRUTO estimado registrado: {profit_est:.4f} USDT.")
                self.telegram_send(f"💰 Lucro BRUTO estimado: {profit_est:.4f} USDT")

                # 2) Lucro REAL (ciclo BUY -> SELL)
                #   - SELL em grid_index
                #   - BUY correspondente em grid_index - 1
                buy_grid_index = grid_index - 1
                self.cursor.execute(
                    '''
                    SELECT id, price, amount, fee
                    FROM filled_orders
                    WHERE side='BUY'
                      AND used_in_cycle=0
                      AND grid_index=?
                    ORDER BY id ASC
                    LIMIT 1
                    ''',
                    (buy_grid_index,)
                )
                buy_row = self.cursor.fetchone()

                if not buy_row:
                    self.logger.warning(
                        f"Nenhuma BUY disponível para formar ciclo com SELL id={order_id} grid_index={grid_index}."
                    )
                else:
                    buy_id, buy_price_real, buy_amount_real, buy_fee_real = buy_row

                    # Quantidade efetiva = mínimo entre buy e sell (por segurança)
                    qty = min(float(buy_amount_real), float(exec_amount))

                    gross_profit_real = (exec_price - buy_price_real) * qty
                    net_profit_real = gross_profit_real - float(buy_fee_real or 0.0) - float(exec_fee or 0.0)

                    self.cursor.execute(
                        '''
                        INSERT INTO real_profits
                        (order_id, gross_profit, net_profit, buy_price, sell_price,
                         amount, buy_fee, sell_fee, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            order_id,
                            float(gross_profit_real),
                            float(net_profit_real),
                            float(buy_price_real),
                            float(exec_price),
                            float(qty),
                            float(buy_fee_real or 0.0),
                            float(exec_fee or 0.0),
                            datetime.now()
                        )
                    )

                    # Marca BUY como usada no ciclo
                    self.cursor.execute(
                        "UPDATE filled_orders SET used_in_cycle=1 WHERE id=?",
                        (buy_id,)
                    )

                    self.conn.commit()

                    msg = (
                        f"💹 Lucro REAL Grid\n"
                        f"Bruto: {gross_profit_real:.4f} USDT\n"
                        f"Líquido (c/ taxas): {net_profit_real:.4f} USDT\n"
                        f"BUY: {buy_price_real:.2f} | SELL: {exec_price:.2f}\n"
                        f"Qtd: {qty:.6f}"
                    )
                    self.logger.info(msg)
                    self.telegram_send(msg)

                # 3) Cria BUY abaixo para manter o grid (próximo nível)
                next_index = grid_index - 1

                if next_index < 0:
                    self.logger.info(
                        f"Limite inferior do GRID atingido no nível {grid_index}. "
                        f"Não será criada BUY abaixo."
                    )
                    self.telegram_send(
                        f"⚠️ Limite inferior do GRID atingido.\n"
                        f"Grid index atual: {grid_index}"
                    )
                else:
                    new_price = price - self.grid_step

                    # Verifica se já existe BUY OPEN nesse nível
                    self.cursor.execute("""
                        SELECT id FROM active_grids
                        WHERE grid_index=? AND side='BUY' AND status='OPEN'
                    """, (next_index,))
                    existing_buy = self.cursor.fetchone()

                    if existing_buy:
                        self.logger.info(f"BUY no nível {next_index} já existente. Nenhuma nova BUY criada.")
                        self.telegram_send(f"ℹ️ BUY do grid {next_index} já está ativa.")
                    else:
                        self.logger.info(f"Criando BUY no nível {next_index}, preço {new_price:.2f}")
                        self.telegram_send(
                            f"📉 Próxima BUY criada\n"
                            f"Preço: {new_price:.2f}\nGrid index: {next_index}"
                        )
                        self.place_order(new_price, "BUY", next_index)

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

    def cancel_old_open_orders(self, hours=24):
        """
        Cancela todas as ordens OPEN com mais de X horas.
        - Cancela na Binance se for ordem real
        - Remove da tabela active_grids
        - Retorna True se houve cancelamento (para reconstrução do GRID)
        """
        self.cursor.execute("""
            SELECT id, order_id, side, updated_at 
            FROM active_grids 
            WHERE status='OPEN'
        """)
        rows = self.cursor.fetchall()

        if not rows:
            return False

        now = datetime.now()
        expired = []

        for row in rows:
            _id, order_id, side, updated_at = row

            # Converte timestamp salvo
            updated_dt = datetime.fromisoformat(str(updated_at))

            if now - updated_dt > timedelta(hours=hours):
                expired.append((_id, order_id, side))

        if not expired:
            return False

        self.logger.warning(f"{len(expired)} ordens OPEN expiradas ({hours}h). Cancelando...")
        self.telegram_send(f"⚠️ {len(expired)} ordens travadas > {hours}h. Cancelando e reiniciando GRID...")

        for _id, order_id, side in expired:
            # 1. Tenta cancelar na Binance
            if not self.SIMULATION:
                try:
                    self.exchange.cancel_order(order_id, self.SYMBOL)
                    self.logger.info(f"Ordem REAL cancelada na Binance: {order_id}")
                except Exception as e:
                    self.logger.error(f"Erro ao cancelar ordem {order_id} na Binance: {e}")

            # 2. Remove do SQLite
            self.cursor.execute("DELETE FROM active_grids WHERE id=?", (_id,))
            self.conn.commit()

            self.logger.info(f"Ordem removida localmente: {order_id}")

        return True


    # --------------------------------------
    # LOOP PRINCIPAL
    # --------------------------------------
    def run(self):
        self.initialize_grid()
        self.logger.info("Monitorando o Grid...")
        self.telegram_send("Monitorando o Grid...")

        while True:
            try:
                # --- CANCELAMENTO AUTOMÁTICO POR TEMPO ---
                if self.cancel_old_open_orders(hours=24):
                    self.logger.info("Recriando GRID após cancelamento de ordens antigas...")
                    self.initialize_grid()
                    time.sleep(5)
                    continue   # <<< mantém o bot rodando

                # Lógica normal do grid
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
