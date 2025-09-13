import os, math, textwrap, requests
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional

from dotenv import load_dotenv
import pytz
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.ext._applicationbuilder import ApplicationBuilder

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret")
LOCAL_TZ = pytz.timezone(os.getenv("TZ", "America/Toronto"))

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {"mlb": "baseball_mlb", "nfl": "americanfootball_nfl"}

def today_local_date_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

def parse_date_arg(arg: str) -> str:
    if arg.lower() == "today":
        return today_local_date_str()
    datetime.strptime(arg, "%Y-%m-%d")
    return arg

def implied_prob_from_moneyline(ml: float) -> Optional[float]:
    try:
        ml = float(ml)
        return (-ml)/((-ml)+100.0) if ml < 0 else 100.0/(ml+100.0)
    except:
        return None

def to_local_date(dt_utc_str: str) -> str:
    dt_utc = datetime.fromisoformat(dt_utc_str.replace("Z", "+00:00"))
    return dt_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

def fetch_events(sport_key: str, date_str: str) -> List[Dict]:
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
        "dateFormat": "iso",
        "commenceTimeFrom": f"{date_str}T00:00:00Z",
        "commenceTimeTo": f"{date_str}T23:59:59Z",
    }
    r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds", params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def best_consensus_pick(event: Dict):
    home, away = event.get("home_team"), event.get("away_team")
    team_probs = {home: [], away: []}
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                p = implied_prob_from_moneyline(outcome.get("price"))
                if p is not None and outcome.get("name") in team_probs:
                    team_probs[outcome["name"]].append(p)
    av = {t:(sum(v)/len(v) if v else None) for t,v in team_probs.items()}
    if av.get(home) is not None and av.get(away) is not None:
        winner = home if av[home] > av[away] else away
        return winner, max(av[home], av[away]), av
    if av.get(home) is not None:
        return home, av[home], av
    if av.get(away) is not None:
        return away, av[away], av
    return None, None, av

def format_games(events: List[Dict], with_picks: bool) -> str:
    if not events: return "No games found for that date."
    lines = []
    for e in sorted(events, key=lambda x: x.get("commence_time", "")):
        start_local = to_local_date(e.get("commence_time"))
        home, away = e.get("home_team"), e.get("away_team")
        lines.append(f"• {away} @ {home} — {start_local}")
        if with_picks:
            pick, p, probs = best_consensus_pick(e)
            if pick and p is not None:
                fmt = lambda x: (f"{round(x*100,1)}%" if x is not None else "N/A")
                lines.append(f"    Pick: {pick}  (home {fmt(probs.get(home))} | away {fmt(probs.get(away))})  Confidence: {round(p*100,1)}%")
            else:
                lines.append("    Pick: Not enough odds data yet.")
    return "\n".join(lines)

def chunk_text(s: str, limit: int = 3800):
    out, cur = [], ""
    for ln in s.splitlines():
        if len(cur)+len(ln)+1 > limit:
            out.append(cur); cur = ln
        else:
            cur = (cur+"\n"+ln) if cur else ln
    if cur: out.append(cur)
    return out

# Telegram app
app_bot: Application = ApplicationBuilder().token(BOT_TOKEN).build()

async def cmd_start(update, context):
    msg = textwrap.dedent("""
    I list MLB/NFL games and predict winners.

    Commands:
    • /games <today|YYYY-MM-DD> <mlb|nfl>
    • /predict <today|YYYY-MM-DD> <mlb|nfl>
    """).strip()
    await update.message.reply_text(msg)

async def cmd_games(update, context):
    try:
        date_str, league = context.args[0], context.args[1].lower()
        events = fetch_events(SPORT_KEYS[league], parse_date_arg(date_str))
        out = f"{league.upper()} games for {date_str} ({LOCAL_TZ.zone}):\n\n" + format_games(events, False)
        for c in chunk_text(out): await update.message.reply_text(c)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_predict(update, context):
    try:
        date_str, league = context.args[0], context.args[1].lower()
        events = fetch_events(SPORT_KEYS[league], parse_date_arg(date_str))
        out = f"{league.upper()} predictions for {date_str} ({LOCAL_TZ.zone}):\n\n" + format_games(events, True)
        for c in chunk_text(out): await update.message.reply_text(c)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

app_bot.add_handler(CommandHandler("start", cmd_start))
app_bot.add_handler(CommandHandler("games", cmd_games))
app_bot.add_handler(CommandHandler("predict", cmd_predict))

# FastAPI for webhook
api = FastAPI()

@api.get("/")
async def health():
    return {"ok": True}

@api.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret")
    data = await request.json()
    update = Update.de_json(data, app_bot.bot)
    await app_bot.initialize()
    await app_bot.process_update(update)
    return {"ok": True}
