import asyncio
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === НАЛАШТУВАННЯ ===
TELEGRAM_BOT_TOKEN = "8875783008:AAH9Rwps0VDDeSj85XnnW8ltR1xmQf0o2o8"
TELEGRAM_CHAT_ID = "284308454"
ODDS_API_KEY = "9b647f63fb6d25bfbca65851bc0ef8e2"

MIN_EV = 5.0      # %
MAX_EV = 25.0     # %
MIN_ODDS = 1.40
MAX_ODDS = 3.20

MARKETS = "h2h,spreads,totals"
REGIONS = "eu,uk"

LEAGUES = [
    "soccer_epl", "soccer_england_league1", "soccer_england_league2", "soccer_england_championship",
    "soccer_spain_la_liga", "soccer_spain_segunda_division", "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2", "soccer_italy_serie_a", "soccer_italy_serie_b",
    "soccer_france_ligue_one", "soccer_france_ligue_two", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_turkey_super_league", "soccer_belgium_first_div",
    "soccer_austria_bundesliga", "soccer_scotland_premiership", "soccer_switzerland_superleague",
    "soccer_denmark_superliga", "soccer_norway_eliteserien", "soccer_sweden_allsvenskan",
    "soccer_poland_ekstraklasa", "soccer_greece_super_league", "soccer_argentina_primera_division",
    "soccer_brazil_campeonato", "soccer_mexico_ligamx", "soccer_usa_mls",
    "soccer_chile_camp_nacional", "soccer_colombia_categoria_primera_a", "soccer_japan_j_league",
    "soccer_korea_k_league_1", "soccer_australia_aleague", "soccer_uefa_champs_league",
    "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league",
    "soccer_conmebol_copa_libertadores", "soccer_international_friendly"
]

sent_value_bets = set()
logging.basicConfig(level=logging.INFO)

def calculate_ev(price, fair_prob):
    return ((price * fair_prob) - 1) * 100

def fetch_and_scan_bets():
    found_bets = []
    for sport in LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={ODDS_API_KEY}&regions={REGIONS}&markets={MARKETS}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            data = res.json()
            for match in data:
                home_team = match.get("home_team")
                away_team = match.get("away_team")
                bookmakers = match.get("bookmakers", [])
                
                if len(bookmakers) < 3:
                    continue
                
                market_outcomes = {}
                for bm in bookmakers:
                    for mkt in bm.get("markets", []):
                        m_key = mkt.get("key")
                        for outcome in mkt.get("outcomes", []):
                            o_name = outcome.get("name")
                            o_point = outcome.get("point", "")
                            key = (m_key, o_name, o_point)
                            if key not in market_outcomes:
                                market_outcomes[key] = []
                            market_outcomes[key].append((bm.get("title"), outcome.get("price")))
                
                for (m_key, o_name, o_point), prices in market_outcomes.items():
                    if len(prices) < 3:
                        continue
                    all_prices = [p[1] for p in prices]
                    avg_price = sum(all_prices) / len(all_prices)
                    fair_prob = 1 / avg_price
                    
                    for bm_title, price in prices:
                        if not (MIN_ODDS <= price <= MAX_ODDS):
                            continue
                        
                        ev = calculate_ev(price, fair_prob)
                        if MIN_EV <= ev <= MAX_EV:
                            point_str = f" ({o_point})" if o_point != "" else ""
                            bet_id = f"{home_team}_{away_team}_{m_key}_{o_name}_{point_str}_{price}_{bm_title}"
                            
                            if bet_id in sent_value_bets:
                                continue
                            
                            sent_value_bets.add(bet_id)
                            msg = (
                                f"⚽️ **{home_team} vs {away_team}**\n"
                                f"🏆 Ліга: `{sport}`\n"
                                f"🎯 Ринок: `{m_key}` | **{o_name}{point_str}**\n"
                                f"📌 Букмекер: **{bm_title}**\n"
                                f"📊 Коефіцієнт: **{price}** (Сер. по ринку: {round(avg_price, 2)})\n"
                                f"🔥 Перевага (EV): **+{round(ev, 1)}%**"
                            )
                            found_bets.append(msg)
        except Exception as e:
            logging.error(f"Error scanning {sport}: {e}")
    return found_bets

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Сканую 48+ ліг (H2H, фори, тотали)...")
    bets = fetch_and_scan_bets()
    if not bets:
        await update.message.reply_text("Наразі валуйних ставок (EV 5–15%, кф 1.40–3.20) не знайдено.")
    else:
        for bet in bets:
            await update.message.reply_text(bet, parse_mode="Markdown")

async def auto_scanner_loop(app):
    while True:
        try:
            bets = fetch_and_scan_bets()
            for bet in bets:
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⚡️ **ЗНАЙДЕНО ВАЛУЙ!**\n\n{bet}", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error in scanner loop: {e}")
        await asyncio.sleep(300)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("scan", scan_command))
    
    loop = asyncio.get_event_loop()
    loop.create_task(auto_scanner_loop(app))
    
    print("Бот працює цілодобово...")
    app.run_polling()
