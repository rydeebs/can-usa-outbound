"""
server.py — CAN USA Outbound Platform backend.

Storage:
  - If DATABASE_URL is set (Railway Postgres): state stored in Postgres.
  - If not set (local dev): falls back to data/state.json.

Environment variables (set in Railway):
    APP_PASSWORD          Password to access the platform (required)
    SECRET_KEY            Random hex string for signing session cookies (required)
    DATABASE_URL          Auto-set by Railway when you add a Postgres database
    ANTHROPIC_API_KEY     For Claude API calls in the agent
    AZURE_CLIENT_ID       Azure app registration client ID
    AZURE_TENANT_ID       Azure tenant ID
    SENDER_EMAIL          pawel@canusa.com
    TOKEN_CACHE_JSON      Contents of token_cache.json (set after running auth.py)
    AUTO_SEND             false (default)
    POLL_INTERVAL_MINUTES 15 (default)

Run locally:
    uvicorn server:app --host 0.0.0.0 --port 8080 --reload
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

import psycopg2
import psycopg2.extras
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
# Railway may use any of these variable names depending on how Postgres was added
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("POSTGRES_URL") or
    os.environ.get("POSTGRESQL_URL") or
    os.environ.get("DATABASE_PRIVATE_URL") or
    os.environ.get("POSTGRES_PRIVATE_URL") or
    ""
)
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL_MINUTES", "15")) * 60
COOKIE_NAME    = "canusa_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set — platform is unprotected.")
log.info(f"Storage: {'PostgreSQL' if DATABASE_URL else 'local JSON file'}")

# ── Session token ──────────────────────────────────────────────────────────
def _make_session_token(password: str) -> str:
    return hmac.new(
        SECRET_KEY.encode(), password.encode(), hashlib.sha256
    ).hexdigest()

SESSION_TOKEN = _make_session_token(APP_PASSWORD) if APP_PASSWORD else ""

def is_authenticated(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return request.cookies.get(COOKIE_NAME) == SESSION_TOKEN

# ── Default state ──────────────────────────────────────────────────────────
DEFAULT_STATE: dict = {
    "contacts": [],
    "seqEmails": {},
    "htmlTemplates": {},
    "customTemplates": [],
    "userSignature": "",
    "azureClientId": "",
    "savedAt": None,
}

# ── PostgreSQL helpers ─────────────────────────────────────────────────────
def _get_conn():
    return psycopg2.connect(DATABASE_URL)

def _init_db() -> None:
    """Create the state table and seed it with one empty row if needed."""
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_state (
                        id         INTEGER PRIMARY KEY DEFAULT 1,
                        data       JSONB   NOT NULL DEFAULT '{}',
                        updated_at TIMESTAMP DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    INSERT INTO app_state (id, data)
                    VALUES (1, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING;
                """, (json.dumps(DEFAULT_STATE),))
        conn.close()
        log.info("PostgreSQL: table ready.")
    except Exception as e:
        log.error(f"PostgreSQL init error: {e}")
        raise

def _read_db() -> dict:
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT data FROM app_state WHERE id = 1;")
            row = cur.fetchone()
        conn.close()
        if row:
            d = row["data"]
            return d if isinstance(d, dict) else json.loads(d)
        return DEFAULT_STATE.copy()
    except Exception as e:
        log.error(f"PostgreSQL read error: {e}")
        return DEFAULT_STATE.copy()

