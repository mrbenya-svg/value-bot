import os
import logging
import math
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask
from threading import Thread
import telebot

# === НАЛАШТУВАННЯ ТА КЛЮЧІ ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN is missing!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

MIN_EV = 5.0         # Мінімальна перевага +5%
MAX_EV = 25.0        # Максимальна перевага
MIN_ODDS = 1.60      # Мінімальний кф
MAX_ODDS = 3.50      # Максимальний кф

MARKETS = "h2h"      # Основні результати (П1, X, П2)
REGIONS = "eu"       # Європейські БК

# === СПИСОК ЛІГ (з Чемпіоншипом) ===
LEAGUES = [
    "soccer_epl",
    "soccer_england_championship",
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
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_uefa_nations_league",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_belgium_first_div",
    "soccer_turkey_super_league",
    "soccer_austria_bundesliga",
]

logging.basicConfig(level=logging.INFO)
app = Flask('')

@app.route('/')
def home():
    return "Poisson Manual Value Bot is running!"

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
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error sending message: {e}")

# === МАТЕМАТИЧНА МОДЕЛЬ ПУАССОНА ТА xG ===

def poisson_prob(lmbda, k):
    """Формула розподілу Пуассона для k голів при очікуваних lmbda"""
    if lmbda < 0:
        return 0
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_match_probabilities(xg_home, xg_away):
    """Розрахунок ймовірностей П1, Х, П2 через матрицю Пуассона на базі xG"""
    max_goals = 6
    p_home = 0.0
    p_draw = 0.0
    p_away = 0.0

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            prob = poisson_prob(xg_home, h) * poisson_prob(xg_away, a)
            if h > a:
                p_home += prob
            elif h == a:
                p_draw += prob
            else:
                p_away += prob

    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total
        p_draw /= total
        p_away /= total

    return p_home, p_draw, p_away

def estimate_xg_from_market(avg_home_odds, avg_away_odds):
    """Конвертація ринкового консенсусу в базові xG"""
    base_goals = 2.75 
    implied_home_prob = 1.0 / avg_home_odds if avg_home_odds > 0 else 0.33
    implied_away_prob = 1.0 / avg_away_odds if avg_away_odds > 0 else 0.33
    
    total_implied = implied_home_prob + implied_away_prob
    if total_implied == 0:
        return 1.4, 1.1

    xg_home = (implied_home_prob / total_implied) * base_goals * 1.15
    xg_away = (implied_away_prob / total_implied) * base_goals * 0.85
    
    return max(0.5, round(xg_home, 2)), max(0.4, round(xg_away, 2))

# === ЛОГІКА СКАНУВАННЯ ЗА ЗАПИТОМ ===

