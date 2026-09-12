import os
import logging
import asyncio
import time
import math
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask
from threading import Thread

# === НАЛАШТУВАННЯ ТА КЛЮЧІ ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

MIN_EV = 5.0         
MAX_EV = 30.0        
MIN_ODDS = 1.60      
MAX_ODDS = 3.60      

REGIONS = "eu"       
ALERT_COOLDOWN = 43200  # 12 годин блокування матчу після аларму
sent_alerts = {}

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
      "soccer_austria_bundesliga",
    "soccer_switzerland_superleague",
    "soccer_denmark_superliga",
    "soccer_norway_eliteserien",
    "soccer_greece_super_league",
    "soccer_czech_republic_first_league",
    "soccer_argentina_primera_division",
    "soccer_brazil_campeonato"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask('')

@app.route('/')
def home():
    return "Poisson Value Bot with /scan command is running!"

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

def poisson_prob(lmbda, k):
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)

def calculate_match_probabilities(home_scored, home_conceded, away_scored, away_conceded):
    home_lambda = max(0.5, (home_scored + away_conceded) / 2)
    away_lambda = max(0.5, (away_scored + home_conceded) / 2)
    
    p_home = 0
    p_draw = 0
    p_away = 0
    
    for h in range(7):
        for a in range(7):
            p = poisson_prob(home_lambda, h) * poisson_prob(away_lambda, a)
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
                
    total = p_home + p_draw + p_away
    if total > 0:
        return p_home / total, p_draw / total, p_away / total
    return 0.33, 0.33, 0.33

def check_value_bets():
    if not ODDS_API_KEY:
        logging.error("ODDS_API_KEY is missing!")
        return []

    logging.info("Початок циклу сканування ринку (модель Пуассона)...")
    signals = []
    current_time = time.time()

    expired_keys = [k for k, v in sent_alerts.items() if current_time - v > ALERT_COOLDOWN]
    for k in expired_keys:
        del sent_alerts[k]
    
    now_utc = datetime.now(timezone.utc)
    max_time_utc = now_utc + timedelta(hours=48)

    checked_matches_count = 0

    for league in LEAGUES:
        odds_url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/?apiKey={ODDS_API_KEY}&regions={REGIONS}&markets=h2h"
        
        try:
            res = requests.get(odds_url, timeout=10)
            if res.status_code != 200:
                logging.warning(f"Ліга {league} повернула статус {res.status_code}")
                continue
            matches = res.json()
        except Exception as e:
            logging.error(f"Error fetching {league}: {e}")
            continue

        for match in matches:
            checked_matches_count += 1
            match_id = match.get('id')

            if match_id in sent_alerts:
                continue

            commence_time_str = match.get('commence_time')
            if not commence_time_str:
                continue
            
            try:
                match_time_utc = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
                if not (now_utc <= match_time_utc <= max_time_utc):
                    continue
                
                match_time_kyiv = match_time_utc + timedelta(hours=3)
                formatted_time = match_time_kyiv.strftime("%d.%m о %H:%M")
            except Exception:
                continue

            home = match.get('home_team')
            away = match.get('away_team')
            bookmakers = match.get('bookmakers', [])
            if not bookmakers:
                continue

            h2h_prices = []
            for bm in bookmakers:
                for mk in bm.get('markets', []):
                    if mk.get('key') == 'h2h':
                        outcomes = {o['name']: o['price'] for o in mk.get('outcomes', [])}
                        if home in outcomes and away in outcomes:
                            h2h_prices.append(outcomes)

            if len(h2h_prices) < 3:
                continue

            avg_home_odds = sum(p[home] for p in h2h_prices if home in p) / len(h2h_prices)
            avg_draw_odds = sum(p['Draw'] for p in h2h_prices if 'Draw' in p) / len(h2h_prices)
            avg_away_odds = sum(p[away] for p in h2h_prices if away in p) / len(h2h_prices)

            est_home_scored = max(0.8, 2.5 / math.sqrt(avg_home_odds))
            est_home_conceded = max(0.5, 2.5 / math.sqrt(avg_away_odds))
            est_away_scored = max(0.7, 2.5 / math.sqrt(avg_away_odds))
            est_away_conceded = max(0.5, 2.5 / math.sqrt(avg_home_odds))

            model_p_home, model_p_draw, model_p_away = calculate_match_probabilities(
                est_home_scored, est_home_conceded, est_away_scored, est_away_conceded
            )

            market_best = {
                home: {"price": 0, "bookie": ""},
                "Draw": {"price": 0, "bookie": ""},
                away: {"price": 0, "bookie": ""}
            }

            for bm in bookmakers:
                b_name = bm.get('title')
                for mk in bm.get('markets', []):
                    if mk.get('key') == 'h2h':
                        for out in mk.get('outcomes', []):
                            name = out.get('name')
                            price = out.get('price')
                            if name in market_best and price > market_best[name]["price"]:
                                market_best[name] = {"price": price, "bookie": b_name}

            best_match_signal = None
            max_ev_found = -100

            evaluations = [
                (home, model_p_home, market_best[home]),
                ("Draw", model_p_draw, market_best["Draw"]),
                (away, model_p_away, market_best[away])
            ]

            for outcome_name, model_prob, best_data in evaluations:
                price = best_data["price"]
                if price < MIN_ODDS or price > MAX_ODDS:
                    continue
                
                ev = (model_prob * price - 1) * 100

                if MIN_EV <= ev <= MAX_EV and ev > max_ev_found:
                    max_ev_found = ev
                    best_match_signal = (
                        f"🎯 <b>POISSON VALUE BET</b>\n\n"
                        f"⚽ <b>Матч:</b> {home} vs {away}\n"
                        f"📅 <b>Час:</b> {formatted_time} (Кв)\n"
                        f"🏆 <b>Ліга:</b> {league}\n"
                        f"📌 <b>Ставка:</b> {outcome_name}\n"
                        f"📈 <b>Коефіцієнт:</b> <code>{price}</code> ({best_data['bookie']})\n"
                        f"🧠 <b>Ймовірність моделі:</b> {round(model_prob * 100, 1)}%\n"
                        f"🔥 <b>EV (Перевага):</b> +{round(ev, 2)}%"
                    )

            if best_match_signal:
                signals.append(best_match_signal)
                sent_alerts[match_id] = current_time

    logging.info(f"Сканування завершено. Перевірено матчів у вікні 48г: {checked_matches_count}. Знайдено валуїв: {len(signals)}")
    return signals