def _write_db(state: dict) -> None:
    conn = _get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_state (id, data, updated_at)
                VALUES (1, %s::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE
                SET data = EXCLUDED.data, updated_at = NOW();
            """, (json.dumps(state, ensure_ascii=False),))
    conn.close()

# ── JSON file helpers (local dev fallback) ─────────────────────────────────
def _read_file() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"JSON read error: {e}")
    return DEFAULT_STATE.copy()

def _write_file(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

# ── Unified state interface ────────────────────────────────────────────────
def read_state() -> dict:
    return _read_db() if DATABASE_URL else _read_file()

def write_state(state: dict) -> None:
    if DATABASE_URL:
        _write_db(state)
    else:
        _write_file(state)

# ── Token cache bootstrap ──────────────────────────────────────────────────
def _bootstrap_token_cache() -> None:
    """Write TOKEN_CACHE_JSON env var to disk so the agent can use it."""
    token_json = os.environ.get("TOKEN_CACHE_JSON")
    if token_json:
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(token_json, encoding="utf-8")
            log.info("Token cache written from env var.")
        except Exception as e:
            log.error(f"Token cache write error: {e}")
    elif not TOKEN_FILE.exists():
        log.warning("token_cache.json not found. Run auth.py then set TOKEN_CACHE_JSON in Railway.")

# ── Agent background loop ──────────────────────────────────────────────────
async def _agent_loop() -> None:
    await asyncio.sleep(15)
    log.info(f"Agent: polling every {POLL_INTERVAL // 60} min.")
    while True:
        try:
            from agent.main import check_inbox  # type: ignore
            log.info("Agent: checking inbox...")
            await asyncio.get_event_loop().run_in_executor(None, check_inbox)
        except FileNotFoundError:
            log.warning("Agent: token_cache.json not found. Set TOKEN_CACHE_JSON in Railway.")
        except ImportError as e:
            log.warning(f"Agent import error: {e}")
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
.hint{color:#4D5F7A;font-size:11px;margin-top:14px;text-align:center}
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
  <div class="hint">Contact your admin for access.</div>
</div>
<script>
async function login(){
  const pw=document.getElementById('pw').value.trim();if(!pw)return;
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.textContent='Signing in\u2026';
  document.getElementById('err').style.display='none';
  try{
    const r=await fetch('/api/login',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
    if(r.ok){window.location.href='/';}
    else{document.getElementById('err').style.display='block';
         btn.disabled=false;btn.textContent='Sign in';
         document.getElementById('pw').select();}
  }catch(e){
    document.getElementById('err').textContent='Connection error \u2014 try again.';
    document.getElementById('err').style.display='block';
    btn.disabled=false;btn.textContent='Sign in';
  }
}
</script>
</body>
</html>"""

# ── FastAPI app ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL:
        _init_db()
    _bootstrap_token_cache()
    task = asyncio.create_task(_agent_loop())
    log.info("Server ready.")
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
    if is_authenticated(request): return RedirectResponse("/")
    return HTMLResponse(LOGIN_HTML)

@app.post("/api/login")
async def api_login(request: Request, response: Response):
    try: body = await request.json()
    except Exception: raise HTTPException(status_code=400)
    if APP_PASSWORD and body.get("password") != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect password")
    response.set_cookie(COOKIE_NAME, SESSION_TOKEN,
                        httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE)
    return {"ok": True}

@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}

# ── State routes ───────────────────────────────────────────────────────────
@app.get("/api/state")
async def get_state(request: Request):
    if not is_authenticated(request): raise HTTPException(status_code=401)
    return JSONResponse(read_state())

@app.post("/api/state")
async def post_state(request: Request):
    if not is_authenticated(request): raise HTTPException(status_code=401)
    try: body = await request.json()
    except Exception: raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict): raise HTTPException(status_code=400)
    write_state(body)
    return {"ok": True, "savedAt": body.get("savedAt")}

# ── Debug (shows which DB env vars Railway has set) ───────────────────────
@app.get("/debug-env")
async def debug_env():
    """Shows DB-related env vars so you can confirm which name Railway is using."""
    db_vars = {k: (v[:30]+"..." if v and len(v)>30 else v)
               for k, v in os.environ.items()
               if any(x in k.upper() for x in ["DATABASE","POSTGRES","PG","PGHOST"])}
    return {
        "db_vars_found": db_vars,
        "DATABASE_URL_resolved": bool(DATABASE_URL),
        "DATABASE_URL_prefix": DATABASE_URL[:30]+"..." if DATABASE_URL else None
    }

# ── Health ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        state = read_state()
        return {
            "status": "ok",
            "storage": "postgres" if DATABASE_URL else "file",
            "contacts": len(state.get("contacts", [])),
            "agent_poll_interval_min": POLL_INTERVAL // 60,
            "token_cache_exists": TOKEN_FILE.exists(),
        }
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ── Frontend ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not is_authenticated(request): return RedirectResponse("/login")
    if not INDEX_HTML.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))

@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, path: str):
    if not is_authenticated(request): return RedirectResponse("/login")
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))