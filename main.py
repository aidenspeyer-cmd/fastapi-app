from datetime import datetime, timedelta
import sqlite3, secrets, httpx, re
from typing import Optional, List
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from jose import JWTError, jwt

# --- Config for JWT ---
SECRET_KEY = "super-secret-key"  # Set to a random secret string for production!
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DB = "picks.db"

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, join_date TEXT);"
        )
        # ... leave your other create-table cmds as is ...
        conn.commit()
init_db()

def get_password_hash(password):
    return pwd_context.hash(password)

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def create_access_token(username: str):
    to_encode = {"sub": username, "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        return username
    except JWTError:
        return None

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = c.fetchone()
        if user and verify_password(password, user["password"]):
            token = create_access_token(username)
            resp = RedirectResponse(url="/games", status_code=303)
            resp.set_cookie("access_token", token, httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES*60)
            return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials."})

@app.get("/register", response_class=HTMLResponse)
def register_get(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})

@app.post("/register", response_class=HTMLResponse)
def register_post(request: Request, username: str = Form(...), password: str = Form(...)):
    hashed = get_password_hash(password)
    with db() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (username, password, join_date) VALUES (?, ?, ?)", (username, hashed, datetime.utcnow().isoformat()))
            conn.commit()
            token = create_access_token(username)
            resp = RedirectResponse(url="/games", status_code=303)
            resp.set_cookie("access_token", token, httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES*60)
            return resp
        except sqlite3.IntegrityError:
            return templates.TemplateResponse("register.html", {"request": request, "error": "Username taken."})

@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("access_token")
    return resp

# Update routes to require login: sample for games route—
@app.get("/games", response_class=HTMLResponse)
async def games(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    # ... rest of your code, use 'user' variable as before ...
    games = await fetch_ap_top25_games_for_week()
    upsert_games(games)
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT game_id FROM picks WHERE user=?", (user,))
        picked_game_ids = {row["game_id"] for row in c.fetchall()}
    games = [g for g in games if g["game_id"] not in picked_game_ids]
    return templates.TemplateResponse("games.html", {"request": request, "games": games, "user": user})
