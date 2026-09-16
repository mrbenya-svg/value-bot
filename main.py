import os
import math
import requests
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask
import telebot

# ================================
# 0. ВЕБ-СЕРВЕР ДЛЯ RENDER ТА KEEP-ALIVE
# ================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Value Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

threading.Thread(target=run_flask, daemon=True).start()

def keep_alive():
    """Фоновий ping через зовнішнє посилання Render для запобігання 'засинанню'"""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://value-bot.onrender.com")
    
    time.sleep(15) 
    
    while True:
        try:
            response = requests.get(render_url, timeout=10)
            print(f"⏰ [Keep-Alive] External ping sent. Status: {response.status_code}")
        except Exception as e:
            print(f"⚠️ [Keep-Alive] External ping error: {e}")
            
        time.sleep(600)

threading.Thread(target=keep_alive, daemon=True).start()

# ================================
# 1. КОНФІГУРАЦІЯ
# ================================
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

MIN_ODDS = 1.60
MAX_ODDS = 3.40
MIN_EV = 5.0
MAX_HOURS_AHEAD = 48

LEAGUES_MAP = {
    "soccer_epl": "Англія: Прем'єр-ліга",
    "soccer_spain_la_liga": "Іспанія: Ла Ліга",
    "soccer_germany_bundesliga": "Німеччина: Бундесліга",
    "soccer_italy_serie_a": "Італія: Серія А",
    "soccer_france_ligue_one": "Франція: Ліга 1",
    "soccer_england_championship": "Англія: Чемпіоншип",
    "soccer_england_league1": "Англія: Перша ліга",
    "soccer_england_league2": "Англія: Друга ліга",
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
    "soccer_spain_segunda_division": "Іспанія: Сегунда",
    "soccer_germany_bundesliga2": "Німеччина: Друга Бундесліга"
}

# ================================
# 2. МАТЕМАТИЧНА МОДЕЛЬ
# ================================
def poisson_probability(lmbda: float, k: int) -> float:
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_full_poisson_model(avg_h, avg_d, avg_a):
    margin = (1 / avg_h) + (1 / avg_d) + (1 / avg_a)
    p_h_clean = (1 / avg_h) / margin
    p_d_clean = (1 / avg_d) / margin
    p_a_clean = (1 / avg_a) / margin

    dampened_total = max(1.85, min(3.10, 2.55 - 1.35 * (p_d_clean - 0.26)))
    
    share_h = p_h_clean / (p_h_clean + p_a_clean)
    lmbda_h = dampened_total * share_h
    lmbda_a = dampened_total * (1 - share_h)

    p_win_h, p_draw, p_win_a = 0.0, 0.0, 0.0
    for h in range(6):
        for a in range(6):
            prob = poisson_probability(lmbda_h, h) * poisson_probability(lmbda_a, a)
            if h > a:
                p_win_h += prob
            elif h == a:
                p_draw += prob
            else:
                p_win_a += prob

    fair_p1 = round(1 / p_win_h, 2) if p_win_h > 0 else 99.0
    fair_p2 = round(1 / p_win_a, 2) if p_win_a > 0 else 99.0

    return {
        'xg_h': round(lmbda_h, 2),
        'xg_a': round(lmbda_a, 2),
        'P1': {'fair_odds': fair_p1, 'prob': round(p_win_h * 100, 1)},
        'P2': {'fair_odds': fair_p2, 'prob': round(p_win_a * 100, 1)}
    }

def format_match_time(iso_time_str: str) -> str:
    try:
        # Перетворюємо час з ISO (UTC) у київський часовий пояс (Europe/Kyiv)
        dt_utc = datetime.fromisoformat(iso_time_str.replace("Z", "+00:00"))
        dt_kyiv = dt_utc.astimezone(ZoneInfo("Europe/Kyiv"))
        return dt_kyiv.strftime("%d.%m о %H:%M")
    except Exception:
        return "Час невідомий"

