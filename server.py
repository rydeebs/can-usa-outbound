"""
server.py — CAN USA Outbound Platform backend.

Responsibilities:
  1. Serves index.html at GET /  (password-protected)
  2. Provides /api/state  (GET to load, POST to save) — shared between all browsers
  3. Provides /api/login and /api/logout
  4. Runs the email agent inbox-polling loop as a background asyncio task
  5. Handles TOKEN_CACHE_JSON env var (writes token_cache.json on startup for Railway)

Run locally:
    uvicorn server:app --host 0.0.0.0 --port 8080 --reload

Environment variables (set in Railway):
    APP_PASSWORD          Password Pawel and you use to log in (required)
    SECRET_KEY            Random string for signing session cookies (required)
    ANTHROPIC_API_KEY     For Claude API calls in the agent
    AZURE_CLIENT_ID       Azure app registration client ID
    AZURE_TENANT_ID       Azure tenant ID
    SENDER_EMAIL          pawel@canusa.com
    TOKEN_CACHE_JSON      Contents of token_cache.json (set after running auth.py)
    AUTO_SEND             false (default) — set true only after 30-day review period
    POLL_INTERVAL_MINUTES 15 (default)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("server")

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent
DATA_DIR   = ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
INDEX_HTML = ROOT / "index.html"
TOKEN_FILE = ROOT / "agent" / "token_cache.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ─────────────────────────────────────────────────────────────────
APP_PASSWORD   = os.environ.get("APP_PASSWORD", "")
SECRET_KEY     = os.environ.get("SECRET_KEY", secrets.token_hex(32))
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL_MINUTES", "15")) * 60
COOKIE_NAME    = "canusa_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

if not APP_PASSWORD:
    log.warning(
        "APP_PASSWORD is not set. The platform will be unprotected. "
        "Set APP_PASSWORD in Railway environment variables."
    )

# ── Session token (HMAC of password so it's stateless) ────────────────────
def _make_session_token(password: str) -> str:
    return hmac.new(
        SECRET_KEY.encode(), password.encode(), hashlib.sha256
    ).hexdigest()

SESSION_TOKEN = _make_session_token(APP_PASSWORD) if APP_PASSWORD else ""


def is_authenticated(request: Request) -> bool:
    if not APP_PASSWORD:
        return True  # No password set — open access (not recommended)
    return request.cookies.get(COOKIE_NAME) == SESSION_TOKEN


# ── State storage ──────────────────────────────────────────────────────────
DEFAULT_STATE: dict = {
    "contacts": [],
    "seqEmails": {},
    "htmlTemplates": {},
    "customTemplates": [],
    "userSignature": "",
    "azureClientId": "",
    "savedAt": None,
}


def read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to read state file: {e}")
    return DEFAULT_STATE.copy()


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ── Token cache bootstrap (for Railway) ────────────────────────────────────
def _bootstrap_token_cache() -> None:
    """
    Railway doesn't persist files between deploys.
    If TOKEN_CACHE_JSON env var is set, write it to disk on every startup.
    After running auth.py locally, paste the contents of token_cache.json
    into Railway as the TOKEN_CACHE_JSON environment variable.
    """
    token_json = os.environ.get("TOKEN_CACHE_JSON")
    if token_json:
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(token_json, encoding="utf-8")
            log.info("Token cache written from TOKEN_CACHE_JSON env var")
        except Exception as e:
            log.error(f"Failed to write token cache: {e}")
    elif not TOKEN_FILE.exists():
        log.warning(
            "token_cache.json not found and TOKEN_CACHE_JSON env var not set. "
            "Run 'python agent/auth.py' to authenticate, then set TOKEN_CACHE_JSON in Railway."
        )


# ── Agent background loop ──────────────────────────────────────────────────
async def _agent_loop() -> None:
    """
    Polls Pawel's inbox every POLL_INTERVAL seconds.
    Runs as a background asyncio task alongside the web server.
    Errors are logged but never crash the web server.
    """
    await asyncio.sleep(15)  # Let the server fully start first
    log.info(f"Agent loop started — polling every {POLL_INTERVAL // 60} minutes")
    while True:
        try:
            # Import here so import errors don't break the web server startup
            from agent.main import check_inbox  # type: ignore
            log.info("Agent: polling inbox...")
            # Run sync function in thread pool so it doesn't block the event loop
            await asyncio.get_event_loop().run_in_executor(None, check_inbox)
        except FileNotFoundError:
            log.warning(
                "Agent: token_cache.json not found. "
                "Set TOKEN_CACHE_JSON in Railway env vars after running auth.py."
            )
        except ImportError as e:
            log.warning(f"Agent: import error — {e}. Agent disabled until fixed.")
            await asyncio.sleep(POLL_INTERVAL)
            continue
        except Exception as e:
            log.error(f"Agent error: {e}", exc_info=True)
        await asyncio.sleep(POLL_INTERVAL)


# ── Login page HTML ────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CAN USA — Sign in</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1A1E30;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     display:flex;align-items:center;justify-content:center;height:100vh;color:#DCE3F2}
.card{background:#202436;border:1px solid rgba(255,255,255,0.08);border-radius:12px;
      padding:40px;width:340px;box-shadow:0 8px 32px rgba(0,0,0,0.4)}
.brand{font-size:22px;font-weight:700;color:#D98B28;letter-spacing:.06em;margin-bottom:6px}
.sub{font-size:12px;color:#4D5F7A;margin-bottom:28px}
label{display:block;font-size:10px;font-weight:600;color:#4D5F7A;
      text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
input{width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);
      border-radius:7px;color:#DCE3F2;padding:11px 13px;font-size:14px;outline:none;
      font-family:inherit;transition:border-color .15s;margin-bottom:16px}
input:focus{border-color:rgba(217,139,40,0.4)}
button{width:100%;background:#D98B28;color:#0B0F1A;border:none;border-radius:7px;
       padding:12px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;
       transition:background .12s}
button:hover{background:#C07A20}
button:disabled{opacity:.5;cursor:not-allowed}
.err{color:#C05050;font-size:12px;margin-top:12px;text-align:center;display:none}
.loading{display:none;text-align:center;color:#8695B2;font-size:12px;margin-top:12px}
</style>
</head>
<body>
<div class="card">
  <div class="brand">CAN USA</div>
  <div class="sub">Outbound platform &mdash; sign in to continue</div>
  <label>Password</label>
  <input type="password" id="pw" placeholder="Enter password"
         onkeydown="if(event.key==='Enter')login()" autofocus/>
  <button id="btn" onclick="login()">Sign in</button>
  <div class="err" id="err">Incorrect password &mdash; try again.</div>
  <div class="loading" id="loading">Signing in&hellip;</div>
</div>
<script>
async function login(){
  const pw=document.getElementById('pw').value.trim();
  if(!pw)return;
  document.getElementById('btn').disabled=true;
  document.getElementById('err').style.display='none';
  document.getElementById('loading').style.display='block';
  try{
    const r=await fetch('/api/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw})
    });
    if(r.ok){
      window.location.href='/';
    }else{
      document.getElementById('err').style.display='block';
      document.getElementById('btn').disabled=false;
      document.getElementById('loading').style.display='none';
      document.getElementById('pw').select();
    }
  }catch(e){
    document.getElementById('err').textContent='Connection error — try again.';
    document.getElementById('err').style.display='block';
    document.getElementById('btn').disabled=false;
    document.getElementById('loading').style.display='none';
  }
}
</script>
</body>
</html>"""


