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
        # Updated games table with all the fields needed
        c.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            short_name TEXT,
            home_id TEXT,
            home_name TEXT,
            away_id TEXT,
            away_name TEXT,
            start_utc TEXT,
            over_under REAL,
            final_home_score INTEGER,
            final_away_score INTEGER,
            is_final BOOLEAN DEFAULT 0
        );
        """)
        # ... (users, picks, groups, etc. unchanged) ...
        # Ensure the rest of your tables are created here as well!
 
        c.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT UNIQUE,
            access_code TEXT,
            created_by TEXT
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER,
            username TEXT,
            joined_at TEXT,
            PRIMARY KEY (group_id, username)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            badge TEXT,
            awarded_at TEXT,
            UNIQUE(username, badge)
        );
        """)
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
        if current_streak >= 5:
        c.execute(
            "INSERT OR IGNORE INTO achievements (username, badge, awarded_at) VALUES (?, ?, ?)",
            (user, "Streak5", datetime.utcnow().isoformat())
        )
    # Award a win% badge
    if win_rate >= 80 and total_picks >= 10:
        c.execute(
            "INSERT OR IGNORE INTO achievements (username, badge, awarded_at) VALUES (?, ?, ?)",
            (user, "Win80", datetime.utcnow().isoformat())
        )
        conn.commit()
    return RedirectResponse(url=f"/games?user={user}", status_code=303)

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, user: Optional[str] = None):
    with db() as conn:
        c = conn.cursor()
        # Fetch all picks for this user (oldest first, to calculate streak correctly)
        c.execute("""
            SELECT p.*, g.short_name, g.final_home_score, g.final_away_score, g.over_under, g.is_final
            FROM picks p
            LEFT JOIN games g ON g.game_id = p.game_id
            WHERE p.user=?
            ORDER BY p.made_at ASC
        """, (user,))
        c.execute("SELECT badge FROM achievements WHERE username=?", (user,))
        badges = [row["badge"] for row in c.fetchall()]
# Pass `badges` to your template for display
        picks = c.fetchall()

        total_picks = 0
        wins = 0
        streak = 0
        current_streak = 0
        last_was_win = None

        for pick in picks:
            # Only count games that are final
            if not pick["is_final"]:
                continue
            total_picks += 1
            home = pick["final_home_score"]
            away = pick["final_away_score"]
            if home is None or away is None:
                continue
            actual_winner = "home" if home > away else "away"
            total_points = (home or 0) + (away or 0)
            ou_result = "over" if total_points > (pick["over_under"] or 0) else "under"

            correct = (pick["pick_winner"] == actual_winner) and (pick["pick_total"] == ou_result)
            if correct:
                wins += 1
                if last_was_win or last_was_win is None:
                    current_streak += 1
                else:
                    current_streak = 1
                last_was_win = True
            else:
                last_was_win = False
                current_streak = 0

        win_rate = int((wins / total_picks) * 100) if total_picks > 0 else 0

    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "picks": picks,
            "win_rate": win_rate,
            "current_streak": current_streak
        }
    )
    import secrets

@app.post("/groups/create")
def create_group(request: Request, group_name: str = Form(...), user: str = "anonymous"):
    access_code = secrets.token_hex(3)
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO groups (group_name, access_code, created_by) VALUES (?, ?, ?)",
            (group_name, access_code, user)
        )
        group_id = c.lastrowid
        c.execute(
            "INSERT INTO group_members (group_id, username, joined_at) VALUES (?, ?, ?)",
            (group_id, user, datetime.utcnow().isoformat())
        )
        conn.commit()
    return RedirectResponse(url="/groups", status_code=303)

@app.get("/groups", response_class=HTMLResponse)
def groups(request: Request, user: str = ""):
    with db() as conn:
        c = conn.cursor()
        # Example: show all groups for the logged-in user
        c.execute("""
            SELECT g.* FROM groups g
            JOIN group_members gm ON gm.group_id = g.group_id
            WHERE gm.username = ?
        """, (user,))
        groups = c.fetchall()
    return templates.TemplateResponse("groups.html", {"request": request, "groups": groups, "user": user})

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
