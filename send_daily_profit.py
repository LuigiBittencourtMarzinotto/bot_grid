import os
import sqlite3
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

# ======================================================
# CONFIG / ENV
# ======================================================

load_dotenv(dotenv_path=".env", override=True)

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_NAME = "grid_data.db"


# ======================================================
# TELEGRAM
# ======================================================

def telegram_send(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados no .env")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={
                "chat_id": TG_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
        print("Mensagem enviada ao Telegram.")
    except Exception as e:
        print("Erro ao enviar mensagem para o Telegram:", e)


# ======================================================
# DB HELPERS
# ======================================================

def open_conn():
    return sqlite3.connect(DB_NAME, timeout=15, check_same_thread=False)


def ensure_real_profits_table(conn: sqlite3.Connection):
    """
    Garante que a tabela real_profits exista.
    Mesmo que o bot ainda não esteja preenchendo, evita erro no SELECT.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS real_profits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   TEXT,
            gross_profit REAL,
            net_profit   REAL,
            buy_price    REAL,
            sell_price   REAL,
            amount       REAL,
            buy_fee      REAL,
            sell_fee     REAL,
            timestamp    TEXT
        )
        """
    )
    conn.commit()


# ======================================================
# CÁLCULOS DE LUCRO
# ======================================================

def get_gross_stats(conn: sqlite3.Connection, since: datetime):
    """
    Usa a tabela 'profits' para lucro bruto.
    """
    cursor = conn.cursor()

    # Total bruto histórico
    cursor.execute("""
        SELECT 
            COALESCE(SUM(profit_usdt), 0.0) AS total_profit,
            COUNT(*) AS total_trades
        FROM profits
    """)
    total_profit, total_trades = cursor.fetchone()

    # Bruto últimas 24h
    cursor.execute("""
        SELECT 
            COALESCE(SUM(profit_usdt), 0.0) AS profit_24h,
            COUNT(*) AS trades_24h
        FROM profits
        WHERE timestamp >= ?
    """, (since.strftime("%Y-%m-%d %H:%M:%S"),))
    profit_24h, trades_24h = cursor.fetchone()

    return {
        "total_profit": float(total_profit or 0.0),
        "total_trades": int(total_trades or 0),
        "profit_24h": float(profit_24h or 0.0),
        "trades_24h": int(trades_24h or 0),
    }


def get_net_stats(conn: sqlite3.Connection, since: datetime):
    """
    Usa a tabela 'real_profits' para lucro líquido REAL (com taxas).
    Se ainda não tiver dados, tudo vem 0.
    """
    cursor = conn.cursor()

    # Total histórico
    cursor.execute("""
        SELECT 
            COALESCE(SUM(gross_profit), 0.0) AS total_gross,
            COALESCE(SUM(net_profit),   0.0) AS total_net,
            COUNT(*) AS total_trades
        FROM real_profits
    """)
    total_gross, total_net, total_trades = cursor.fetchone()

    # Últimas 24h
    cursor.execute("""
        SELECT 
            COALESCE(SUM(gross_profit), 0.0) AS gross_24h,
            COALESCE(SUM(net_profit),   0.0) AS net_24h,
            COUNT(*) AS trades_24h
        FROM real_profits
        WHERE timestamp >= ?
    """, (since.strftime("%Y-%m-%d %H:%M:%S"),))
    gross_24h, net_24h, trades_24h = cursor.fetchone()

    # Melhor / pior trade (por net_profit)
    cursor.execute("""
        SELECT 
            COALESCE(MAX(net_profit), 0.0) AS best,
            COALESCE(MIN(net_profit), 0.0) AS worst
        FROM real_profits
    """)
    best_trade, worst_trade = cursor.fetchone()

    # Média por trade (líquido)
    avg_net = 0.0
    if total_trades and total_trades > 0:
        avg_net = float(total_net) / int(total_trades)

    return {
        "total_gross": float(total_gross or 0.0),
        "total_net": float(total_net or 0.0),
        "total_trades": int(total_trades or 0),
        "gross_24h": float(gross_24h or 0.0),
        "net_24h": float(net_24h or 0.0),
        "trades_24h": int(trades_24h or 0),
        "best_trade": float(best_trade or 0.0),
        "worst_trade": float(worst_trade or 0.0),
        "avg_net": float(avg_net or 0.0),
    }


# ======================================================
# MONTAR RELATÓRIO
# ======================================================

def build_report():
    now = datetime.now()
    since_24h = now - timedelta(days=1)

    try:
        conn = open_conn()
    except Exception as e:
        return f"⚠️ Erro ao conectar no banco `{DB_NAME}`:\n{e}"

    try:
        ensure_real_profits_table(conn)

        gross = get_gross_stats(conn, since_24h)
        net = get_net_stats(conn, since_24h)

    except Exception as e:
        conn.close()
        return f"⚠️ Erro ao consultar dados de lucro:\n{e}"

    conn.close()

    date_str = now.strftime("%d/%m/%Y %H:%M")

    msg_lines = []

    msg_lines.append(f"📊 *Relatório Diário do Grid*")
    msg_lines.append(f"🕒 _Gerado em {date_str}_")
    msg_lines.append("")

    # ---------------- BRUTO ----------------
    msg_lines.append("🔹 *Lucro BRUTO (sem taxas)*")
    msg_lines.append(f"• Total histórico: `{gross['total_profit']:.4f}` USDT em `{gross['total_trades']}` trades")
    msg_lines.append(f"• Últimas 24h: `{gross['profit_24h']:.4f}` USDT em `{gross['trades_24h']}` trades")
    msg_lines.append("")

    # ---------------- LÍQUIDO ----------------
    msg_lines.append("🟢 *Lucro LÍQUIDO REAL (com taxas)*")
    msg_lines.append(f"• Total bruto com taxas: `{net['total_gross']:.4f}` USDT")
    msg_lines.append(f"• Total líquido: `{net['total_net']:.4f}` USDT em `{net['total_trades']}` trades")
    msg_lines.append(f"• Últimas 24h (bruto): `{net['gross_24h']:.4f}` USDT")
    msg_lines.append(f"• Últimas 24h (líquido): `{net['net_24h']:.4f}` USDT em `{net['trades_24h']}` trades")
    msg_lines.append(f"• Lucro médio por trade (líquido): `{net['avg_net']:.4f}` USDT")
    msg_lines.append(f"• Melhor trade (líquido): `{net['best_trade']:.4f}` USDT")
    msg_lines.append(f"• Pior trade (líquido): `{net['worst_trade']:.4f}` USDT")
    msg_lines.append("")

    # Aviso se ainda não houver dados líquidos reais
    if net["total_trades"] == 0:
        msg_lines.append(
            "ℹ️ *Observação:*\n"
            "Ainda não há registros na tabela `real_profits`.\n"
            "Configure o bot para gravar lucro líquido REAL (com taxas) "
            "para este bloco ficar completo."
        )

    return "\n".join(msg_lines)


# ======================================================
# MAIN
# ======================================================

if __name__ == "__main__":
    report = build_report()
    print("=== RELATÓRIO ===")
    print(report)
    telegram_send(report)
