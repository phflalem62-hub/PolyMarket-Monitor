"""
Monitor de atividade no Polymarket -> alertas no Telegram
Versão para rodar no GitHub Actions (execução única a cada agendamento,
não fica em loop -- quem cuida da repetição é o cron do GitHub Actions).

Configuração via variáveis de ambiente (definidas como Secrets no GitHub):
- WALLET_ADDRESS
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

O estado (última atividade já notificada) é salvo em state.json,
que este script atualiza e o workflow do GitHub Actions commita de volta
no repositório a cada execução.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

WALLET_ADDRESS = os.environ["WALLET_ADDRESS"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Por quanto tempo (em segundos) o script fica rodando em loop dentro de
# uma única execução do GitHub Actions, e de quanto em quanto tempo checa.
# 270s (4m30s) com checagem a cada 30s garante que ele termina antes do
# próximo agendamento do cron (a cada 5 minutos), sem sobrepor execuções.
LOOP_DURATION_SECONDS = 270
POLL_INTERVAL_SECONDS = 30

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
ACTIVITY_URL = "https://data-api.polymarket.com/activity"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def load_last_seen_timestamp() -> int:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return int(data.get("last_timestamp", 0))
        except (json.JSONDecodeError, ValueError):
            return 0
    return 0


def save_last_seen_timestamp(ts: int) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump({"last_timestamp": ts}, f)


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


def check_once(last_seen_ts: int, first_run: bool) -> int:
    """Faz uma checagem e retorna o novo last_seen_ts."""
    activities = fetch_activity(WALLET_ADDRESS, limit=20)

    if first_run:
        # Primeira execução: só marca o ponto de partida, não manda alertas do passado.
        if activities:
            newest_ts = int(activities[0]["timestamp"])
            save_last_seen_timestamp(newest_ts)
            print(f"Primeira execução: marcando ponto de partida em {newest_ts}.")
            return newest_ts
        return last_seen_ts

    new_activities = [a for a in activities if int(a.get("timestamp", 0)) > last_seen_ts]
    new_activities.reverse()  # manda em ordem cronológica

    for act in new_activities:
        msg = format_message(act)
        send_telegram_message(msg)
        print(f"[OK] Alerta enviado: {act.get('title')} ({act.get('side')})")
        last_seen_ts = max(last_seen_ts, int(act.get("timestamp", 0)))

    if new_activities:
        save_last_seen_timestamp(last_seen_ts)
    else:
        print("Nenhuma atividade nova.")

    return last_seen_ts


def main():
    last_seen_ts = load_last_seen_timestamp()
    first_run = last_seen_ts == 0

    start_time = time.monotonic()
    while True:
        try:
            last_seen_ts = check_once(last_seen_ts, first_run)
            first_run = False
        except requests.RequestException as e:
            print(f"[ERRO] Falha ao consultar a API da Polymarket: {e}")

        elapsed = time.monotonic() - start_time
        if elapsed + POLL_INTERVAL_SECONDS > LOOP_DURATION_SECONDS:
            break
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
