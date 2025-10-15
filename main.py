from datetime import datetime, timedelta
import sqlite3, secrets, httpx, asyncio, json, re
from typing import Optional, List
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DB = "picks.db"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams"

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
            over_under REAL,
            final_home_score INTEGER,
            final_away_score INTEGER,
            is_final BOOLEAN DEFAULT 0
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            join_date TEXT
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            game_id TEXT,
            pick_winner TEXT,
            pick_total TEXT,
            made_at TEXT,
            UNIQUE(user, game_id)
        );
        """)
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

def get_team_rank(team_dict):
    """
    Robustly extracts the team's poll rank from ESPN API JSON.
    Returns int if found, otherwise None.
    """
    # Check curatedRank: {'current': 1}
    curated_rank = team_dict.get("curatedRank", {})
    if isinstance(curated_rank, dict):
        rank_val = curated_rank.get("current")
        try:
            rank_int = int(rank_val)
            return rank_int
        except Exception:
            pass
    # Fallback fields
    for key in ["rank", "currentRank", "seed"]:
        val = team_dict.get(key)
        try:
            rank_int = int(val)
            return rank_int
        except Exception:
            continue
    # Try rankings list (AP poll usually first)
    rankings = team_dict.get("rankings", [])
    if rankings and isinstance(rankings, list):
        try:
            first_rank = rankings[0].get("rank")
            if first_rank is not None:
                return int(first_rank)
        except Exception:
            pass
    return None
async def fetch_games_with_ranked_teams_for_week():
    today = datetime.utcnow()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    date_range = f"{monday.strftime('%Y%m%d')}-{sunday.strftime('%Y%m%d')}"

    async with httpx.AsyncClient(timeout=20) as client:
        # Fetch current week’s games
        scoreboard_resp = await client.get(f"{ESPN_SCOREBOARD}?dates={date_range}")
        scoreboard_data = scoreboard_resp.json()

        # Fetch all FBS teams and extract ranks
        teams_resp = await client.get(ESPN_TEAMS)
        teams_data = teams_resp.json()
        ranked_teams = set()
        for item in teams_data.get("sports", [])[0].get("leagues", [])[0].get("teams", []):
            team = item.get("team", {})
            rank_info = team.get("rankings", []) or team.get("curatedRank")
            rank = None
            if isinstance(rank_info, list) and rank_info:
                rank = rank_info[0].get("rank")
            elif isinstance(rank_info, dict):
                rank = rank_info.get("current")

            try:
                rank = int(rank)
                if 1 <= rank <= 25:
                    ranked_teams.add(team.get("displayName"))
            except Exception:
                continue

    # Filter games
    games = []
    for ev in events:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            comps = comp.get("competitors", []) or []

            # Log teams and what get_rank() returns
            for c in comps:
                team = c.get("team") or {}
                name = team.get("displayName") or "Unknown"
                rank = get_rank(team)
                print(f"Team: {name} → get_rank: {rank}")

            # Skip bad or incomplete competitions
            if len(comps) != 2:
                continue
    
            home = next((c for c in comps if c.get("homeAway") == "home"), {})
            away = next((c for c in comps if c.get("homeAway") == "away"), {})
            home_team = home.get("team", {}).get("displayName")
            away_team = away.get("team", {}).get("displayName")

            # Only add if one of the teams is ranked
            if home_team in ranked_teams or away_team in ranked_teams:
                over_under = None
                odds_list = comp.get("odds") or []
                if odds_list:
                    try:
                        over_under = float(odds_list[0].get("overUnder"))
                    except (TypeError, ValueError):
                        over_under = None

            games.append({
                "home_team": home_team,
                "away_team": away_team,
                "over_under": over_under,
            })

    except Exception as e:
        print("⚠️ Error processing event:", e)
        continue

            games.append({
                "game_id": ev.get("id"),
                "short_name": ev.get("shortName"),
                "home_name": home_team,
                "away_name": away_team,
                "start_utc": comp.get("date"),
                "over_under": over_under
            })

    return games
def upsert_games(games: List[dict]):
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

async def update_scores_with_finals(date_range):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{ESPN_SCOREBOARD}?dates={date_range}")
        r.raise_for_status()
        data = r.json()
        for ev in data.get("events", []):
            gid = ev.get("id")
            comp = (ev.get("competitions") or [{}])[0]
            home = [c for c in comp.get("competitors", []) if c.get("homeAway") == "home"]
            away = [c for c in comp.get("competitors", []) if c.get("homeAway") == "away"]
            home_score = int(home[0]["score"]) if home and "score" in home[0] else None
            away_score = int(away[0]["score"]) if away and "score" in away[0] else None
            status = (comp.get("status") or {}).get("type", {})
            completed = bool(status.get("completed"))
            with db() as conn:
                conn.execute(
                    "UPDATE games SET final_home_score=?, final_away_score=?, is_final=? WHERE game_id=?",
                    (home_score, away_score, completed, gid)
                )
                conn.commit()

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/games")

@app.get("/games", response_class=HTMLResponse)
async def games(request: Request, user: Optional[str] = None):
    games = await fetch_games_with_ranked_teams_for_week()
    upsert_games(games)
    if user:
        with db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT game_id FROM picks WHERE user=?",
                (user,)
            )
            picked_game_ids = {row["game_id"] for row in c.fetchall()}
        games = [g for g in games if g["game_id"] not in picked_game_ids]
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
        c.execute("INSERT OR IGNORE INTO users (username, join_date) VALUES (?, ?)", (user, datetime.utcnow().isoformat()))
        try:
            c.execute("""
                INSERT INTO picks (user, game_id, pick_winner, pick_total, made_at)
                VALUES (?, ?, ?, ?, ?)
            """, (user, game_id, winner, pick_total, datetime.utcnow().isoformat()))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
    return RedirectResponse(url=f"/games?user={user}", status_code=303)

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, user: Optional[str] = None):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT game_id FROM games ORDER BY start_utc ASC LIMIT 15")
        top_game_ids = [row["game_id"] for row in c.fetchall()]
        if not top_game_ids:
            picks = []
        else:
            qmarks = ",".join("?" for _ in top_game_ids)
            query = f"""
                SELECT p.*, g.short_name, g.final_home_score, g.final_away_score, g.over_under, g.is_final
                FROM picks p
                LEFT JOIN games g ON g.game_id = p.game_id
                WHERE p.user=? AND p.game_id IN ({qmarks})
                ORDER BY p.made_at ASC
            """
            params = [user] + top_game_ids
            c.execute(query, params)
            picks = c.fetchall()

        total_picks = 0
        wins = 0
        current_streak = 0
        last_was_win = None

        for pick in picks:
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

        # Achievements logic
        badges = []
        if current_streak >= 5:
            c.execute(
                "INSERT OR IGNORE INTO achievements (username, badge, awarded_at) VALUES (?, ?, ?)",
                (user, "Streak5", datetime.utcnow().isoformat())
            )
            badges.append("Streak5")
        if win_rate >= 80 and total_picks >= 10:
            c.execute(
                "INSERT OR IGNORE INTO achievements (username, badge, awarded_at) VALUES (?, ?, ?)",
                (user, "Win80", datetime.utcnow().isoformat())
            )
            badges.append("Win80")
        c.execute("SELECT badge FROM achievements WHERE username=?", (user,))
        badges += [row["badge"] for row in c.fetchall() if row["badge"] not in badges]

    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "picks": picks,
            "win_rate": win_rate,
            "current_streak": current_streak,
            "badges": badges
        }
    )

@app.get("/groups", response_class=HTMLResponse)
def groups(request: Request, user: str = ""):
    with db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT g.* FROM groups g
            JOIN group_members gm ON gm.group_id = g.group_id
            WHERE gm.username = ?
        """, (user,))
        groups = c.fetchall()
    return templates.TemplateResponse("groups.html", {"request": request, "groups": groups, "user": user})

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

