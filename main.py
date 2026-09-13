import os
import math
import requests
import threading
import re
from datetime import datetime
from difflib import get_close_matches
from flask import Flask
import telebot

# ================================
# 0. ВЕБ-СЕРВЕР ДЛЯ RENDER (PORT BINDING)
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

# Фільтри
MIN_ODDS = 1.30
MAX_ODDS = 5.00
MIN_EV = 1.0  # % EV

ALLOWED_BOOKMAKERS = ["pinnacle", "bet365", "unibet", "1xbet", "onexbet"]

LEAGUES_MAP = {
    "soccer_epl": 39,
    "soccer_england_championship": 40,
    "soccer_england_league1": 41,
    "soccer_england_league2": 42,
    "soccer_england_efl_cup": 48,
    "soccer_spain_la_liga": 140,
    "soccer_spain_segunda_division": 141,
    "soccer_germany_bundesliga": 78,
    "soccer_germany_bundesliga2": 79,
    "soccer_germany_3liga": 80,
    "soccer_italy_serie_a": 135,
    "soccer_italy_serie_b": 136,
    "soccer_france_ligue_one": 61,
    "soccer_france_ligue_two": 62,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
    "soccer_belgium_first_div": 144,
    "soccer_turkey_super_lig": 203,
    "soccer_scotland_premiership": 179,
    "soccer_austria_bundesliga": 218,
    "soccer_switzerland_superleague": 207,
    "soccer_greece_super_league": 197
}

LEAGUE_TEAMS_CACHE = {}
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
# 3. РОБОТА З API-FOOTBALL
# ================================
def fetch_league_teams_once(league_id: int):
    if league_id in LEAGUE_TEAMS_CACHE:
        return LEAGUE_TEAMS_CACHE[league_id]

    current_year = datetime.now().year

    try:
        res = requests.get(
            f"{API_FOOTBALL_URL}/teams",
            headers=HEADERS_FOOTBALL,
            params={'league': league_id, 'season': current_year},
            timeout=10
        ).json()
        
        teams_map = {}
        for item in res.get('response', []):
            teams_map[item['team']['name']] = item['team']['id']
            
        LEAGUE_TEAMS_CACHE[league_id] = teams_map
        return teams_map
    except Exception as e:
        print(f"Помилка завантаження команд ліги {league_id}: {e}")
        return {}

def clean_team_name(name: str) -> str:
    name = re.sub(r'\b(FC|CF|AFC|BSC|SC|AC|FK|SV|1\.)\b', '', name, flags=re.IGNORECASE)
    return name.strip().lower()

def find_best_team_match(odds_team_name: str, api_teams_map: dict):
    if not api_teams_map:
        return None
        
    known_names = list(api_teams_map.keys())
    clean_odds_name = clean_team_name(odds_team_name)
    
    for name in known_names:
        if clean_team_name(name) == clean_odds_name:
            return api_teams_map[name]
            
    matches = get_close_matches(odds_team_name, known_names, n=1, cutoff=0.3)
    if matches:
        return api_teams_map[matches[0]]
        
    return None

def get_real_team_xg(team_id: int):
    if team_id in XG_CACHE:
        return XG_CACHE[team_id]

    try:
        res = requests.get(
            f"{API_FOOTBALL_URL}/fixtures",
            headers=HEADERS_FOOTBALL,
            params={'team': team_id, 'last': 5, 'status': 'FT'},
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
    bot.send_message(chat_id, "🔎 <b>Запуск сканування 22 ліг...</b>", parse_mode="HTML")
    found_count = 0

    for odds_league_key, fb_league_id in LEAGUES_MAP.items():
        api_teams_map = fetch_league_teams_once(fb_league_id)
        if not api_teams_map:
            print(f"⚠️ Не вдалося отримати команди для ліги {odds_league_key} (ID: {fb_league_id})")
            continue

        try:
            odds_res = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{odds_league_key}/odds/",
                params={'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'},
                timeout=10
            ).json()
        except Exception as e:
            print(f"Помилка Odds API для {odds_league_key}: {e}")
            continue

        if not isinstance(odds_res, list):
            continue

        for match in odds_res:
            home_team_odds = match['home_team']
            away_team_odds = match['away_team']

            best_odds = 0.0
            best_bk_name = ""
            
            for bm in match.get('bookmakers', []):
                bm_key = bm.get('key', '').lower()
                if any(allowed in bm_key for allowed in ALLOWED_BOOKMAKERS):
                    for market in bm.get('markets', []):
                        if market['key'] == 'h2h':
                            for outcome in market.get('outcomes', []):
                                if outcome['name'] == home_team_odds and outcome['price'] > best_odds:
                                    best_odds = outcome['price']
                                    best_bk_name = bm['title']

            if not (MIN_ODDS <= best_odds <= MAX_ODDS):
                continue

            home_team_id = find_best_team_match(home_team_odds, api_teams_map)
            away_team_id = find_best_team_match(away_team_odds, api_teams_map)

            if not home_team_id or not away_team_id:
                print(f"❌ Не знайдено ID для: {home_team_odds} або {away_team_odds}")
                continue

            home_xg = get_real_team_xg(home_team_id)
            away_xg = get_real_team_xg(away_team_id)

            if home_xg is None or away_xg is None:
                print(f"❌ Немає xG для: {home_team_odds} ({home_xg}) або {away_team_odds} ({away_xg})")
                continue

            fair_odds, ev, prob_pct = calculate_fair_odds_and_ev(home_xg, away_xg, best_odds)

            if ev is not None and ev >= MIN_EV:
                found_count += 1
                msg = (
                    f"🎯 <b>VALUE BET FOUND</b>\n\n"
                    f"⚽️ <b>Матч:</b> {home_team_odds} vs {away_team_odds}\n"
                    f"📊 <b>Model xG (Last 5):</b> {home_xg} - {away_xg}\n"
                    f"🏆 <b>Ліга:</b> {odds_league_key}\n"
                    f"📌 <b>Ставка:</b> {home_team_odds} (П1)\n"
                    f"📈 <b>Коефіцієнт БК:</b> {best_odds} ({best_bk_name})\n"
                    f"⚖️ <b>Справедливий кф:</b> {fair_odds} ({prob_pct}%)\n"
                    f"🔥 <b>EV:</b> {ev}%"
                )
                bot.send_message(chat_id, msg, parse_mode="HTML")

    if found_count == 0:
        bot.send_message(chat_id, "🏁 Завершено. Валуїв не знайдено.")
    else:
        bot.send_message(chat_id, f"✅ Завершено. Знайдено матчів: {found_count}")

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
