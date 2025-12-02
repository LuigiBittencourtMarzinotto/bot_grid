import sqlite3
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

# ================================
# Carregar variáveis do .env
# ================================
load_dotenv()

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_NAME = "grid_data.db"

# ================================
# Função para enviar mensagem
# ================================
def telegram_send(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Variáveis TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não encontradas.")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        print("Erro ao enviar telegram:", e)


# ================================
# Busca lucro no SQLite
# ================================
def get_total_profit():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(profit_usdt) FROM profits")
    total = cursor.fetchone()[0]
    conn.close()

    return total if total else 0.0


# ================================
# Executa e envia
# ================================
if __name__ == "__main__":
    total_profit = get_total_profit()
    today = datetime.now().strftime("%d/%m/%Y")

    message = (
        f"📊 *Relatório Diário — {today}*\n\n"
        f"💰 *Lucro acumulado:* {total_profit:.4f} USDT\n"
    )

    telegram_send(message)
    print("Relatório enviado com sucesso!")
