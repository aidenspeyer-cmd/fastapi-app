from datetime import datetime, timedelta
import sqlite3
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
        c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            user TEXT,
            game_id TEXT,
            pick_winner TEXT, -- 'home' or 'away'
            pick_total TEXT, -- 'over' or 'under'
            locked INTEGER DEFAULT 0,
            line_ou_at_pick REAL,
            created_at TEXT,
            PRIMARY KEY (user, game_id),
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );
        """)
        conn.commit()
init_db()

def next_saturday(date: datetime) -> datetime:
    # Saturday = 5
    dow = date.weekday()
    days_ahead = (5 - dow) % 7
    return date + timedelta(days=days_ahead)

async def fetch_top25_for_week() -> list[dict]:
    # ESPN defaults to Top-25 on this endpoint; we use Sat..Sun range
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

def is_locked(start_iso: str) -> bool:
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except Exception:
        return False
    return datetime.utcnow() >= start

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    games = await fetch_top25_for_week()
    upsert_games(games)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM games ORDER BY start_utc ASC")
        rows = c.fetchall()
    return templates.TemplateResponse("index.html", {"request": request, "games": rows})

@app.post("/submit")
async def submit(
    user: str = Form(...),
    payload: str = Form(...)
):
    lines = [l for l in payload.split("\n") if l.strip()]
    with db() as conn:
        c = conn.cursor()
        for line in lines:
            game_id, pick_winner, pick_total = line.split("|")
            c.execute("SELECT start_utc, over_under FROM games WHERE game_id=?", (game_id,))
            r = c.fetchone()
            if not r:
                continue
            if is_locked(r["start_utc"]):
                # skip late picks
                continue
            c.execute("""
            INSERT INTO picks (user, game_id, pick_winner, pick_total, locked, line_ou_at_pick, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user, game_id) DO UPDATE SET
                pick_winner=excluded.pick_winner,
                pick_total=excluded.pick_total,
                line_ou_at_pick=excluded.line_ou_at_pick
            """, (
                user.strip(), game_id, pick_winner, pick_total, 0, r["over_under"], datetime.utcnow().isoformat()
            ))
        conn.commit()
    return RedirectResponse(url=f"/board?user={user}", status_code=303)

async def fetch_scores_and_totals_for_scoring(date_range: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{ESPN_SCOREBOARD}?dates={date_range}")
        r.raise_for_status()
        data = r.json()
        out = {}
        for ev in data.get("events", []):
            gid = ev.get("id")
            comp = (ev.get("competitions") or [{}])[0]
            status = (comp.get("status") or {}).get("type", {})
            completed = status.get("completed") is True
            if not completed:
                continue
            total_points = 0
            for team in comp.get("competitors", []):
                try:
                    total_points += int(team.get("score"))
                except Exception:
                    pass
            winner_side = None
            for team in comp.get("competitors", []):
                if team.get("winner") is True:
                    winner_side = team.get("homeAway")
            out[gid] = {"total_points": total_points, "winner_side": winner_side}
        return out

@app.get("/board", response_class=HTMLResponse)
async def leaderboard(request: Request, user: str = ""):
    today = datetime.utcnow()
    sat = next_saturday(today)
    sun = sat + timedelta(days=1)
    date_range = f"{sat.strftime('%Y%m%d')}-{sun.strftime('%Y%m%d')}"
    finals = await fetch_scores_and_totals_for_scoring(date_range)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM picks")
        all_picks = c.fetchall()
        points_by_user = {}
        for p in all_picks:
            gid = p["game_id"]
            if gid not in finals:
                continue
            fin = finals[gid]
            u = p["user"]
            points_by_user.setdefault(u, 0)
            if p["pick_winner"] == fin["winner_side"]:
                points_by_user[u] += 1
            ou_line = p["line_ou_at_pick"]
            total_points = fin["total_points"]
            if ou_line is None:
                pass
            elif p["pick_total"] == "over" and total_points > ou_line:
                points_by_user[u] += 1
            elif p["pick_total"] == "under" and total_points < ou_line:
                points_by_user[u] += 1
        board = sorted(
            [{"user": u, "points": pts} for u, pts in points_by_user.items()],
            key=lambda r: (-r["points"], r["user"].lower())
        )
    return templates.TemplateResponse("board.html", {"request": request, "board": board, "me": user})
