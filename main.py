# Create project files and zip them for download
import os, textwrap, zipfile, json, pathlib

root = "data/top25-pickem"
templates_dir = os.path.join(root, "templates")
static_dir = os.path.join(root, "static")
os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

app_py = r'''
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
            pick_winner TEXT,     -- 'home' or 'away'
            pick_total TEXT,      -- 'over' or 'under'
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
    lines = [l for l in payload.split("\\n") if l.strip()]
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
'''

base_html = r'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Top-25 Pick’em</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="/static/style.css" rel="stylesheet">
</head>
<body class="min-h-screen bg-neutral-50 text-neutral-900">
  <main class="max-w-4xl mx-auto p-4">
    <h1 class="text-2xl font-semibold mb-4">Top-25 CFB Pick’em</h1>
    {% block content %}{% endblock %}
  </main>
</body>
</html>
'''

index_html = r'''{% extends "base.html" %}
{% block content %}
<form method="post" action="/submit" id="pick-form" class="space-y-4">
  <div class="flex items-center gap-2">
    <label class="text-sm">Your name</label>
    <input name="user" required placeholder="e.g., Wyatt" class="border px-3 py-2 rounded w-48">
  </div>

  <div class="text-sm text-neutral-600">Pick outright winner + Over/Under for each game. Picks lock at kickoff.</div>

  <input type="hidden" name="payload" id="payload">
  <div class="space-y-3">
    {% for g in games %}
    <div class="rounded border bg-white p-3">
      <div class="flex items-center justify-between">
        <div class="font-medium">{{ g["short_name"] or (g["away_name"] ~ " @ " ~ g["home_name"]) }}</div>
        <div class="text-xs text-neutral-600">
          O/U: {{ "%.1f"|format(g["over_under"] or 0.0) }}
        </div>
      </div>
      <div class="mt-2 grid grid-cols-2 gap-2">
        <label class="border rounded p-2 flex items-center gap-2">
          <input type="radio" name="winner-{{ g['game_id'] }}" value="away" required>
          <span>{{ g["away_name"] }}</span>
        </label>
        <label class="border rounded p-2 flex items-center gap-2">
          <input type="radio" name="winner-{{ g['game_id'] }}" value="home" required>
          <span>{{ g["home_name"] }}</span>
        </label>
      </div>
      <div class="mt-2 grid grid-cols-2 gap-2">
        <label class="border rounded p-2 flex items-center gap-2">
          <input type="radio" name="ou-{{ g['game_id'] }}" value="over" required>
          <span>Over</span>
        </label>
        <label class="border rounded p-2 flex items-center gap-2">
          <input type="radio" name="ou-{{ g['game_id'] }}" value="under" required>
          <span>Under</span>
        </label>
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="flex gap-3">
    <button class="px-4 py-2 rounded bg-black text-white">Submit Picks</button>
    <a href="/board" class="px-4 py-2 rounded border">Leaderboard</a>
  </div>
</form>

<script>
  const form = document.getElementById('pick-form');
  form.addEventListener('submit', (e) => {
    const payload = [];
    {% for g in games %}
    const w = document.querySelector('input[name="winner-{{ g["game_id"] }}"]:checked');
    const o = document.querySelector('input[name="ou-{{ g["game_id"] }}"]:checked');
    if (w && o) payload.push(`{{ g["game_id"] }}|${w.value}|${o.value}`);
    {% endfor %}
    document.getElementById('payload').value = payload.join('\\n');
  });
</script>
{% endblock %}
'''

board_html = r'''{% extends "base.html" %}
{% block content %}
<h2 class="text-xl font-medium mb-3">Leaderboard</h2>
<table class="w-full border-collapse">
  <thead>
    <tr class="text-left border-b">
      <th class="py-2">User</th>
      <th class="py-2">Points</th>
    </tr>
  </thead>
  <tbody>
    {% for row in board %}
    <tr class="border-b">
      <td class="py-2">{{ row.user }}{% if row.user == me %} (you){% endif %}</td>
      <td class="py-2">{{ row.points }}</td>
    </tr>
    {% else %}
    <tr><td class="py-4 text-neutral-500" colspan="2">No scored picks yet.</td></tr>
    {% endfor %}
  </tbody>
</table>
<a href="/" class="inline-block mt-4 px-4 py-2 rounded border">Back to picks</a>
{% endblock %}
'''

style_css = r'''* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
.border { border: 1px solid rgba(0,0,0,.12); }
.rounded { border-radius: .5rem; }
.bg-white { background: #fff; }
.bg-neutral-50 { background: #fafafa; }
.text-neutral-900 { color: #111; }
.text-neutral-600 { color: #666; }
.text-xs { font-size: .8rem; }
.text-sm { font-size: .9rem; }
.text-xl { font-size: 1.25rem; }
.text-2xl { font-size: 1.5rem; }
.font-medium { font-weight: 600; }
.font-semibold { font-weight: 600; }
.min-h-screen { min-height: 100vh; }
.max-w-4xl { max-width: 64rem; }
.mx-auto { margin-left: auto; margin-right: auto; }
.p-3 { padding: .75rem; } .p-4 { padding: 1rem; }
.py-2 { padding-top: .5rem; padding-bottom: .5rem; }
.mt-2 { margin-top: .5rem; } .mb-3 { margin-bottom: .75rem; } .mb-4 { margin-bottom: 1rem; }
.space-y-3 > * + * { margin-top: .75rem; }
.space-y-4 > * + * { margin-top: 1rem; }
.grid { display: grid; } .grid-cols-2 { grid-template-columns: repeat(2,minmax(0,1fr)); }
.gap-2 { gap: .5rem; } .gap-3 { gap: .75rem; }
.flex { display: flex; } .items-center { align-items: center; } .justify-between { justify-content: space-between; }
.w-full { width: 100%; } .w-48 { width: 12rem; }
.inline-block { display: inline-block; }
.bg-black { background: #000; }
.text-white { color: #fff; }
.px-3 { padding-left: .75rem; padding-right: .75rem; }
.px-4 { padding-left: 1rem; padding-right: 1rem; }
.rounded { border-radius: .5rem; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
button { cursor: pointer; }
table { width: 100%; }
'''

requirements_txt = r'''fastapi
uvicorn[standard]
httpx
jinja2
python-multipart
'''

procfile = r'''web: uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}'''

readme_md = r'''# Top-25 CFB Pick’em (FastAPI)

A tiny web app for you and friends to pick **outright winners** and **over/under** on ESPN Top-25 college football games each week. Stores picks in SQLite and scores automatically after finals.

## Quick start (local)

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload'''
