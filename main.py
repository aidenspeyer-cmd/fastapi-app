from datetime import datetime, timedelta
import sqlite3
from typing import Optional
import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DB = "picks.db"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        c = conn.cursor()
        # Games table
        c.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            short_name TEXT,
            home_id TEXT, home_name TEXT,
            away_id TEXT, away_name TEXT,
            start_utc TEXT,
            over_under REAL
        );
        """)
        # Users table - can expand with more fields later
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            join_date TEXT
        );
        """)
        # Picks table (now more detailed)
        c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,  -- Keep simple, username for now
            game_id TEXT,
            pick_winner TEXT,
            pick_total TEXT,
            made_at TEXT,
            UNIQUE(user, game_id),
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );
        """)
        # Groups and group membership can be added here later

        conn.commit()
init_db()

def next_saturday(date: datetime) -> datetime:
    dow = date.weekday()
    days_ahead = (5 - dow) % 7
    return date + timedelta(days=days_ahead)

async def fetch_top25_for_week() -> list[dict]:
    today = datetime.utcnow()
    sat = next_saturday(today)
    sun = sat + timedelta(days=1)
    date_range = f"{sat.strftime('%Y%m%d')}-{sun.strftime('%Y%m%d')}"
    url = f"{ESPN_SCOREBOARD}?dates={date_range}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        events = data.get("events", [])
        games = []
        for ev in events:
            gid = ev.get("id")
            comp = (ev.get("competitions") or [{}])[0]
            comps = comp.get("competitors", [])
            home = next((c for c in comps if c.get("homeAway") == "home"), {})
            away = next((c for c in comps if c.get("homeAway") == "away"), {})
            home_team = home.get("team", {})
            away_team = away.get("team", {})
            over_under = None
            odds_list = comp.get("odds") or []
            if odds_list:
                try:
                    over_under = float(odds_list[0].get("overUnder"))
                except (TypeError, ValueError):
                    over_under = None
            games.append({
                "game_id": gid,
                "short_name": ev.get("shortName"),
                "home_id": str(home_team.get("id")) if home_team else None,
                "home_name": home_team.get("displayName") if home_team else None,
                "away_id": str(away_team.get("id")) if away_team else None,
                "away_name": away_team.get("displayName") if away_team else None,
                "start_utc": comp.get("date"),
                "over_under": over_under
            })
        return games

def upsert_games(games: list[dict]):
    with db() as conn:
        c = conn.cursor()
        for g in games:
            c.execute("""
            INSERT INTO games (game_id, short_name, home_id, home_name, away_id, away_name, start_utc, over_under)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                short_name=excluded.short_name,
                home_id=excluded.home_id,
                home_name=excluded.home_name,
                away_id=excluded.away_id,
                away_name=excluded.away_name,
                start_utc=excluded.start_utc,
                over_under=excluded.over_under
            """, (
                g["game_id"], g["short_name"], g["home_id"], g["home_name"],
                g["away_id"], g["away_name"], g["start_utc"], g["over_under"]
            ))
        conn.commit()

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/games")

@app.get("/games", response_class=HTMLResponse)
async def games(request: Request, user: Optional[str] = None):
    games = await fetch_top25_for_week()
    upsert_games(games)
    return templates.TemplateResponse("games.html", {"request": request, "games": games, "user": user})

@app.post("/predict")
async def make_prediction(
    request: Request,
    user: str = Form(...),
    game_id: str = Form(...),
    winner: str = Form(...),
    pick_total: str = Form(...)
):
    with db() as conn:
        c = conn.cursor()
        # Optional: insert user if not exists
        c.execute("INSERT OR IGNORE INTO users (username, join_date) VALUES (?, ?)", (user, datetime.utcnow().isoformat()))
        c.execute("""
            INSERT INTO picks (user, game_id, pick_winner, pick_total, made_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user, game_id) DO UPDATE SET
                pick_winner=excluded.pick_winner,
                pick_total=excluded.pick_total,
                made_at=excluded.made_at
        """, (user, game_id, winner, pick_total, datetime.utcnow().isoformat()))
        conn.commit()
    return RedirectResponse(url=f"/games?user={user}", status_code=303)

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, user: Optional[str] = None):
    with db() as conn:
        c = conn.cursor()
        # Fetch all picks for this user, latest first
        c.execute("""
            SELECT p.*, g.short_name FROM picks p
            LEFT JOIN games g ON g.game_id = p.game_id
            WHERE p.user=?
            ORDER BY made_at ASC
        """, (user,))
        picks = c.fetchall()
        
        # Calculate stats
        total_picks = len(picks)
        wins = 0
        streak = 0
        last_pick_win = None

        # You need logic for "correct prediction" — here, assume you know the real outcome and it's in picks table (expand as you add that info)
        for pick in reversed(picks):  # latest first
            # For demo: treat home winning as correct winner, or check against actual results if stored
            actual_winner = "home"  # TODO: Replace with actual result for pick["game_id"]
            actual_ou = "over"      # TODO: Replace with result logic

            correct_pick = (pick["pick_winner"] == actual_winner) and (pick["pick_total"] == actual_ou)
            if correct_pick:
                wins += 1
                if streak == 0 or last_pick_win:
                    streak += 1
                last_pick_win = True
            else:
                last_pick_win = False
                streak = 0  # streak broken

        win_rate = int((wins / total_picks) * 100) if total_picks > 0 else 0

    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "picks": picks,
            "win_rate": win_rate,
            "current_streak": streak,  # Pass to template
            # Add other variables for badges/achievements as you expand!
        }
    )
@app.get("/groups", response_class=HTMLResponse)
def groups(request: Request):
    # Stub: Implement group logic here
    return templates.TemplateResponse("groups.html", {"request": request})

@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request):
    # Stub: compute and show leaderboard across all users or by group
    with db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT user, COUNT(*) as correct FROM picks
            GROUP BY user
            ORDER BY correct DESC
        """)
        board = c.fetchall()
    return templates.TemplateResponse("leaderboard.html", {"request": request, "board": board})
