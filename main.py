import os
import requests
import math

# Ключі з Render Environment Variables
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"
HEADERS_FOOTBALL = {'x-apisports-key': API_FOOTBALL_KEY}

# --- ФІЛЬТРИ СТРАТЕГІЇ ---
MIN_ODDS = 1.50      # Мінімальний коефіцієнт БК
MAX_ODDS = 4.50      # Максимальний коефіцієнт БК
MIN_EV = 5.0         # Мінімальний чистий EV (%) для відправки алерта

# Словник-мапінг усіх 22 ліг (Odds API Key -> API-Football League ID)
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

def get_team_xg_by_league(team_name: str, league_id: int) -> float:
    """Отримує середній xG/голи команди за 5 останніх матчів у лізі"""
    try:
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
        print(f"Error fetching xG for {team_name}: {e}")
        return 1.20

def poisson_probability(lmbda: float, k: int) -> float:
    """Обчислення ймовірності за формулою Пуассона"""
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_ev(home_xg: float, away_xg: float, bookmaker_odds: float):
    """Розрахунок справедливого кф та EV на П1"""
    home_win_prob = 0.0
    for h in range(0, 6):
        for a in range(0, 6):
            if h > a:
                home_win_prob += poisson_probability(home_xg, h) * poisson_probability(away_xg, a)
                
    if home_win_prob <= 0:
        return None, None

    fair_odds = round(1 / home_win_prob, 2)
    ev = round(((bookmaker_odds / fair_odds) - 1) * 100, 2)
    return fair_odds, ev

def scan_all_22_leagues():
    """Сканує всі 22 ліги та відсіює матчі за фільтрами кф і EV"""
    for odds_league_key, fb_league_id in LEAGUES_MAP.items():
        odds_url = f"https://api.the-odds-api.com/v4/sports/{odds_league_key}/odds/"
        odds_res = requests.get(
            odds_url,
            params={
                'apiKey': ODDS_API_KEY,
                'regions': 'eu',
                'markets': 'h2h'
            },
            timeout=10
        ).json()
        
        if not isinstance(odds_res, list):
            continue
            
        for match in odds_res:
            home_team = match['home_team']
            away_team = match['away_team']
            
            # Отримання кращого коефіцієнта на П1 з букмекерів
            bookmakers = match.get('bookmakers', [])
            if not bookmakers:
                continue
                
            # Беремо перший доступний кф на П1 (або шукаємо найвищий)
            bm = bookmakers[0]
            outcomes = bm['markets'][0]['outcomes']
            bk_odds_home = next((o['price'] for o in outcomes if o['name'] == home_team), None)
            
            if not bk_odds_home:
                continue

            # 1. ФІЛЬТР ПО КОЕФІЦІЄНТУ
            if not (MIN_ODDS <= bk_odds_home <= MAX_ODDS):
                continue

            # Розрахунок xG та EV
            home_xg = get_team_xg_by_league(home_team, fb_league_id)
            away_xg = get_team_xg_by_league(away_team, fb_league_id)
            fair_odds, ev = calculate_ev(home_xg, away_xg, bk_odds_home)

            # 2. ФІЛЬТР ПО EV
            if ev is not None and ev >= MIN_EV:
                print(f"🔥 ВАЛУЙ ЗНАЙДЕНО: {home_team} vs {away_team} | Кф БК: {bk_odds_home} | Справедливий кф: {fair_odds} | EV: +{ev}%")

if __name__ == "__main__":
    scan_all_22_leagues()