@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request):
    with db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT p.user, COUNT(*) as correct FROM picks p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.is_final = 1
            AND ((p.pick_winner = CASE WHEN g.final_home_score > g.final_away_score THEN 'home' ELSE 'away' END)
                 AND (p.pick_total = CASE WHEN g.final_home_score + g.final_away_score > g.over_under THEN 'over' ELSE 'under' END))
            GROUP BY p.user
            ORDER BY correct DESC
        """)
        board = c.fetchall()
    return templates.TemplateResponse("leaderboard.html", {"request": request, "board": board})

@app.post("/admin/update_scores")
async def admin_update_scores():
    today = datetime.utcnow()
    sat = next_saturday(today)
    sun = sat + timedelta(days=1)
    date_range = f"{sat.strftime('%Y%m%d')}-{sun.strftime('%Y%m%d')}"
    await update_scores_with_finals(date_range)
    return {"status": "game results updated"}
#hopefully this one works this time
@app.get("/debug/espn")
async def debug_espn():
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(ESPN_SCOREBOARD)
        data = r.json()
    return {"count": len(data.get("events", [])), "events": [e.get("shortName") for e in data.get("events", [])]}

@app.get("/debug/teamdata")
async def debug_teamdata():
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(ESPN_SCOREBOARD)
        data = r.json()

    sample = []
    for event in data.get("events", [])[:3]:  # only show first 3 games to keep output short
        comp = event.get("competitions", [{}])[0]
        teams = [t["team"] for t in comp.get("competitors", [])]
        for t in teams:
            sample.append({
                "displayName": t.get("displayName"),
                "abbrev": t.get("abbreviation"),
                "curatedRank": t.get("curatedRank"),
                "rankings": t.get("rankings"),
            })

    return sample