def run_manual_scan():
    if not ODDS_API_KEY:
        send_telegram_message("❌ <b>Помилка:</b> Відсутній ODDS_API_KEY!")
        return

    send_telegram_message("🔍 <b>Запущено ручне сканування ліг (Poisson xG модель)...</b> Будь ласка, зачекайте.")
    
    signals = []
    now_utc = datetime.now(timezone.utc)
    max_time_utc = now_utc + timedelta(hours=48)
    requests_made = 0

    for league in LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/?apiKey={ODDS_API_KEY}&regions={REGIONS}&markets={MARKETS}"
        
        try:
            res = requests.get(url, timeout=10)
            requests_made += 1
            if res.status_code != 200:
                continue
            data = res.json()
        except Exception as e:
            logging.error(f"Error fetching {league}: {e}")
            continue

        for match in data:
            commence_time_str = match.get('commence_time')
            if not commence_time_str:
                continue
            
            try:
                match_time_utc = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
                if not (now_utc <= match_time_utc <= max_time_utc):
                    continue
                
                match_time_kyiv = match_time_utc + timedelta(hours=3)
                formatted_time = match_time_kyiv.strftime("%d.%m о %H:%M")
            except Exception as e:
                continue

            home = match.get('home_team')
            away = match.get('away_team')
            bookmakers = match.get('bookmakers', [])

            market_outcomes = {}
            for bm in bookmakers:
                for mk in bm.get('markets', []):
                    for out in mk.get('outcomes', []):
                        name = out.get('name')
                        price = out.get('price')
                        point = out.get('point', '')
                        
                        outcome_key = (mk.get('key'), name, point)
                        if outcome_key not in market_outcomes:
                            market_outcomes[outcome_key] = []
                        market_outcomes[outcome_key].append((bm['title'], price))

            h_prices = [p[1] for k, prices in market_outcomes.items() for p in prices if k[1] == home]
            a_prices = [p[1] for k, prices in market_outcomes.items() for p in prices if k[1] == away]
            
            avg_h = sum(h_prices) / len(h_prices) if h_prices else 2.5
            avg_a = sum(a_prices) / len(a_prices) if a_prices else 3.0

            xg_h, xg_a = estimate_xg_from_market(avg_h, avg_a)
            p_home, p_draw, p_away = calculate_match_probabilities(xg_h, xg_a)

            model_probs = {
                home: p_home,
                "Draw": p_draw,
                away: p_away
            }

            best_match_signal = None
            max_ev_found = -100

            for (m_key, name, point), prices in market_outcomes.items():
                if len(prices) < 3:
                    continue

                all_odds = [p[1] for p in prices]
                max_price = max(all_odds)
                
                model_prob = model_probs.get(name, 0)
                if model_prob <= 0:
                    continue

                ev = (max_price * model_prob - 1) * 100
                fair_odds = round(1.0 / model_prob, 2) if model_prob > 0 else 0

                if MIN_EV <= ev <= MAX_EV and MIN_ODDS <= max_price <= MAX_ODDS:
                    if ev > max_ev_found:
                        max_ev_found = ev
                        best_bookies = [p[0] for p in prices if p[1] == max_price]
                        
                        best_match_signal = (
                            f"🎯 <b>POISSON VALUE BET</b>\n\n"
                            f"⚽ <b>Матч:</b> {home} vs {away}\n"
                            f"📊 <b>xG Модель:</b> {xg_h} - {xg_a}\n"
                            f"📅 <b>Час:</b> {formatted_time} (Кв)\n"
                            f"🏆 <b>Ліга:</b> {league}\n"
                            f"📌 <b>Ставка:</b> {name}\n"
                            f"📈 <b>Коефіцієнт:</b> <code>{max_price}</code>\n"
                            f"⚖️ <b>Модельний кф:</b> {fair_odds} ({round(model_prob * 100, 1)}%)\n"
                            f"🔥 <b>EV:</b> +{round(ev, 2)}%\n"
                            f"🏦 <b>БК:</b> {', '.join(best_bookies)}"
                        )

            if best_match_signal:
                signals.append(best_match_signal)

    # Відправляємо результати
    if signals:
        for sig in signals:
            send_telegram_message(sig)
        send_telegram_message(f"✅ <b>Сканування завершено!</b> Знайдено валуїв: {len(signals)}. Використано запитів API: {requests_made}")
    else:
        send_telegram_message(f"📭 <b>Сканування завершено.</b> Наразі валуїв за вашими критеріями не знайдено. (Використано запитів API: {requests_made})")

# === ОБРОБНИК TELEGRAM КОМАНД ===

@bot.message_handler(commands=['scan'])
def handle_scan_command(message):
    # Можна додати перевірку на твій власний chat_id, якщо треба безпека
    if str(message.chat.id) != str(TELEGRAM_CHAT_ID):
        return
    # Запускаємо сканування в окремому потоці, щоб не блокувати бота
    t = Thread(target=run_manual_scan)
    t.start()

@bot.message_handler(commands=['start'])
def handle_start(message):
    send_telegram_message("🤖 <b>Бот готовий до роботи у ручному режимі.</b>\nНапиши /scan, щоб запустити перевірку ліній.")

if __name__ == '__main__':
    keep_alive()  # Підтримка вебсервера для Render
    
    # Запуск телеграм-бота в режимі постійного прослуховування команд (long polling)
    # Він не робить запити до The Odds API поки ти не відправиш /scan!
    bot.infinity_polling()