# ================================
# 3. СКАНУВАННЯ ТА ХРОНОЛОГІЧНЕ СОРТУВАННЯ
# ================================
def run_scan_and_notify(chat_id):
    bot.send_message(chat_id, "🔎 <b>Запуск сканера (хронологічний порядок)...</b>", parse_mode="HTML")
    
    valuable_matches = []
    now_utc = datetime.now(timezone.utc)

    for odds_league_key, league_title in LEAGUES_MAP.items():
        try:
            res_raw = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{odds_league_key}/odds/",
                params={'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'},
                timeout=10
            )

            if res_raw.status_code in [401, 429]:
                bot.send_message(
                    chat_id,
                    f"⚠️ <b>Помилка Odds API (Код {res_raw.status_code}):</b>\n"
                    f"Вичерпано ліміт запитів або вказано невірний API Key.\n"
                    f"<i>Деталі: {res_raw.text}</i>",
                    parse_mode="HTML"
                )
                return

            if res_raw.status_code != 200:
                print(f"Помилка Odds API ({odds_league_key}): Статус {res_raw.status_code}")
                continue

            odds_res = res_raw.json()
        except Exception as e:
            bot.send_message(chat_id, f"❌ <b>Критична помилка мережі:</b> {e}", parse_mode="HTML")
            return

        if not isinstance(odds_res, list) or not odds_res:
            continue

        for match in odds_res:
            commence_str = match.get('commence_time', '')
            match_dt = None
            if commence_str:
                try:
                    match_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                    if match_dt - now_utc > timedelta(hours=MAX_HOURS_AHEAD):
                        continue
                except Exception:
                    pass

            if not match_dt:
                match_dt = now_utc + timedelta(days=99)

            home_team = match['home_team']
            away_team = match['away_team']
            match_time_formatted = format_match_time(commence_str)

            home_odds_all, draw_odds_all, away_odds_all = [], [], []
            max_h_odds, max_a_odds = 0.0, 0.0
            best_bk_h, best_bk_a = "", ""

            for bm in match.get('bookmakers', []):
                for market in bm.get('markets', []):
                    if market['key'] == 'h2h':
                        h_p, d_p, a_p = None, None, None
                        for outcome in market.get('outcomes', []):
                            if outcome['name'] == home_team:
                                h_p = outcome['price']
                            elif outcome['name'] == away_team:
                                a_p = outcome['price']
                            else:
                                d_p = outcome['price']

                        if h_p and d_p and a_p:
                            home_odds_all.append(h_p)
                            draw_odds_all.append(d_p)
                            away_odds_all.append(a_p)
                            if h_p > max_h_odds:
                                max_h_odds, best_bk_h = h_p, bm['title']
                            if a_p > max_a_odds:
                                max_a_odds, best_bk_a = a_p, bm['title']

            if not home_odds_all:
                continue

            avg_h = sum(home_odds_all) / len(home_odds_all)
            avg_d = sum(draw_odds_all) / len(draw_odds_all)
            avg_a = sum(away_odds_all) / len(away_odds_all)

            model = calculate_full_poisson_model(avg_h, avg_d, avg_a)

            # Перевірка П1
            if MIN_ODDS <= max_h_odds <= MAX_ODDS:
                ev_p1 = round(((max_h_odds / model['P1']['fair_odds']) - 1) * 100, 2)
                if ev_p1 >= MIN_EV:
                    msg = (
                        f"🎯 <b>VALUE BET FOUND</b>\n\n"
                        f"⚽️ <b>Матч:</b> {home_team} vs {away_team}\n"
                        f"📅 <b>Час:</b> {match_time_formatted}\n"
                        f"🏆 <b>Ліга:</b> {league_title}\n"
                        f"📊 <b>Оціночний xG:</b> {model['xg_h']} - {model['xg_a']}\n"
                        f"📌 <b>Ставка:</b> {home_team} (П1)\n"
                        f"📈 <b>Макс. кф БК:</b> {max_h_odds} ({best_bk_h})\n"
                        f"⚖️ <b>Fair Odds:</b> {model['P1']['fair_odds']} ({model['P1']['prob']}%)\n"
                        f"🔥 <b>EV:</b> +{ev_p1}%"
                    )
                    valuable_matches.append({'match_time': match_dt, 'msg': msg})

            # Перевірка П2
            if MIN_ODDS <= max_a_odds <= MAX_ODDS:
                ev_p2 = round(((max_a_odds / model['P2']['fair_odds']) - 1) * 100, 2)
                if ev_p2 >= MIN_EV:
                    msg = (
                        f"🎯 <b>VALUE BET FOUND</b>\n\n"
                        f"⚽️ <b>Матч:</b> {home_team} vs {away_team}\n"
                        f"📅 <b>Час:</b> {match_time_formatted}\n"
                        f"🏆 <b>Ліга:</b> {league_title}\n"
                        f"📊 <b>Оціночний xG:</b> {model['xg_h']} - {model['xg_a']}\n"
                        f"📌 <b>Ставка:</b> {away_team} (П2)\n"
                        f"📈 <b>Макс. кф БК:</b> {max_a_odds} ({best_bk_a})\n"
                        f"⚖️ <b>Fair Odds:</b> {model['P2']['fair_odds']} ({model['P2']['prob']}%)\n"
                        f"🔥 <b>EV:</b> +{ev_p2}%"
                    )
                    valuable_matches.append({'match_time': match_dt, 'msg': msg})

    valuable_matches.sort(key=lambda item: item['match_time'])

    for val_item in valuable_matches:
        bot.send_message(chat_id, val_item['msg'], parse_mode="HTML")

    if not valuable_matches:
        bot.send_message(chat_id, "🏁 Завершено. Валуїв з кф 1.60-3.40 та EV >= 5.0% не знайдено.")
    else:
        bot.send_message(chat_id, f"✅ Завершено. Знайдено валуйних сигналів: {len(valuable_matches)}")

# ================================
# 4. TELEGRAM HANDLERS
# ================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Надішли 'скан' або /scan для запуску.\nКоманда /quota покаже залишок ліміту API.")

@bot.message_handler(commands=['quota'])
def check_quota(message):
    try:
        res = requests.get(
            "https://api.the-odds-api.com/v4/sports/",
            params={'apiKey': ODDS_API_KEY},
            timeout=5
        )
        remaining = res.headers.get('x-requests-remaining', 'Невідомо')
        used = res.headers.get('x-requests-used', 'Невідомо')
        
        if res.status_code == 200:
            msg = f"📊 <b>Статус Odds API:</b>\n\n✅ Використано запитів: <b>{used}</b>\n🔋 Залишилось запитів: <b>{remaining}</b>"
        else:
            msg = f"⚠️ Помилка ключа (Код {res.status_code}):\n{res.text}"
            
        bot.reply_to(message, msg, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка з'єднання: {e}")

@bot.message_handler(func=lambda message: message.text.lower() in ['скан', '/scan'])
def handle_scan_request(message):
    run_scan_and_notify(message.chat.id)

if __name__ == "__main__":
    print("🤖 Бот чекає команду 'скан'...")
    bot.infinity_polling()
