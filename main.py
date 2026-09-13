import os
import math
import requests

# ================================
# 1. КОНФІГУРАЦІЯ ТА ЗМІННІ СЕРЕДОВИЩА
# ================================
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"
HEADERS_FOOTBALL = {'x-apisports-key': API_FOOTBALL_KEY}

# Фільтри для відсіювання ставок
MIN_ODDS = 1.50      # Мінімальний коефіцієнт
MAX_ODDS = 4.50      # Максимальний коефіцієнт
MIN_EV = 5.0         # Мінімальний чистий EV (%)

# Мапінг усіх 22 ліг (Odds API Key -> API-Football League ID)
LEAGUES_MAP = {
    "soccer_epl": 39,                     # EPL (Англія)
    "soccer_england_championship": 40,    # Championship (Англія)
    "soccer_england_league1": 41,         # League One (Англія)
    "soccer_england_league2": 42,         # League Two (Англія)
    "soccer_england_efl_cup": 48,         # EFL Cup (Англія)
    "soccer_spain_la_liga": 140,          # La Liga (Іспанія)
    "soccer_spain_segunda_division": 141, # La Liga 2 (Іспанія)
    "soccer_germany_bundesliga": 78,      # Bundesliga (Німеччина)
    "soccer_germany_bundesliga2": 79,     # 2. Bundesliga (Німеччина)
    "soccer_germany_3liga": 80,           # 3. Liga (Німеччина)
    "soccer_italy_serie_a": 135,          # Serie A (Італія)
    "soccer_italy_serie_b": 136,          # Serie B (Італія)
    "soccer_france_ligue_one": 61,        # Ligue 1 (Франція)
    "soccer_france_ligue_two": 62,        # Ligue 2 (Франція)
    "soccer_netherlands_eredivisie": 88,   # Eredivisie (Нідерланди)
    "soccer_portugal_primeira_liga": 94,  # Primeira Liga (Португалія)
    "soccer_belgium_first_div": 144,      # Pro League (Бельгія)
    "soccer_turkey_super_lig": 203,       # Super Lig (Туреччина)
    "soccer_scotland_premiership": 179,   # Premiership (Шотландія)
    "soccer_austria_bundesliga": 218,     # Bundesliga (Австрія)
    "soccer_switzerland_superleague": 207,# Super League (Швейцарія)
    "soccer_greece_super_league": 197     # Super League (Греція)
}

# ================================
# 2. МАТЕМАТИКА ТА РОЗРАХУНКИ
# ================================
def poisson_probability(lmbda: float, k: int) -> float:
    """Обчислення ймовірності за розподілом Пуассона"""
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_fair_odds_and_ev(home_xg: float, away_xg: float, bk_odds: float):
    """Розрахунок ймовірності П1, справедливого кф та EV"""
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
# 3. ОТРИМАННЯ ДАНИХ З API-FOOTBALL
# ================================
def get_team_xg_by_league(team_name: str, league_id: int) -> float:
    """Шукає команду та рахує середній xG/голи за 5 останніх матчів у лізі"""
    try:
        # Пошук ID команди
        search_res = requests.get(
            f"{API_FOOTBALL_URL}/teams",
            headers=HEADERS_FOOTBALL,
            params={'search': team_name},
            timeout=10
        ).json()
        
        teams = search_res.get('response', [])
        if not teams:
            return 1.20
            
        team_id = teams[0]['team']['id']
        
        # Отримання 5 останніх завершених матчів
        fixtures_res = requests.get(
            f"{API_FOOTBALL_URL}/fixtures",
            headers=HEADERS_FOOTBALL,
            params={'team': team_id, 'league': league_id, 'last': 5, 'status': 'FT'},
            timeout=10
        ).json()
        
        fixtures = fixtures_res.get('response', [])
        if not fixtures:
            return 1.20
            
        total_goals = 0
        for match in fixtures:
            if match['teams']['home']['id'] == team_id:
                goals = match['goals']['home']
            else:
                goals = match['goals']['away']
            total_goals += goals if goals is not None else 1
            
        return round(total_goals / len(fixtures), 2)

    except Exception as e:
        print(f"Помилка отримання xG для {team_name}: {e}")
        return 1.20

# ================================
# 4. ВІДПРАВКА В TELEGRAM
# ================================
def send_telegram_alert(home_team, away_team, home_xg, away_xg, league_key, bk_name, bk_odds, fair_odds, prob_pct, ev):
    """Форматує та відправляє картку валуйної ставки в Telegram"""
    message = (
        f"🎯 <b>TRUE POISSON VALUE BET (REAL xG)</b>\n\n"
        f"⚽️ <b>Матч:</b> {home_team} vs {away_team}\n"
        f"📊 <b>Model xG (Last 5):</b> {home_xg} - {away_xg}\n"
        f"🏆 <b>Ліга:</b> {league_key}\n"
        f"📌 <b>Ставка:</b> {home_team} (П1)\n"
        f"📈 <b>Коефіцієнт БК:</b> {bk_odds} ({bk_name})\n"
        f"⚖️ <b>Справедливий кф:</b> {fair_odds} ({prob_pct}%)\n"
        f"🔥 <b>Чистий EV:</b> +{ev}%"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Помилка відправки в Telegram: {e}")

# ================================
# 5. ГОЛОВНИЙ ЦИКЛ СКАНУВАННЯ
# ================================
def scan_all_leagues():
    """Сканує всі 22 ліги з The Odds API"""
    print("🚀 Запуск сканування 22 ліг...")
    
    for odds_league_key, fb_league_id in LEAGUES_MAP.items():
        odds_url = f"https://api.the-odds-api.com/v4/sports/{odds_league_key}/odds/"
        
        try:
            odds_res = requests.get(
                odds_url,
                params={
                    'apiKey': ODDS_API_KEY,
                    'regions': 'eu',
                    'markets': 'h2h'
                },
                timeout=10
            ).json()
        except Exception as e:
            print(f"Помилка запиту до Odds API ({odds_league_key}): {e}")
            continue

        if not isinstance(odds_res, list):
            continue

        for match in odds_res:
            home_team = match['home_team']
            away_team = match['away_team']
            
            bookmakers = match.get('bookmakers', [])
            if not bookmakers:
                continue

            # Знаходимо кращий коефіцієнт на господарів (П1)
            best_odds = 0.0
            best_bk_name = ""

            for bm in bookmakers:
                for market in bm.get('markets', []):
                    if market['key'] == 'h2h':
                        for outcome in market.get('outcomes', []):
                            if outcome['name'] == home_team:
                                price = outcome['price']
                                if price > best_odds:
                                    best_odds = price
                                    best_bk_name = bm['title']

            # Фільтр по мінімальному і максимальному кф
            if not (MIN_ODDS <= best_odds <= MAX_ODDS):
                continue

            # Запит реального xG з API-Football
            home_xg = get_team_xg_by_league(home_team, fb_league_id)
            away_xg = get_team_xg_by_league(away_team, fb_league_id)

            # Розрахунок справедливого кф та EV
            fair_odds, ev, prob_pct = calculate_fair_odds_and_ev(home_xg, away_xg, best_odds)

            # Фільтр по EV та відправка
            if ev is not None and ev >= MIN_EV:
                print(f"✅ Знайдено валуй: {home_team} vs {away_team} (EV: +{ev}%)")
                send_telegram_alert(
                    home_team, away_team, home_xg, away_xg,
                    odds_league_key, best_bk_name, best_odds,
                    fair_odds, prob_pct, ev
                )

if __name__ == "__main__":
    scan_all_leagues()
