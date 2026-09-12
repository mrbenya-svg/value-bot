import os
import logging
import asyncio
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask
from threading import Thread

# === НАЛАШТУВАННЯ ТА КЛЮЧІ ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

MIN_EV = 7.0         # Мінімальна перевага +7%
MAX_EV = 20.0        # Максимальна перевага (захист від аномалій)
MIN_ODDS = 1.40      # Мінімальний кф
MAX_ODDS = 2.60      # Максимальний кф

MARKETS = "h2h"      # Тільки основні результати (П1, X, П2)
REGIONS = "eu"       # Тільки європейські БК

ALERT_COOLDOWN = 43200  # 12 годин бана для матчу після відправки сповіщення
sent_alerts = {}

# === ПОВНИЙ СПИСОК ЛІГ (60+) ===
LEAGUES = [
    "soccer_epl",
    "soccer_england_league1",
    "soccer_england_league2",
    "soccer_england_efl_cup",
    "soccer_fa_cup",
    "soccer_spain_la_liga",
    "soccer_spain_segunda_division",
    "soccer_italy_serie_a",
    "soccer_italy_serie_b",
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_germany_3liga",
    "soccer_france_lique_one",
    "soccer_france_lique_two",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_uefa_nations_league",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_belgium_first_div",
    "soccer_turkey_super_league",
    "soccer_scotland_premier_league",
    "soccer_austria_bundesliga",
    "soccer_switzerland_superleague",
    "soccer_denmark_superliga",
    "soccer_norway_eliteserien",
    "soccer_sweden_allsvenskan",
    "soccer_poland_ekstraklasa",
    "soccer_greece_super_league",
    "soccer_czech_republic_first_league",
    "soccer_argentina_primera_division",
    "soccer_brazil_campeonato",
    "soccer_brazil_serie_b",
    "soccer_usa_mls",
    "soccer_mexico_ligamx",
    "soccer_japan_j_league",
    "soccer_korea_k_league_1",
    "soccer_australia_aleague",
    "soccer_chile_camp_national",
    "soccer_colombia_categoria_primera_a",
    "soccer_finland_veikkausliiga",
    "soccer_ireland_a_league",
    "soccer_china_super_league"
]

logging.basicConfig(level=logging.INFO)

app = Flask('')

@app.route('/')
def home():
    return "Bot is running with 48h filter and datetime!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Error sending message: {e}")

def check_value_bets():
    if not ODDS_API_KEY:
        return []

    signals = []
    current_time = time.time()

    # Очищення застарілих алертів із пам'яті
    expired_keys = [k for k, v in sent_alerts.items() if current_time - v > ALERT_COOLDOWN]
    for k in expired_keys:
        del sent_alerts[k]
    
    now_utc = datetime.now(timezone.utc)
    max_time_utc = now_utc + timedelta(hours=48)  # Вікно пошуку: максимум 48 годин вперед

    for league in LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/?apiKey={ODDS_API_KEY}&regions={REGIONS}&markets={MARKETS}"
        
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            data = res.json()
        except Exception as e:
            logging.error(f"Error fetching {league}: {e}")
            continue

        for match in data:
            match_id = match.get('id')

            # 1. Фільтр: якщо матч вже відправляли
            if match_id in sent_alerts:
                continue

            # 2. Фільтр за часом (тільки на найближчі 48 годин)
            commence_time_str = match.get('commence_time')
            if not commence_time_str:
                continue
            
            try:
                match_time_utc = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
                # Якщо матч вже почався або буде пізніше ніж через 48 годин — пропускаємо
                if not (now_utc <= match_time_utc <= max_time_utc):
                    continue
                
                # Конвертація у київський час (UTC+3 для вересня)
                match_time_kyiv = match_time_utc + timedelta(hours=3)
                formatted_time = match_time_kyiv.strftime("%d.%m о %H:%M")
            except Exception as e:
                logging.error(f"Error parsing date {commence_time_str}: {e}")
                continue

            home = match.get('home_team')
            away = match.get('away_team')
            bookmakers = match.get('bookmakers', [])

            market_outcomes = {}

            for bm in bookmakers:
                for mk in bm.get('markets', []):
                    m_key = mk.get('key')
                    for out in mk.get('outcomes', []):
                        name = out.get('name')
                        price = out.get('price')
                        point = out.get('point', '')
                        
                        outcome_key = (m_key, name, point)
                        if outcome_key not in market_outcomes:
                            market_outcomes[outcome_key] = []
                        market_outcomes[outcome_key].append((bm['title'], price))

            best_match_signal = None
            max_ev_found = -100

            # Шукаємо найкращий валуй у матчі
            for (m_key, name, point), prices in market_outcomes.items():
                if len(prices) < 4:
                    continue

                all_odds = [p[1] for p in prices]
                max_price = max(all_odds)
                avg_price = sum(all_odds) / len(all_odds)
                
                fair_prob = 1.0 / avg_price
                ev = (max_price * fair_prob - 1) * 100

                if MIN_EV <= ev <= MAX_EV and MIN_ODDS <= max_price <= MAX_ODDS:
                    if ev > max_ev_found:
                        max_ev_found = ev
                        best_bookies = [p[0] for p in prices if p[1] == max_price]
                        
                        best_match_signal = (
                            f"🎯 <b>VALUE BET FOUND</b>\n\n"
                            f"⚽ <b>Матч:</b> {home} vs {away}\n"
                            f"📅 <b>Час:</b> {formatted_time} (Кв)\n"
                            f"🏆 <b>Ліга:</b> {league}\n"
                            f"📌 <b>Ставка:</b> {name}\n"
                            f"📈 <b>Коефіцієнт:</b> <code>{max_price}</code> (сер. {round(avg_price, 2)})\n"
                            f"🔥 <b>EV:</b> +{round(ev, 2)}%\n"
                            f"🏦 <b>БК:</b> {', '.join(best_bookies)}"
                        )

            # Якщо знайшли варіант — відправляємо 1 сигнал і блокуємо матч
            if best_match_signal:
                signals.append(best_match_signal)
                sent_alerts[match_id] = current_time

    return signals

async def main_loop():
    send_telegram_message("⚙️ <b>Сканер оновлено! Додано час події та фільтр на 48 годин.</b>")
    while True:
        try:
            signals = check_value_bets()
            for sig in signals:
                send_telegram_message(sig)
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
        
        await asyncio.sleep(900)  # Сканування раз на 15 хвилин

if __name__ == '__main__':
    keep_alive()
    asyncio.run(main_loop())
