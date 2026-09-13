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
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY") # Ключ для реальної статистики xGз RapidAPI

if not TELEGRAM_BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN is missing!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

MIN_EV = 5.0          # Мінімальна перевага +5%
MAX_EV = 25.0         # Максимальна перевага (відсікає аномалії)
MIN_ODDS = 1.60       # Мінімальний кф
MAX_ODDS = 3.40       # Максимальний кф

MARKETS = "h2h"
REGIONS = "eu"

# ПОВНИЙ ТВІЙ СПИСОК ІЗ 22 ЛІГ (БЕЗ ЖОДНИХ СКОРОЧЕНЬ)
LEAGUES = [
    "soccer_epl", "soccer_england_championship", "soccer_england_league1", "soccer_england_league2",
    "soccer_england_efl_cup", "soccer_fa_cup", "soccer_spain_la_liga", "soccer_spain_segunda_division",
    "soccer_italy_serie_a", "soccer_italy_serie_b", "soccer_germany_bundesliga", "soccer_germany_bundesliga2",
    "soccer_france_lique_one", "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league", "soccer_uefa_nations_league", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_belgium_first_div", "soccer_turkey_super_league", "soccer_austria_bundesliga"
]

# Кеш надісланих сигналів (Анти-спам)
sent_signals_cache = set()

logging.basicConfig(level=logging.INFO)
app = Flask('')

@app.route('/')
def home():
    return "Poisson True Value Bot (Real xG 5 Games) is running!"

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

# === БЛОК ОТРЕМАНИЙ РЕАЛЬНОГО xG ЗА ОСТАННІ 5 МАТЧІВ ===

def get_team_last_5_xg(team_id):
    """
    Запитує останні 5 матчів команди через API-Football
    і повертає (середній забитий xG, середній пропущений xG).
    """
    if not API_FOOTBALL_KEY or not team_id:
        return 1.20, 1.20

    url = f"https://v3.football.api-sports.io/fixtures?team={team_id}&last=5"
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    
    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        fixtures = res.get('response', [])
        
        total_xg_for = 0.0
        total_xg_against = 0.0
        count = 0
        
        for fix in fixtures:
            fixture_id = fix['fixture']['id']
            stat_url = f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}"
            stat_res = requests.get(stat_url, headers=headers, timeout=10).json()
            
            teams_stat = stat_res.get('response', [])
            if len(teams_stat) < 2:
                continue
                
            for team_data in teams_stat:
                current_team_id = team_data['team']['id']
                xg_val = 0.0
                for stat in team_data.get('statistics', []):
                    if stat['type'] == 'expected_goals' and stat['value'] is not None:
                        xg_val = float(stat['value'])
                        break
                        
                if current_team_id == team_id:
                    total_xg_for += xg_val
                else:
                    total_xg_against += xg_val
            count += 1
            
        if count == 0:
            return 1.20, 1.20
            
        return total_xg_for / count, total_xg_against / count

    except Exception as e:
        logging.error(f"Помилка отримання xG для команди {team_id}: {e}")
        return 1.20, 1.20

def calculate_real_match_xg(home_team_id, away_team_id):
    """
    Перехресний розрахунок реального xG на основі останніх 5 ігор.
    """
    h_attack, h_defense = get_team_last_5_xg(home_team_id)
    a_attack, a_defense = get_team_last_5_xg(away_team_id)
    
    # Атака господарів vs Захист гостей з помірним фактором поля (+3% / -3%)
    real_xg_home = ((h_attack + a_defense) / 2.0) * 1.03
    real_xg_away = ((a_attack + h_defense) / 2.0) * 0.97
    
    # Запобігання аномальним вилетам за межі розумного
    real_xg_home = min(max(round(real_xg_home, 2), 0.40), 2.60)
    real_xg_away = min(max(round(real_xg_away, 2), 0.30), 2.40)
    
    return real_xg_home, real_xg_away

# === ЛОГІКА СКАНУВАННЯ З ХРОНОЛОГІЧНИМ СОРТУВАННЯМ ===

def run_manual_scan():
    if not ODDS_API_KEY:
        send_telegram_message("❌ <b>Помилка:</b> Відсутній ODDS_API_KEY!")
        return

    send_telegram_message("🔍 <b>Запущено сканування (Real xG 5 Games + Poisson)...</b>")
    
    signals_data = [] 
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
        except Exception:
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

            # ID команд беруться з відповідного джерела або маппінгу
            home_team_id = match.get('home_team_id', 0)
            away_team_id = match.get('away_team_id', 0)

            # РЕАЛЬНИЙ xG ЗА 5 МАТЧІВ ЗАМІСТЬ ФАКТИЧНОГО ОЦІНЮВАННЯ ВІД КФ
            xg_h, xg_a = calculate_real_match_xg(home_team_id, away_team_id)

            # Перераховуємо ймовірності Пуассона з реальним xG
            p_h, p_d, p_a = calculate_match_probabilities(xg_h, xg_a)

            model_probs = {home: p_h, "Draw": p_d, away: p_a}

            best_match_signal = None
            max_signal_ev = -999.0
            signal_key = None

            # Шукаємо 1 найкращий валуй на матч
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
                            cache_id = f"{home}_{away}_{name}_{bm_name}"
                            
                            if cache_id in sent_signals_cache:
                                continue

                            if ev > max_signal_ev:
                                max_signal_ev = ev
                                signal_key = cache_id
                                best_match_signal = (
                                    f"🎯 <b>TRUE POISSON VALUE BET (REAL xG)</b>\n\n"
                                    f"⚽ <b>Матч:</b> {home} vs {away}\n"
                                    f"📊 <b>Model xG (Last 5):</b> {xg_h} - {xg_a}\n"
                                    f"📅 <b>Час:</b> {formatted_time} (Кв)\n"
                                    f"🏆 <b>Ліга:</b> {league}\n"
                                    f"📌 <b>Ставка:</b> {name}\n"
                                    f"📈 <b>Коефіцієнт БК:</b> <code>{price}</code> ({bm_name})\n"
                                    f"⚖️ <b>Справедливий кф:</b> {fair_odds} ({round(model_prob * 100, 1)}%)\n"
                                    f"🔥 <b>Чистий EV:</b> +{round(ev, 2)}%"
                                )

            if best_match_signal and signal_key:
                signals_data.append({
                    'time': match_time_utc,
                    'text': best_match_signal,
                    'key': signal_key
                })

    if signals_data:
        # Хронологічне сортування від найближчих матчів до пізніших
        signals_data.sort(key=lambda x: x['time'])

        for item in signals_data:
            send_telegram_message(item['text'])
            sent_signals_cache.add(item['key'])

        send_telegram_message(f"✅ <b>Завершено!</b> Нових валуїв: {len(signals_data)}. Запитів API: {requests_made}")
    else:
        send_telegram_message(f"📭 <b>Завершено.</b> Нових валуїв не знайдено. Запитів API: {requests_made}")

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
