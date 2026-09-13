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

MIN_EV = 5.0          # Мінімальна перевага +5%
MAX_EV = 25.0         # Максимальна перевага
MIN_ODDS = 1.60       # Мінімальний кф
MAX_ODDS = 3.40       # Максимальний кф

MARKETS = "h2h"
REGIONS = "eu"

LEAGUES = [
    "soccer_epl", "soccer_england_championship", "soccer_england_league1", "soccer_england_league2",
    "soccer_england_efl_cup", "soccer_fa_cup", "soccer_spain_la_liga", "soccer_spain_segunda_division",
    "soccer_italy_serie_a", "soccer_italy_serie_b", "soccer_germany_bundesliga", "soccer_germany_bundesliga2",
    "soccer_france_lique_one", "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league", "soccer_uefa_nations_league", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_belgium_first_div", "soccer_turkey_super_league", "soccer_austria_bundesliga"
]

logging.basicConfig(level=logging.INFO)
app = Flask('')

@app.route('/')
def home():
    return "Poisson True Value Bot is running!"

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

# === МАТЕМАТИЧНА МОДЕЛЬ ПУАССОНА ===

def poisson_prob(lmbda, k):
    if lmbda <= 0:
        return 0
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_match_probabilities(xg_home, xg_away):
    max_goals = 6
    p_home, p_draw, p_away = 0.0, 0.0, 0.0

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

def extract_fair_sharp_probabilities(bookmakers, home_team, away_team):
    """
    Витягує лінію Sharp-букмекера (Pinnacle/Betfair) та знімає маржу (Fair Probability), 
    щоб отримати об'єктивний xG та справжню ймовірність події.
    """
    sharp_bm = None
    for bm in bookmakers:
        if bm.get('key') in ['pinnacle', 'betfair_ex_uk', 'matchbook']:
            sharp_bm = bm
            break
    
    if not sharp_bm and bookmakers:
        sharp_bm = bookmakers[0] # Резерв

    if not sharp_bm:
        return None, None, None

    h_odds, d_odds, a_odds = None, None, None
    for mk in sharp_bm.get('markets', []):
        if mk.get('key') == 'h2h':
            for out in mk.get('outcomes', []):
                if out.get('name') == home_team: h_odds = out.get('price')
                elif out.get('name') == 'Draw': d_odds = out.get('price')
                elif out.get('name') == away_team: a_odds = out.get('price')

    if not (h_odds and d_odds and a_odds):
        return None, None, None

    # Очищення від маржі (Zero-margin normalization)
    raw_h, raw_d, raw_a = 1.0 / h_odds, 1.0 / d_odds, 1.0 / a_odds
    margin_sum = raw_h + raw_d + raw_a

    fair_h = raw_h / margin_sum
    fair_d = raw_d / margin_sum
    fair_a = raw_a / margin_sum

    return fair_h, fair_d, fair_a

# === ЛОГІКА СКАНУВАННЯ ===

def run_manual_scan():
    if not ODDS_API_KEY:
        send_telegram_message("❌ <b>Помилка:</b> Відсутній ODDS_API_KEY!")
        return

    send_telegram_message("🔍 <b>Запущено сканування (True Poisson xG + Fair Odds)...</b>")
    
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
            continue

        for match in data:
            commence_time_str = match.get('commence_time')
            if not commence_time_str: continue
            
            try:
                match_time_utc = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
                if not (now_utc <= match_time_utc <= max_time_utc): continue
                match_time_kyiv = match_time_utc + timedelta(hours=3)
                formatted_time = match_time_kyiv.strftime("%d.%m о %H:%M")
            except Exception:
                continue

            home = match.get('home_team')
            away = match.get('away_team')
            bookmakers = match.get('bookmakers', [])

            fair_h, fair_d, fair_a = extract_fair_sharp_probabilities(bookmakers, home, away)
            if not fair_h: continue

            # Оцінка xG на основі безмаржинальних ймовірностей
            base_total_goals = 2.70
            xg_h = round((fair_h / (fair_h + fair_a)) * base_total_goals * 1.08, 2)
            xg_a = round((fair_a / (fair_h + fair_a)) * base_total_goals * 0.92, 2)

            p_h, p_d, p_a = calculate_match_probabilities(xg_h, xg_a)

            model_probs = {home: p_h, "Draw": p_d, away: p_a}

            for bm in bookmakers:
                bm_name = bm.get('title')
                for mk in bm.get('markets', []):
                    if mk.get('key') != 'h2h': continue
                    for out in mk.get('outcomes', []):
                        name = out.get('name')
                        price = out.get('price')

                        model_prob = model_probs.get(name, 0)
                        if model_prob <= 0: continue

                        ev = (price * model_prob - 1) * 100
                        fair_odds = round(1.0 / model_prob, 2)

                        if MIN_EV <= ev <= MAX_EV and MIN_ODDS <= price <= MAX_ODDS:
                            signals.append(
                                f"🎯 <b>TRUE POISSON VALUE BET</b>\n\n"
                                f"⚽ <b>Матч:</b> {home} vs {away}\n"
                                f"📊 <b>Model xG:</b> {xg_h} - {xg_a}\n"
                                f"📅 <b>Час:</b> {formatted_time} (Кв)\n"
                                f"🏆 <b>Ліга:</b> {league}\n"
                                f"📌 <b>Ставка:</b> {name}\n"
                                f"📈 <b>Коефіцієнт БК:</b> <code>{price}</code> ({bm_name})\n"
                                f"⚖️ <b>Справедливий кф:</b> {fair_odds} ({round(model_prob * 100, 1)}%)\n"
                                f"🔥 <b>Чистий EV:</b> +{round(ev, 2)}%"
                            )

    if signals:
        for sig in signals:
            send_telegram_message(sig)
        send_telegram_message(f"✅ <b>Завершено!</b> Знайдено валуїв: {len(signals)}. Запитів API: {requests_made}")
    else:
        send_telegram_message(f"📭 <b>Завершено.</b> Валуїв не знайдено. Запитів API: {requests_made}")

@bot.message_handler(commands=['scan'])
def handle_scan_command(message):
    if str(message.chat.id) != str(TELEGRAM_CHAT_ID):
        return
    t = Thread(target=run_manual_scan)
    t.start()

@bot.message_handler(commands=['start'])
def handle_start(message):
    send_telegram_message("🤖 <b>Бот готовий.</b> Напиши /scan для пошуку валуїв.")

if __name__ == '__main__':
    keep_alive()
    bot.infinity_polling()
