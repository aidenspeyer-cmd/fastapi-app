from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import List, Dict

app = FastAPI()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

@app.get("/")
def read_root():
    return {"message": "Welcome to CFB 2025 Pick'Em!"}

# In-memory user picks {username: [pick, pick, ...]}
user_picks: Dict[str, List[dict]] = {}

class Pick(BaseModel):
    game_id: str
    prediction: str
    pick_time: str

def get_current_user(token: str = Depends(oauth2_scheme)):
    # Replace this with your own authentication method for real users
    # Here, we just interpret the token string as the username for demo purpose
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return token

@app.post("/predict")
async def make_pick(pick: Pick, user: str = Depends(get_current_user)):
    # Save pick for the current user in memory
    if user not in user_picks:
        user_picks[user] = []
    user_picks[user].append(pick.dict())
    return {"message": "Pick locked in!", "pick": pick}

@app.get("/profile/mypicks", response_model=List[Pick])
async def get_my_picks(user: str = Depends(get_current_user)):
    # Retrieve picks for the current user
    return user_picks.get(user, [])
