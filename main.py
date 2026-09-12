import os
import logging
import asyncio
import requests
from flask import Flask
from threading import Thread

# === НАЛАШТУВАННЯ ТА КЛЮЧІ З ЗМІННИХ СЕРЕДОВИЩА ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

MIN_EV = 5.0
MAX_EV = 25.0
MIN_ODDS = 1.20
MAX_ODDS = 3.20

MARKETS = "h2h,spreads,totals"
REGIONS = "eu,uk"

LEAGUES = [
    # Англія
    "soccer_epl", "soccer_efl_champ", "soccer_england_league1", "soccer_england_league2", "soccer_england_national_league",
    # Іспанія, Італія, Німеччина, Франція
    "soccer_spain_la_liga", "soccer_spain_segunda_division",
    "soccer_italy_serie_a", "soccer_italy_serie_b",
    "soccer_germany_bundesliga", "soccer_germany_bundesliga2", "soccer_germany_3liga",
    "soccer_france_lique_one", "soccer_france_lique_two",
    # Європа
    "soccer_netherlands_eredivisie", "soccer_netherlands_eerste_divisie",
    "soccer_portugal_primeira_liga", "soccer_portugal_liga2",
    "soccer_belgium_first_div",
    "soccer_austria_bundesliga", "soccer_austria_2_liga",
    "soccer_denmark_superliga", "soccer_denmark_1st_division",
    "soccer_norway_eliteserien", "soccer_norway_1st_division",
    "soccer_sweden_allsvenskan", "soccer_sweden_superettan",
    "soccer_poland_ekstraklasa", "soccer_poland_1_liga",
    "soccer_turkey_super_league", "soccer_turkey_1_lig",
    "soccer_greece_super_league",
    "soccer_scotland_premier_league",
    "soccer_switzerland_super_league",
    "soccer_croatia_hnl",
    "soccer_czech_republic_first_league",
    "soccer_romania_liga_1",
    "soccer_hungary_nb_i",
    "soccer_slovakia_super_liga",
    "soccer_slovenia_prva_liga",
    "soccer_cyprus_first_division",
    "soccer_israel_premier_league",
    "soccer_latvia_virsliga",
    "soccer_lithuania_a_lyga",
    "soccer_bulgaria_first_league",
    # Південна та Латинська Америка
    "soccer_argentina_primera_division", "soccer_argentina_primera_b",
    "soccer_brazil_campeonato", "soccer_brazil_serie_b", "soccer_brazil_serie_c",
    "soccer_chile_camp_nacional", "soccer_chile_primera_b",
    "soccer_colombia_categoria_primera_a",
    "soccer_ecuador_serie_a",
    "soccer_peru_liga_1",
    "soccer_paraguay_primera_division",
    "soccer_uruguay_primera_division", "soccer_uruguay_segunda_division",
    "soccer_mexico_ligamx",
    "soccer_conmebol_copa_libertadores", "soccer_conmebol_copa_sudamericana",
    # Інші регіони та Міжнародні
    "soccer_usa_mls",
    "soccer_saudi_prof_league",
    "soccer_japan_j_league",
    "soccer_korea_kleague1",
    "soccer_australia_aleague",
    "soccer_morocco_botola_pro",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league",
    "soccer_uefa_nations_league", "soccer_fifa_world_cup", "soccer_uefa_european_championship",
    "soccer_fifa_world_cup_qualifiers", "soccer_uefa_euro_qualifiers"
]

logging.basicConfig(level=logging.INFO)

# === ВЕБ-СЕРВЕР ДЛЯ RENDER (Keep-Alive) ===
app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# === ВІДПРАВКА ПОВІДОМЛЕНЬ В TELEGRAM ===
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable is missing!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            logging.error(f"Failed to send Telegram msg: {res.text}")
    except Exception as e:
        logging.error(f"Error sending Telegram message: {e}")

# === СКАНУВАННЯ МАТЧІВ ===
def check_value_bets():
    if not ODDS_API_KEY:
        logging.error("ODDS_API_KEY is missing!")
        return []

    signals = []
    
    for league in LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/?apiKey={ODDS_API_KEY}&regions={REGIONS}&markets={MARKETS}"
        
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            data = res.json()
        except Exception as e:
            logging.error(f"Error fetching {league}: {e}")
            continue

        for match in data:
            home = match.get('home_team')
            away = match.get('away_team')
            bookmakers = match.get('bookmakers', [])

            market_outcomes = {}

            for bm in bookmakers:
                for mk in bm.get('markets', []):
                    m_key = mk.get('key')
                    for out in mk.get('outcomes', []):
                        name = out.get('name')
                        price = out.get('price')
                        point = out.get('point', '')
                        
                        outcome_key = (m_key, name, point)
                        if outcome_key not in market_outcomes:
                            market_outcomes[outcome_key] = []
                        market_outcomes[outcome_key].append((bm['title'], price))

            for (m_key, name, point), prices in market_outcomes.items():
                if len(prices) < 3:
                    continue

                all_odds = [p[1] for p in prices]
                max_price = max(all_odds)
                avg_price = sum(all_odds) / len(all_odds)
                
                fair_prob = 1.0 / avg_price
                ev = (max_price * fair_prob - 1) * 100

                if MIN_EV <= ev <= MAX_EV and MIN_ODDS <= max_price <= MAX_ODDS:
                    best_bookies = [p[0] for p in prices if p[1] == max_price]
                    point_str = f" ({point})" if point != '' else ""
                    
                    msg = (
                        f"🚨 <b>VALUE BET FOUND!</b> 🚨\n\n"
                        f"⚽ <b>Матч:</b> {home} vs {away}\n"
                        f"🏆 <b>Ліга:</b> {league}\n"
                        f"🎯 <b>Маркет:</b> {m_key.upper()} - {name}{point_str}\n"
                        f"📈 <b>Кращий коефіцієнт:</b> <code>{max_price}</code>\n"
                        f"📊 <b>Середній коефіцієнт:</b> <code>{round(avg_price, 2)}</code>\n"
                        f"🔥 <b>EV (Перевага):</b> +{round(ev, 2)}%\n"
                        f"🏦 <b>Букмекери:</b> {', '.join(best_bookies)}"
                    )
                    signals.append(msg)

    return signals

async def main_loop():
    send_telegram_message("🤖 <b>Сканер валуїв успішно запущений і працює!</b>")
    while True:
        try:
            signals = check_value_bets()
            for sig in signals:
                send_telegram_message(sig)
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
        
        await asyncio.sleep(300)

if __name__ == '__main__':
    keep_alive()
    asyncio.run(main_loop())