# ── FastAPI app ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_token_cache()
    task = asyncio.create_task(_agent_loop())
    log.info("Server ready")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ── Auth routes ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/")
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login")
async def api_login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")
    if not APP_PASSWORD:
        # No password configured — accept anything
        pass
    elif body.get("password") != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect password")
    response.set_cookie(
        COOKIE_NAME, SESSION_TOKEN,
        httponly=True, samesite="lax",
        max_age=COOKIE_MAX_AGE,
    )
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


# ── State routes ───────────────────────────────────────────────────────────

@app.get("/api/state")
async def get_state(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return JSONResponse(read_state())


@app.post("/api/state")
async def post_state(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    # Validate minimal structure
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="State must be a JSON object")
    write_state(body)
    return {"ok": True, "savedAt": body.get("savedAt")}


# ── Health check ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    contacts_count = len(read_state().get("contacts", []))
    return {
        "status": "ok",
        "contacts": contacts_count,
        "agent_poll_interval_min": POLL_INTERVAL // 60,
        "token_cache_exists": TOKEN_FILE.exists(),
    }


# ── Frontend ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login")
    if not INDEX_HTML.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


# ── Catch-all for any other path ───────────────────────────────────────────

@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, path: str):
    if not is_authenticated(request):
        return RedirectResponse("/login")
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))