# CAN USA Outbound Platform

Outbound email platform for Pawel Wojcik (Special Rigger, CAN USA) targeting NYC architect firms with FISP/Local Law 11 inspection backlogs.

---

## File map

| File | Purpose |
|------|---------|
| `index.html` | Frontend UI — deploy to Netlify |
| `AGENTS.md` | Instructions for AI coding agents working in this repo |
| `SOUL.md` | Pawel's voice, values, tone — loaded by the responder agent |
| `agent/main.py` | Email agent entry point |
| `agent/router.py` | Step 1: classify incoming reply type |
| `agent/responder.py` | Step 2: generate reply using SOUL.md |
| `agent/evaluator.py` | Step 3: quality-check the draft |
| `agent/prompts/router.md` | System prompt for classification |
| `agent/prompts/responder.md` | System prompt for response generation |
| `agent/prompts/evaluator.md` | System prompt for quality evaluation |
| `data/contacts.json` | Contact database (shared with frontend) |
| `templates/T{tier}-{tpl}.html` | HTML email designs per Tier × Template combo |

---

## Architecture

The agent uses three Anthropic patterns from [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents):

```
Incoming reply
     │
     ▼
[1] ROUTER            claude-haiku-4-5
     │ category, urgency, key_points
     ▼
[2] RESPONDER         claude-sonnet-4-5 (or haiku for simple cases)
     │ draft email body
     ▼
[3] EVALUATOR         claude-haiku-4-5
     │ pass/fail + fix_instructions
     ├─ fail → back to RESPONDER (max 2 retries)
     └─ pass → SEND or QUEUE FOR REVIEW
```

**Why three separate steps instead of one big prompt:**
- Each step has one focused job (routing, writing, checking). A combined prompt degrades on all three.
- The router uses cheap/fast haiku; only the responder needs sonnet. Saves cost.
- The evaluator-optimizer loop catches em-dashes, banned phrases, and quality issues automatically before anything reaches Pawel's inbox or a contact's inbox.

---

## Deploying the frontend (Netlify)

1. Go to [netlify.com](https://netlify.com) and create a free account
2. Drag and drop `index.html` onto the deploy zone
3. Set site name: Site configuration → Domain management → Edit site name → `canusa-outbound`
4. Live at `https://canusa-outbound.netlify.app`

---

## Setting up Outlook auth

Pawel does this once using his CAN USA Microsoft account.

**For the frontend (browser OAuth — popup):**
1. Sign in to [portal.azure.com](https://portal.azure.com) with Pawel's CAN USA email
2. Azure Active Directory → App registrations → New registration
3. Name: "CAN USA Outbound Frontend" | Redirect URI: **Single-page application** → `https://canusa-outbound.netlify.app`
4. Copy Application (client) ID → paste in the platform Settings tab
5. API permissions → Add → `Mail.Send` + `Mail.ReadWrite` (Delegated)

**For the agent (server-to-server — no popup):**
1. Register a second app: "CAN USA Outbound Agent"
2. Redirect URI: none (daemon app)
3. Certificates & secrets → New client secret → copy the value
4. API permissions → Add → `Mail.Send` + `Mail.ReadWrite` + `Mail.Read` (Application, not Delegated)
5. Admin consent required — Pawel's IT admin approves once
6. Copy `client_id`, `tenant_id`, `client_secret` into `agent/.env`

---

## Running the agent

```bash
cd agent
pip install -r requirements.txt
cp .env.example .env    # fill in your keys
python main.py          # polls every 15 minutes
```

**First run checklist:**
- [ ] `AUTO_SEND=false` in `.env` (always start in review mode)
- [ ] Verify at least one contact in `data/contacts.json`
- [ ] Confirm Graph API permissions are admin-consented
- [ ] Run `python -c "from graph_client import GraphClient; GraphClient().test_connection()"` to verify auth

---

## Two-mode operation

**Review mode** (`AUTO_SEND=false`) — recommended for the first 30 days

The agent detects replies, generates Claude responses, and writes them to `data/reply_log.json`. Pawel reviews and approves in `index.html` before anything is sent. This lets Pawel catch edge cases and build trust in the agent's quality.

**Auto-send mode** (`AUTO_SEND=true`) — after 30 days of review

The agent sends approved categories automatically. Hard opt-outs and out-of-office replies are always handled automatically. Interested replies and objections should stay in review mode the longest.

---

## Updating the agent

| What you want to change | Where to edit |
|------------------------|---------------|
| Pawel's tone or voice | `SOUL.md` |
| How replies are classified | `agent/prompts/router.md` |
| How responses are written | `agent/prompts/responder.md` |
| Quality criteria | `agent/prompts/evaluator.md` |
| FISP deadlines | `SOUL.md` + `agent/prompts/responder.md` |
| CAN USA offerings | `SOUL.md` Section 2 |
| HTML email designs | Upload via platform → HTML templates panel |

No Python changes needed for any messaging or tone updates.

---

## Where to run the agent

| Option | Cost | Always-on | Recommended for |
|--------|------|-----------|-----------------|
| Your laptop | Free | No | Initial testing |
| DigitalOcean Droplet ($6/mo) | ~$6/mo | Yes | Production |
| Railway.app | ~$5/mo | Yes | Production |
| GitHub Actions (scheduled) | Free | Near-real-time | Low volume |
