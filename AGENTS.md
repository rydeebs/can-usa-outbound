# AGENTS.md
> Standard instructions for AI coding agents working in this repository.
> Human setup guide is in README.md. Pawel's voice and values are in SOUL.md.

## Project overview
CAN USA outbound email platform. Single-page frontend (`index.html`) + Python email agent (`agent/`). The agent polls Pawel's Outlook inbox, detects contact replies, routes them through Claude, and generates or sends responses.

## Setup
```bash
cd agent
pip install -r requirements.txt
cp .env.example .env          # then fill in real keys
python main.py                # starts the polling loop
```

## Environment variables (required)
```
ANTHROPIC_API_KEY=sk-ant-...
AZURE_CLIENT_ID=...
AZURE_TENANT_ID=...
AZURE_CLIENT_SECRET=...       # server-to-server daemon auth
SENDER_EMAIL=pawel@canusa.com
AUTO_SEND=false               # set true only after 30-day review period
POLL_INTERVAL_MINUTES=15
```

## Project structure
```
canusa-outbound/
├── index.html                # Frontend UI (deploy to Netlify)
├── AGENTS.md                 # This file
├── SOUL.md                   # Pawel's voice and values (loaded by responder)
│
├── agent/
│   ├── main.py               # Entry point — polling loop
│   ├── router.py             # Step 1: classify incoming reply
│   ├── responder.py          # Step 2: generate reply using SOUL.md
│   ├── evaluator.py          # Step 3: quality-check the draft
│   ├── graph_client.py       # Microsoft Graph API (inbox, send)
│   ├── contact_store.py      # Read/write contacts.json
│   ├── sequence_engine.py    # Sequence step logic and timing
│   ├── template_engine.py    # Apply HTML templates (T1-B.html etc.)
│   ├── requirements.txt
│   ├── .env.example
│   └── prompts/
│       ├── router.md         # System prompt: classify reply type
│       ├── responder.md      # System prompt: generate email response
│       └── evaluator.md      # System prompt: check response quality
│
├── data/
│   ├── contacts.json         # Contact database (synced with frontend)
│   ├── seq_emails.json       # Per-contact sequence email history
│   └── reply_log.json        # Log of all replies + Claude responses
│
├── templates/                # HTML email designs per Tier × Template
│   ├── T1-A.html  T1-B.html  T1-C.html
│   ├── T2-A.html  T2-B.html  T2-C.html
│   └── T3-A.html  T3-B.html  T3-C.html
│
└── README.md                 # Human setup and architecture guide
```

## Coding conventions
- Python 3.11+, typed with `from __future__ import annotations`
- No frameworks (no LangChain, no CrewAI) — direct `anthropic` SDK calls
- Each prompt file (`prompts/*.md`) is the complete system prompt for that step — do not inline prompts in Python
- `contact_store.py` is the single source of truth for contact state; never mutate `contacts.json` directly from multiple places
- All Graph API calls go through `graph_client.py` — never call the Graph API directly from other modules
- Log every Claude call (model, tokens, step name) to a rotating file log

## Running tests
```bash
cd agent
python -m pytest tests/ -v
python tests/test_router.py       # unit test reply classification
python tests/test_responder.py    # integration test with real Claude API
```

## Editing agent behaviour
- **Change Pawel's tone or voice** → edit `SOUL.md`
- **Change how replies are classified** → edit `agent/prompts/router.md`
- **Change how responses are written** → edit `agent/prompts/responder.md`
- **Change quality criteria** → edit `agent/prompts/evaluator.md`
- **Change FISP deadlines or CAN USA offerings** → update SOUL.md Section 2 and responder.md context block
- No Python changes needed for any of the above

## Security rules
- Never commit `.env` — it is in `.gitignore`
- Never log email body content to stdout in production
- `AUTO_SEND=true` requires explicit confirmation — default is always false
- All Graph API tokens are refreshed silently via MSAL — never store raw tokens in files
