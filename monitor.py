"""
Monitor de atividade no Polymarket -> alertas no Telegram
Versão para rodar no GitHub Actions, disparada por um cron externo
(cron-job.org) a cada 1 minuto. Cada execução faz UMA única checagem
e encerra -- quem cuida da frequência é o cron externo, não um loop
interno (evita execuções sobrepostas).

Configuração via variáveis de ambiente (definidas como Secrets no GitHub):
- WALLET_ADDRESS
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

O estado (último horário visto + IDs recentes já notificados) é salvo em
state.json, que este script atualiza e o workflow do GitHub Actions commita
de volta no repositório a cada execução.

Proteção contra duplicatas: além de comparar por horário, cada trade tem um
identificador único (transactionHash + detalhes do trade) guardado numa lista
dos últimos vistos. Mesmo que a mesma atividade apareça de novo numa consulta
seguinte, ela não é reenviada.
"""

import json
import os
from datetime import datetime, timezone

import requests

WALLET_ADDRESS = os.environ["WALLET_ADDRESS"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Quantos IDs de trades recentes guardar para checagem de duplicata.
MAX_SEEN_IDS = 300

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
ACTIVITY_URL = "https://data-api.polymarket.com/activity"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {
                    "last_timestamp": int(data.get("last_timestamp", 0)),
                    "seen_ids": list(data.get("seen_ids", [])),
                }
        except (json.JSONDecodeError, ValueError):
            pass
    return {"last_timestamp": 0, "seen_ids": []}


def save_state(last_timestamp: int, seen_ids: list) -> None:
    trimmed = seen_ids[-MAX_SEEN_IDS:]
    with open(STATE_FILE, "w") as f:
        json.dump({"last_timestamp": last_timestamp, "seen_ids": trimmed}, f)


def activity_unique_id(activity: dict) -> str:
    tx_hash = activity.get("transactionHash")
    if tx_hash:
        return "-".join(str(activity.get(k, "")) for k in
                         ["transactionHash", "asset", "side", "size", "price", "timestamp"])
    return "-".join(str(activity.get(k, "")) for k in
                     ["timestamp", "asset", "side", "size", "price", "outcome"])


def fetch_activity(wallet: str, limit: int = 20):
    params = {
        "user": wallet,
        "limit": limit,
        "type": "TRADE",
        "sortBy": "TIMESTAMP",
    }
    resp = requests.get(ACTIVITY_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def format_message(activity: dict) -> str:
    title = activity.get("title") or activity.get("slug", "Mercado desconhecido")
    outcome = activity.get("outcome", "?")
    side = activity.get("side", "?")
    price = activity.get("price")
    size = activity.get("size")
    usdc = activity.get("usdcSize")
    ts = activity.get("timestamp")

    dt_str = ""
    if ts:
        dt_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    lines = [
        "🔔 <b>Nova entrada no Polymarket</b>",
        f"📊 {title}",
        f"↳ Outcome: <b>{outcome}</b> | Lado: <b>{side}</b>",
    ]
    if price is not None:
        lines.append(f"↳ Preço: {price}")
    if size is not None:
        lines.append(f"↳ Tamanho: {size}")
    if usdc is not None:
        lines.append(f"↳ Valor: ${usdc}")
    if dt_str:
        lines.append(f"🕒 {dt_str}")

    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    url = TELEGRAM_SEND_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[ERRO] Falha ao enviar mensagem no Telegram: {resp.status_code} {resp.text}")


def main():
    state = load_state()
    last_seen_ts = state["last_timestamp"]
    seen_ids = state["seen_ids"]
    first_run = last_seen_ts == 0

    activities = fetch_activity(WALLET_ADDRESS, limit=20)

    if first_run:
        if activities:
            newest_ts = int(activities[0]["timestamp"])
            new_seen_ids = (seen_ids + [activity_unique_id(a) for a in activities])[-MAX_SEEN_IDS:]
            save_state(newest_ts, new_seen_ids)
            print(f"Primeira execução: marcando ponto de partida em {newest_ts}.")
        return

    candidates = [a for a in activities if int(a.get("timestamp", 0)) >= last_seen_ts]
    candidates.reverse()  # ordem cronológica

    new_activities = []
    for act in candidates:
        uid = activity_unique_id(act)
        if uid not in seen_ids:
            new_activities.append((uid, act))

    for uid, act in new_activities:
        msg = format_message(act)
        send_telegram_message(msg)
        print(f"[OK] Alerta enviado: {act.get('title')} ({act.get('side')})")
        seen_ids.append(uid)
        last_seen_ts = max(last_seen_ts, int(act.get("timestamp", 0)))

    if new_activities:
        save_state(last_seen_ts, seen_ids)
    else:
        print("Nenhuma atividade nova.")


if __name__ == "__main__":
    main()
