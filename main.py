import os
import math
import requests
import threading
from datetime import datetime
from flask import Flask
import telebot

# ================================
# 0. ВЕБ-СЕРВЕР ДЛЯ RENDER
# ================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Value Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

threading.Thread(target=run_flask, daemon=True).start()

# ================================
# 1. КОНФІГУРАЦІЯ
# ================================
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

MIN_ODDS = 1.75
MAX_ODDS = 3.6
MIN_EV = 5.0  # Поріг EV +3%

# Повний список з 22 ліг
LEAGUES_MAP = {
    # Топ-5 Ліг
    "soccer_epl": "Англія: Прем'єр-ліга",
    "soccer_spain_la_liga": "Іспанія: Ла Ліга",
    "soccer_germany_bundesliga": "Німеччина: Бундесліга",
    "soccer_italy_serie_a": "Італія: Серія А",
    "soccer_france_ligue_one": "Франція: Ліга 1",
    
    # Англійські нижчі дивізіони
    "soccer_england_championship": "Англія: Чемпіоншип",
    "soccer_england_league1": "Англія: Перша ліга",
    "soccer_england_league2": "Англія: Друга ліга",
    
    # Інші європейські чемпіонати
    "soccer_netherlands_eredivisie": "Нідерланди: Ередивізі",
    "soccer_portugal_primeira_liga": "Португалія: Прімейра",
    "soccer_turkey_super_league": "Туреччина: Суперліга",
    "soccer_belgium_first_div": "Бельгія: Про-ліга",
    "soccer_scotland_premier_league": "Шотландія: Прем'єр-ліга",
    "soccer_austria_bundesliga": "Австрія: Бундесліга",
    "soccer_switzerland_superleague": "Швейцарія: Суперліга",
    "soccer_denmark_superliga": "Данія: Суперліга",
    "soccer_norway_eliteserien": "Норвегія: Елітсеріен",
    "soccer_sweden_allsvenskan": "Швеція: Аллсвенскан",
    "soccer_poland_ekstraklasa": "Польща: Екстракляса",
    "soccer_greece_super_league": "Греція: Суперліга",
    
    # Другі дивізіони Топ-країн
    "soccer_spain_segunda_division": "Іспанія: Сегунда",
    "soccer_germany_bundesliga2": "Німеччина: Друга Бундесліга"
}

# ================================
# 2. МАТЕМАТИКА ПУАССОНА
# ================================
def poisson_probability(lmbda: float, k: int) -> float:
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_fair_odds_and_ev(home_xg: float, away_xg: float, bk_odds: float):
    home_win_prob = 0.0
    for h in range(0, 6):
        for a in range(0, 6):
            if h > a:
                home_win_prob += poisson_probability(home_xg, h) * poisson_probability(away_xg, a)
                
    if home_win_prob <= 0:
        return None, None, None

    fair_odds = round(1 / home_win_prob, 2)
    ev = round(((bk_odds / fair_odds) - 1) * 100, 2)
    prob_pct = round(home_win_prob * 100, 1)
    
    return fair_odds, ev, prob_pct

def format_match_time(iso_time_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_time_str.replace("Z", "+00:00"))
        return dt.strftime("%d.%m о %H:%M")
    except Exception:
        return "Час невідомий"

# ================================
# 3. СКАНУВАННЯ
# ================================
def run_scan_and_notify(chat_id):
    bot.send_message(chat_id, "🔎 <b>Запуск сканування 22 ліг...</b>", parse_mode="HTML")
    found_count = 0

    DEFAULT_HOME_XG = 1.45
    DEFAULT_AWAY_XG = 1.15

    for odds_league_key, league_title in LEAGUES_MAP.items():
        try:
            res_raw = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{odds_league_key}/odds/",
                params={'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'},
                timeout=10
            )
            odds_res = res_raw.json()
        except Exception as e:
            print(f"Помилка Odds API ({odds_league_key}): {e}")
            continue

        if not isinstance(odds_res, list) or not odds_res:
            continue

        for match in odds_res:
            home_team = match['home_team']
            away_team = match['away_team']
            match_time_formatted = format_match_time(match.get('commence_time', ''))

            max_odds = 0.0
            best_bk_name = ""

            for bm in match.get('bookmakers', []):
                for market in bm.get('markets', []):
                    if market['key'] == 'h2h':
                        for outcome in market.get('outcomes', []):
                            if outcome['name'] == home_team and outcome['price'] > max_odds:
                                max_odds = outcome['price']
                                best_bk_name = bm['title']

            if not (MIN_ODDS <= max_odds <= MAX_ODDS):
                continue

            fair_odds, ev, prob_pct = calculate_fair_odds_and_ev(DEFAULT_HOME_XG, DEFAULT_AWAY_XG, max_odds)

            if ev is not None and ev >= MIN_EV:
                found_count += 1
                msg = (
                    f"🎯 <b>VALUE BET FOUND</b>\n\n"
                    f"⚽️ <b>Матч:</b> {home_team} vs {away_team}\n"
                    f"📅 <b>Час:</b> {match_time_formatted}\n"
                    f"🏆 <b>Ліга:</b> {league_title}\n"
                    f"📌 <b>Ставка:</b> {home_team} (П1)\n"
                    f"📈 <b>Макс. кф БК:</b> {max_odds} ({best_bk_name})\n"
                    f"⚖️ <b>Справедливий кф:</b> {fair_odds} ({prob_pct}%)\n"
                    f"🔥 <b>EV:</b> +{ev}%"
                )
                bot.send_message(chat_id, msg, parse_mode="HTML")

    if found_count == 0:
        bot.send_message(chat_id, "🏁 Завершено. Валуїв з позитивним EV не знайдено.")
    else:
        bot.send_message(chat_id, f"✅ Завершено. Знайдено валуйних матчів: {found_count}")

# ================================
# 4. TELEGRAM HANDLERS
# ================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Надішли 'скан' або /scan для запуску.")

@bot.message_handler(func=lambda message: message.text.lower() in ['скан', '/scan'])
def handle_scan_request(message):
    run_scan_and_notify(message.chat.id)

if __name__ == "__main__":
    print("🤖 Бот чекає команду 'скан'...")
    bot.infinity_polling()
