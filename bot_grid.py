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
        self.min_cost = None

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
        
        self.DB_NAME = "grid_data.db"

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
        self.markets = self.exchange.load_markets()
        
        # Carregar precisões do par para evitar erros de API
        market = self.markets[self.SYMBOL]
        self.min_amount = market['limits']['amount']['min']
        self.min_cost = market['limits']['cost']['min']
        self.logger.info(f"Conectado! Par: {self.SYMBOL} | Min Cost: {self.min_cost}")

    def _init_db(self):
        """Cria tabela para rastrear as ordens ativas do Grid"""
        conn = sqlite3.connect(self.DB_NAME)
        cursor = conn.cursor()
        
        # Tabela de Grids: Rastreia cada linha
        # status: 'OPEN' (Esperando execução), 'FILLED' (Executada, aguardando oposta)
        # side: 'BUY' ou 'SELL'
        cursor.execute('''
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
        
        # Tabela de Histórico de Lucros
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS profits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profit_usdt REAL,
                timestamp TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def telegram_send(self, message):
        if not self.TG_TOKEN or not self.TG_CHAT_ID: return
        try:
            url = f"https://api.telegram.org/bot{self.TG_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": self.TG_CHAT_ID, "text": message}, timeout=5)
        except Exception as e:
            self.logger.error(f"Erro Telegram: {e}")

    # ==========================
    # LÓGICA DO GRID
    # ==========================

    def calculate_grid_lines(self):
        """Gera os preços das linhas do Grid (Aritmético)"""
        step = (self.UPPER_PRICE - self.LOWER_PRICE) / self.GRID_LEVELS
        prices = [self.LOWER_PRICE + (i * step) for i in range(self.GRID_LEVELS + 1)]
        return prices, step

    def initialize_grid(self):
        """
        Executado apenas na primeira vez. Verifica onde o preço está 
        e coloca ordens de COMPRA abaixo e VENDA acima (se tiver saldo).
        """
        conn = sqlite3.connect(self.DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM active_grids WHERE status='OPEN'")
        active_orders = cursor.fetchone()[0]
        
        if active_orders > 0:
            self.logger.info(f"Reiniciando com {active_orders} ordens já ativas no banco de dados.")
            conn.close()
            return

        self.logger.info("⚡ Iniciando NOVO Grid...")
        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        current_price = ticker['last']
        
        grid_prices, step = self.calculate_grid_lines()
        
        self.telegram_send(f"🤖 **GRID BOT INICIADO**\nPreço Atual: {current_price}\nFaixa: {self.LOWER_PRICE} - {self.UPPER_PRICE}\nGrids: {self.GRID_LEVELS}")

        for i, price in enumerate(grid_prices):
            # Não coloca ordem muito perto do preço atual (evita execução imediata indesejada na criação)
            if abs(price - current_price) / current_price < 0.002:
                continue

            if price < current_price:
                # Abaixo do preço atual -> Colocar Ordem de COMPRA
                self.place_order(price, 'BUY', i)
            
            # Nota: Em um grid "neutro", você precisaria ter BTC para colocar as ordens de VENDA acima.
            # Este bot assume que começamos em USDT, então ele só coloca COMPRAS inicialmente.
            # As VENDAS só são criadas depois que uma compra é executada.
        
        conn.close()

    def place_order(self, price, side, grid_index):
        """Calcula precisão e envia ordem para Binance"""
        amount_btc = self.INVESTMENT_PER_GRID / price
        
        # Ajustes de precisão da Binance
        amount_final = self.exchange.amount_to_precision(self.SYMBOL, amount_btc)
        price_final = self.exchange.price_to_precision(self.SYMBOL, price)
        
        # Validação de Custo Mínimo ($10 normalmente)
        cost = float(amount_final) * float(price_final)
        if cost < self.min_cost:
            self.logger.warning(f"Ordem ignorada: Valor ${cost:.2f} menor que o mínimo da exchange.")
            return

        order_id = "SIM_" + str(int(time.time()*1000))
        
        if not self.SIMULATION:
            try:
                if side == 'BUY':
                    order = self.exchange.create_limit_buy_order(self.SYMBOL, amount_final, price_final)
                else:
                    order = self.exchange.create_limit_sell_order(self.SYMBOL, amount_final, price_final)
                order_id = order['id']
                self.logger.info(f"✅ Ordem {side} criada em ${price_final} | ID: {order_id}")
            except Exception as e:
                self.logger.error(f"Erro ao criar ordem na Binance: {e}")
                return
        else:
            self.logger.info(f"📢 [SIMULADO] Ordem {side} colocada em ${price_final}")

        # Salvar no Banco
        conn = sqlite3.connect(self.DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO active_grids (grid_index, order_id, price, side, amount, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (grid_index, order_id, float(price_final), side, float(amount_final), 'OPEN', datetime.now()))
        conn.commit()
        conn.close()

    def check_orders(self):
        """Loop principal: Verifica status das ordens e repõe o grid"""
        conn = sqlite3.connect(self.DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Buscar todas ordens ABERTAS
        cursor.execute("SELECT * FROM active_grids WHERE status='OPEN'")
        open_orders = cursor.fetchall()
        
        grid_prices, step = self.calculate_grid_lines()

        for row in open_orders:
            order_id = row['order_id']
            side = row['side']
            grid_index = row['grid_index']
            price = row['price']
            amount = row['amount']
            
            is_filled = False
            
            if self.SIMULATION:
                # Na simulação, verificamos o preço atual
                ticker = self.exchange.fetch_ticker(self.SYMBOL)
                curr = ticker['last']
                if side == 'BUY' and curr <= price: is_filled = True
                if side == 'SELL' and curr >= price: is_filled = True
            else:
                try:
                    order_info = self.exchange.fetch_order(order_id, self.SYMBOL)
                    if order_info['status'] == 'closed' or order_info['status'] == 'filled':
                        is_filled = True
                except Exception as e:
                    self.logger.error(f"Erro checando ordem {order_id}: {e}")
                    continue

            if is_filled:
                self.logger.info(f"💰 Ordem {side} EXECULTADA em ${price}!")
                
                # 1. Marcar como FILLED no DB
                cursor.execute("UPDATE active_grids SET status='FILLED' WHERE id=?", (row['id'],))
                
                # 2. Criar a ordem OPOSTA (A mágica do Grid)
                if side == 'BUY':
                    # Comprou barato, agora coloca venda no grid de cima
                    new_price = price + step
                    self.telegram_send(f"🔵 **COMPRA Executada** a ${price}\nArmando venda em ${new_price:.2f}")
                    self.place_order(new_price, 'SELL', grid_index + 1)
                    
                elif side == 'SELL':
                    # Vendeu caro, agora coloca recompra no grid de baixo
                    new_price = price - step
                    profit = (price - (price - step)) * amount # Lucro bruto aproximado
                    
                    self.logger.info(f"💵 LUCRO REALIZADO: +${profit:.2f} USDT")
                    self.telegram_send(f"🟢 **VENDA (LUCRO)** a ${price}\nGanho: ${profit:.2f}\nRearmando compra em ${new_price:.2f}")
                    
                    # Registrar lucro
                    cursor.execute("INSERT INTO profits (profit_usdt, timestamp) VALUES (?, ?)", (profit, datetime.now()))
                    
                    # Repor a grade (comprar de volta mais barato)
                    self.place_order(new_price, 'BUY', grid_index - 1)

        conn.commit()
        conn.close()

    def run(self):
        self.initialize_grid()
        self.logger.info("Monitorando o Grid...")
        while True:
            try:
                self.check_orders()
                time.sleep(10) # Verifica a cada 10 segundos
            except KeyboardInterrupt:
                self.logger.info("Parando bot...")
                break
            except Exception as e:
                self.logger.error(f"Erro no loop principal: {e}")
                time.sleep(5)

# ==========================================
# EXECUÇÃO
# ==========================================
if __name__ == "__main__":
    bot = GridBot()
    bot.run()