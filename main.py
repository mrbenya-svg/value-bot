import os
import math
import requests
from difflib import get_close_matches
import telebot

# ================================
# 1. КОНФІГУРАЦІЯ
# ================================
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"
HEADERS_FOOTBALL = {'x-apisports-key': API_FOOTBALL_KEY}

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Фільтри
MIN_ODDS = 1.50
MAX_ODDS = 4.50
MIN_EV = 5.0  # % EV

# Мапінг 22 ліг
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
# 2. МАТЕМАТИКА
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
# 3. ЕКОНОМНА РОБОТА З API-FOOTBALL
# ================================
def fetch_league_teams_once(league_id: int):
    if league_id in LEAGUE_TEAMS_CACHE:
        return LEAGUE_TEAMS_CACHE[league_id]

    try:
        res = requests.get(
            f"{API_FOOTBALL_URL}/teams",
            headers=HEADERS_FOOTBALL,
            params={'league': league_id, 'season': 2024},
            timeout=10
        ).json()
        
        teams_map = {}
        for item in res.get('response', []):
            t_name = item['team']['name']
            t_id = item['team']['id']
            teams_map[t_name] = t_id
            
        LEAGUE_TEAMS_CACHE[league_id] = teams_map
        return teams_map
    except Exception as e:
        print(f"Помилка завантаження команд ліги {league_id}: {e}")
        return {}

def find_best_team_match(odds_team_name: str, api_teams_map: dict):
    if not api_teams_map:
        return None
        
    known_names = list(api_teams_map.keys())
    matches = get_close_matches(odds_team_name, known_names, n=1, cutoff=0.5)
    
    if matches:
        matched_name = matches[0]
        return api_teams_map[matched_name]
    return None

def get_real_team_xg(team_id: int, league_id: int):
    cache_key = f"{team_id}_{league_id}"
    if cache_key in XG_CACHE:
        return XG_CACHE[cache_key]

    try:
        res = requests.get(
            f"{API_FOOTBALL_URL}/fixtures",
            headers=HEADERS_FOOTBALL,
            params={'team': team_id, 'league': league_id, 'last': 5, 'status': 'FT'},
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
        XG_CACHE[cache_key] = xg
        return xg
    except Exception as e:
        print(f"Помилка розрахунку xG для team_id {team_id}: {e}")
        return None

# ================================
# 4. ГОЛОВНА ФУНКЦІЯ СКТАНУВАННЯ
# ================================
def run_scan_and_notify(chat_id):
    bot.send_message(chat_id, "🔎 <b>Запуск сканування 22 ліг... Зачекайте.</b>", parse_mode="HTML")
    found_count = 0

    for odds_league_key, fb_league_id in LEAGUES_MAP.items():
        api_teams_map = fetch_league_teams_once(fb_league_id)
        if not api_teams_map:
            continue

        try:
            odds_res = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{odds_league_key}/odds/",
                params={'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'},
                timeout=10
            ).json()
        except Exception:
            continue

        if not isinstance(odds_res, list):
            continue

        for match in odds_res:
            home_team_odds = match['home_team']
            away_team_odds = match['away_team']

            best_odds = 0.0
            best_bk_name = ""
            for bm in match.get('bookmakers', []):
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
                continue

            home_xg = get_real_team_xg(home_team_id, fb_league_id)
            away_xg = get_real_team_xg(away_team_id, fb_league_id)

            if home_xg is None or away_xg is None:
                continue

            fair_odds, ev, prob_pct = calculate_fair_odds_and_ev(home_xg, away_xg, best_odds)

            if ev is not None and ev >= MIN_EV:
                found_count += 1
                msg = (
                    f"🎯 <b>TRUE POISSON VALUE BET (REAL xG)</b>\n\n"
                    f"⚽️ <b>Матч:</b> {home_team_odds} vs {away_team_odds}\n"
                    f"📊 <b>Model xG (Last 5):</b> {home_xg} - {away_xg}\n"
                    f"🏆 <b>Ліга:</b> {odds_league_key}\n"
                    f"📌 <b>Ставка:</b> {home_team_odds} (П1)\n"
                    f"📈 <b>Коефіцієнт БК:</b> {best_odds} ({best_bk_name})\n"
                    f"⚖️ <b>Справедливий кф:</b> {fair_odds} ({prob_pct}%)\n"
                    f"🔥 <b>Чистий EV:</b> +{ev}%"
                )
                bot.send_message(chat_id, msg, parse_mode="HTML")

    if found_count == 0:
        bot.send_message(chat_id, "🏁 Сканування завершено. Валуйних ставок за поточними фільтрами не знайдено.")
    else:
        bot.send_message(chat_id, f"✅ Сканування завершено. Знайдено валуїв: {found_count}")

# ================================
# 5. ОБРОБКА КОМАНД TELEGRAM
# ================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Привіт! Надішли команду /scan або слово 'скан', щоб запустити пошук валуїв.")

@bot.message_handler(func=lambda message: message.text.lower() in ['скан', '/scan'])
def handle_scan_request(message):
    run_scan_and_notify(message.chat.id)

if __name__ == "__main__":
    print("🤖 Бот запущений і чекає команди 'скан'...")
    bot.infinity_polling()
