import os
import math
import requests
import threading
import re
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
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"
HEADERS_FOOTBALL = {'x-apisports-key': API_FOOTBALL_KEY}

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

MIN_ODDS = 1.30
MAX_ODDS = 5.00
MIN_EV = 3.0
CURRENT_SEASON = 2026  # Сезон 2026/2027

LEAGUES_MAP = {
    "soccer_epl": "Англія: Прем'єр-ліга",
    "soccer_england_championship": "Англія: Чемпіоншип",
    "soccer_spain_la_liga": "Іспанія: Ла Ліга",
    "soccer_germany_bundesliga": "Німеччина: Бундесліга",
    "soccer_italy_serie_a": "Італія: Серія А",
    "soccer_france_ligue_one": "Франція: Ліга 1",
    "soccer_netherlands_eredivisie": "Нідерланди: Ередивізі",
    "soccer_portugal_primeira_liga": "Португалія: Прімейра",
    "soccer_turkey_super_league": "Туреччина: Суперліга"
}

TEAM_ID_CACHE = {}
XG_CACHE = {}

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

# ================================
# 3. ПОШУК ТА ОБРОБКА ДАТИ
# ================================
def format_match_time(iso_time_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_time_str.replace("Z", "+00:00"))
        return dt.strftime("%d.%m о %H:%M")
    except Exception:
        return "Час невідомий"

def get_team_id_by_search(team_name: str):
    if team_name in TEAM_ID_CACHE:
        return TEAM_ID_CACHE[team_name]

    clean_search = re.sub(r'\b(FC|CF|AFC|BSC|SC|AC|FK|SV|1\.)\b', '', team_name, flags=re.IGNORECASE).strip()
    
    try:
        res = requests.get(
            f"{API_FOOTBALL_URL}/teams",
            headers=HEADERS_FOOTBALL,
            params={'search': clean_search},
            timeout=10
        ).json()
        
        # Перевірка статусу ключа
        if 'errors' in res and res['errors']:
            print(f"❌ Помилка API-Football: {res['errors']}")
            return None

        teams_data = res.get('response', [])
        if teams_data:
            team_id = teams_data[0]['team']['id']
            TEAM_ID_CACHE[team_name] = team_id
            return team_id
    except Exception as e:
        print(f"Помилка пошуку ID для {team_name}: {e}")

    TEAM_ID_CACHE[team_name] = None
    return None

def get_real_team_xg(team_id: int):
    if not team_id:
        return None
        
    if team_id in XG_CACHE:
        return XG_CACHE[team_id]

    try:
        res = requests.get(
            f"{API_FOOTBALL_URL}/fixtures",
            headers=HEADERS_FOOTBALL,
            params={'team': team_id, 'last': 5, 'season': CURRENT_SEASON, 'status': 'FT'},
            timeout=10
        ).json()
        
        fixtures = res.get('response', [])
        if not fixtures:
            return None

        total_goals = 0
        for match in fixtures:
            if match['teams']['home']['id'] == team_id:
                goals = match['goals']['home']
            else:
                goals = match['goals']['away']
            total_goals += goals if goals is not None else 0

        xg = round(total_goals / len(fixtures), 2)
        XG_CACHE[team_id] = xg
        return xg
    except Exception as e:
        print(f"Помилка xG для team_id {team_id}: {e}")
        return None

# ================================
# 4. СКАНУВАННЯ
# ================================
def run_scan_and_notify(chat_id):
    bot.send_message(chat_id, "🔎 <b>Запуск точного сканування...</b>", parse_mode="HTML")
    found_count = 0

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

            home_id = get_team_id_by_search(home_team)
            away_id = get_team_id_by_search(away_team)

            if not home_id or not away_id:
                continue

            home_xg = get_real_team_xg(home_id)
            away_xg = get_real_team_xg(away_id)

            if home_xg is None or away_xg is None:
                continue

            fair_odds, ev, prob_pct = calculate_fair_odds_and_ev(home_xg, away_xg, max_odds)

            if ev is not None and ev >= MIN_EV:
                found_count += 1
                msg = (
                    f"🎯 <b>VALUE BET FOUND</b>\n\n"
                    f"⚽️ <b>Матч:</b> {home_team} vs {away_team}\n"
                    f"📅 <b>Час:</b> {match_time_formatted}\n"
                    f"📊 <b>Model xG (Last 5):</b> {home_xg} - {away_xg}\n"
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
# 5. TELEGRAM HANDLERS
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