# Функція для перевірки команд у Telegram (працює через Telegram GetUpdates API)
def poll_telegram_commands():
    last_update_id = 0
    while True:
        try:
            if not TELEGRAM_BOT_TOKEN:
                time.sleep(10)
                continue
                
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            res = requests.get(url, timeout=35)
            if res.status_code == 200:
                data = res.json()
                for update in data.get('result', []):
                    last_update_id = update['update_id'] + 1
                    message = update.get('message', {})
                    text = message.get('text', '')
                    
                    if text.startswith('/scan') or text.startswith('/check'):
                        send_telegram_message("🔄 <b>Отримано команду /scan. Запускаю примусову перевірку ринку...</b>")
                        signals = check_value_bets()
                        if signals:
                            for sig in signals:
                                send_telegram_message(sig)
                        else:
                            send_telegram_message("✅ Перевірку завершено. Наразі матчів із критеріями валую у найближчі 48 годин не знайдено.")
        except Exception as e:
            logging.error(f"Error in telegram poller: {e}")
        time.sleep(2)

async def main_loop():
    send_telegram_message("⚙️ <b>Бот активований! Напиши мені /scan у чат для примусової перевірки.</b>")
    
    # Запускаємо слухача команд у фоновому потоці
    t_commands = Thread(target=poll_telegram_commands)
    t_commands.daemon = True
    t_commands.start()

    while True:
        try:
            signals = check_value_bets()
            for sig in signals:
                send_telegram_message(sig)
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
        
        await asyncio.sleep(900)  # Автоматичне сканування кожні 15 хв

if __name__ == '__main__':
    keep_alive()
    asyncio.run(main_loop())
