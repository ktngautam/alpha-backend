from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
from dotenv import load_dotenv 
from supabase import create_client, Client
from datetime import datetime, timedelta
import google.generativeai as genai
import tweepy
import asyncio

load_dotenv()

app = FastAPI(title="AlphaBot Backend")

# CORS (allow Vercel frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://alphabot-ashen.vercel.app",  # Your actual Vercel frontend
        "http://localhost:3000",  # For local development
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Env vars
X_CLIENT_ID = os.getenv("X_CLIENT_ID")
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "https://alpha-backend-production.up.railway.app/api/auth/x/callback")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # Use service_role key for backend
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)

@app.get("/api/auth/x/start")
async def start_auth():
    auth_url = (
        "https://x.com/i/oauth2/authorize?"
        f"response_type=code&"
        f"client_id={X_CLIENT_ID}&"
        f"redirect_uri={REDIRECT_URI}&"
        f"scope=tweet.read%20tweet.write%20users.read%20offline.access&"
        f"state=state&code_challenge=challenge&code_challenge_method=plain"
    )
    return JSONResponse({"url": auth_url})

@app.get("/api/auth/x/callback")
async def auth_callback(request: Request, code: str = None, state: str = None):
    if not code:
        raise HTTPException(400, "No code provided")
    
    # Exchange code for tokens
    token_url = "https://api.x.com/2/oauth2/token"
    auth = httpx.BasicAuth(X_CLIENT_ID, X_CLIENT_SECRET)
    data = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": X_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": "challenge",  # PKCE (simple for demo)
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=data, auth=auth)
        if resp.status_code != 200:
            raise HTTPException(400, f"Token exchange failed: {resp.text}")
        
        tokens = resp.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        
        # Get user info
        headers = {"Authorization": f"Bearer {access_token}"}
        user_resp = await client.get("https://api.x.com/2/users/me", headers=headers)
        if user_resp.status_code != 200:
            raise HTTPException(400, "Failed to fetch user")
        
        user_data = user_resp.json()["data"]
        username = user_data["username"]
        x_id = user_data["id"]
        
        # Save to Supabase
        db_data = {
            "x_id": x_id,
            "username": username,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "next_post_at": (datetime.utcnow() + timedelta(hours=1)).isoformat(),  # First post soon
            "active": True
        }
        result = supabase.table("users").upsert(db_data).execute()
        if not result.data:
            raise HTTPException(500, "Failed to save user")
    
    # Redirect to frontend
    frontend_url = f"https://alphabot-ashen.vercel.app?activated=true&user={username}"
    return RedirectResponse(frontend_url, status_code=302)

async def generate_tweet(username: str) -> str:
    model = genai.GenerativeModel('gemini-2.5-flash')
    prompt = f"Write ONE tweet (max 270 chars) in @{username}'s alpha/success style. ALL CAPS power words. End with 🐺."
    resp = await asyncio.to_thread(model.generate_content, prompt)  # Async fix
    tweet = resp.text.strip().replace("```", "")
    return tweet[:270]

async def post_for_user(user: dict):
    # Tweepy with OAuth 2.0 bearer (2025 compatible)
    client = tweepy.Client(user.get("access_token"))
    tweet = await generate_tweet(user["username"])
    try:
        response = client.create_tweet(text=tweet, user_auth=False)
        print(f"Posted for @{user['username']}: {tweet} (ID: {response.data['id']})")
    except Exception as e:
        print(f"Error: {e}")
        # Refresh token logic (add if needed)
        pass

@app.get("/cron/daily")
async def daily_cron():
    now = datetime.utcnow()
    users = supabase.table("users").select("*").eq("active", True).execute().data
    
    for user in users:
        next_post = datetime.fromisoformat(user["next_post_at"].replace('Z', '+00:00'))
        if now >= next_post:
            await post_for_user(user)
            # Reschedule
            new_next = (now + timedelta(days=1)).isoformat()
            supabase.table("users").update({"next_post_at": new_next}).eq("x_id", user["x_id"]).execute()
    
    return JSONResponse({"status": "ok", "processed": len(users)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
