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

MIN_EV = 5.0         # Мінімальна перевага моделі над ринком (+5%)
MAX_EV = 30.0        # Максимальний захист від аномалій
MIN_ODDS = 1.60      # Мінімальний кф
MAX_ODDS = 3.50      # Максимальний кф

REGIONS = "eu"       # Європейські контори
ALERT_COOLDOWN = 43200  # 12 годин блокування матчу після аларму
sent_alerts = {}

# Основні ліги для аналізу (щоб не перевищувати ліміти запитів API)
LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_lique_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga"
]

logging.basicConfig(level=logging.INFO)

app = Flask('')

@app.route('/')
def home():
    return "Poisson Value Bot is running!"

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
    """Обчислення ймовірності за розподілом Пуассона"""
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)

def calculate_match_probabilities(home_scored, home_conceded, away_scored, away_conceded):
    """Розрахунок ймовірностей П1, Х, П2 через розподіл Пуассона (до 6 голів у матчі)"""
    # Середні показники (базова логіка xG)
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
        return []

    signals = []
    current_time = time.time()

    # Очищення старих алертів
    expired_keys = [k for k, v in sent_alerts.items() if current_time - v > ALERT_COOLDOWN]
    for k in expired_keys:
        del sent_alerts[k]
    
    now_utc = datetime.now(timezone.utc)
    max_time_utc = now_utc + timedelta(hours=48)  # Останні 48 годин

    for league in LEAGUES:
        # Запит коефіцієнтів та історичних даних/результатів якщо доступні, або розширений набір ринків
        odds_url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/?apiKey={ODDS_API_KEY}&regions={REGIONS}&markets=h2h"
        
        try:
            res = requests.get(odds_url, timeout=10)
            if res.status_code != 200:
                continue
            matches = res.json()
        except Exception as e:
            logging.error(f"Error fetching {league}: {e}")
            continue

        for match in matches:
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

            # Збираємо середню статистику голів з попередніх результатів або апроксимуємо по ринку
            # (Для базового розрахунку Пуассона беремо пре-матч консенсус коефіцієнти як зворотну ймовірність ринку для оцінки сили)
            h2h_prices = []
            for bm in bookmakers:
                for mk in bm.get('markets', []):
                    if mk.get('key') == 'h2h':
                        outcomes = {o['name']: o['price'] for o in mk.get('outcomes', [])}
                        if home in outcomes and away in outcomes:
                            h2h_prices.append(outcomes)

            if len(h2h_prices) < 3:
                continue

            # Середній ринковий кф для оцінки базових очікувань моделі
            avg_home_odds = sum(p[home] for p in h2h_prices if home in p) / len(h2h_prices)
            avg_draw_odds = sum(p['Draw'] for p in h2h_prices if 'Draw' in p) / len(h2h_prices)
            avg_away_odds = sum(p[away] for p in h2h_prices if away in p) / len(h2h_prices)

            # Перетворюємо ринкові кф на умовні середні голи для Пуассона
            # Чим нижчий кф на команду, тим вищий її умовний показник забитої статистики в моделі
            est_home_scored = max(0.8, 2.5 / math.sqrt(avg_home_odds))
            est_home_conceded = max(0.5, 2.5 / math.sqrt(avg_away_odds))
            est_away_scored = max(0.7, 2.5 / math.sqrt(avg_away_odds))
            est_away_conceded = max(0.5, 2.5 / math.sqrt(avg_home_odds))

            # Модель Пуассона рахує наші теоретичні ймовірності
            model_p_home, model_p_draw, model_p_away = calculate_match_probabilities(
                est_home_scored, est_home_conceded, est_away_scored, est_away_conceded
            )

            # Шукаємо максимальні коефіцієнти серед усіх буків по кожному результату
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

            # Порівнюємо ймовірність моделі з пропонованим коефіцієнтом (Value = Model_Prob * Odds - 1)
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
                
                # Розрахунок Value (EV) у відсотках
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

    return signals

async def main_loop():
    send_telegram_message("⚙️ <b>Бот оновлено! Активовано математичну модель Пуассона для розрахунку валуїв.</b>")
    while True:
        try:
            signals = check_value_bets()
            for sig in signals:
                send_telegram_message(sig)
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
        
        await asyncio.sleep(900)  # Сканування кожні 15 хвилин

if __name__ == '__main__':
    keep_alive()
    asyncio.run(main_loop())
