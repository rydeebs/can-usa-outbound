"""
CAN USA Email Agent — main.py
Entry point. Starts the inbox polling loop.
See AGENTS.md for setup. See SOUL.md for Pawel's voice.
"""

from __future__ import annotations

import os
from pathlib import Path

if os.environ.get("TOKEN_CACHE_JSON"):
    Path(__file__).parent.joinpath("token_cache.json").write_text(os.environ["TOKEN_CACHE_JSON"])

import logging
import time
import schedule
from dotenv import load_dotenv

from graph_client import GraphClient
from router import classify_reply
from responder import generate_reply
from evaluator import evaluate_reply
from contact_store import ContactStore
from sequence_engine import SequenceEngine
from template_engine import TemplateEngine

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("agent.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("main")

AUTO_SEND = os.getenv("AUTO_SEND", "false").lower() == "true"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
MAX_RETRIES = 2  # evaluator-optimizer retries before escalating to Pawel


def process_reply(graph: GraphClient, store: ContactStore, engine: SequenceEngine, tpl: TemplateEngine, reply: dict) -> None:
    """
    Full pipeline for a single incoming reply.
    Implements: routing → prompt chaining (respond) → evaluator-optimizer.
    """
    contact = store.get_by_email(reply["from_email"])
    if not contact:
        log.info(f"Unknown sender {reply['from_email']} — skipping")
        return

    # ── Step 1: Route (classify the reply) ─────────────────────────────
    route = classify_reply(
        reply_text=reply["body"],
        contact=contact,
    )
    log.info(f"Routed {contact['firstName']} {contact['lastName']}: {route['category']} / {route['urgency']}")

    # Hard opt-outs — send a short close, stop sequence, no review needed
    if route["category"] == "HARD_OBJECTION":
        body = "Understood — I'll take you off our list. If the 10A window ever gets tight, we're easy to find."
        graph.send_email(to=contact["workEmail"], subject=f"Re: {reply['subject']}", body=body)
        store.update(contact["id"], {"paused": True, "replied": True})
        log.info(f"Sent opt-out close to {contact['workEmail']}")
        return

    # Auto-log out-of-office, no reply needed
    if route["category"] == "OUT_OF_OFFICE":
        log.info(f"Out of office from {contact['workEmail']} — logged, no reply")
        store.update(contact["id"], {"ooo_detected": True})
        return

    # ── Step 2: Generate reply (prompt chaining with SOUL.md context) ───
    previous_email = engine.get_last_sent_email(contact["id"])
    draft_body = generate_reply(
        contact=contact,
        reply_text=reply["body"],
        previous_email=previous_email,
        route=route,
        model=route["recommended_model"],
    )

    # ── Step 3: Evaluate (evaluator-optimizer loop) ──────────────────────
    approved_body = None
    for attempt in range(MAX_RETRIES + 1):
        verdict = evaluate_reply(draft=draft_body, route=route)
        log.info(f"Evaluator score: {verdict['score']}/10 pass={verdict['pass']} flags={verdict['flags']}")
        if verdict["pass"] and verdict["score"] >= 7:
            approved_body = draft_body
            break
        if attempt < MAX_RETRIES and verdict["fix_instructions"]:
            # Re-generate with fix instructions
            draft_body = generate_reply(
                contact=contact,
                reply_text=reply["body"],
                previous_email=previous_email,
                route=route,
                model=route["recommended_model"],
                fix_instructions=verdict["fix_instructions"],
            )
        else:
            log.warning(f"Draft failed evaluation after {attempt + 1} attempt(s) — queuing for Pawel review")
            store.queue_for_review(contact["id"], reply, draft_body, verdict)
            return

    # ── Step 4: Send or queue ────────────────────────────────────────────
    html_content = tpl.apply(contact, approved_body, reply["subject"])
    full_plain = approved_body  # signature appended by graph_client from settings

    if AUTO_SEND and route["auto_send_safe"]:
        graph.send_email(
            to=contact["workEmail"],
            subject=f"Re: {reply['subject']}",
            body=full_plain,
            html=html_content,
        )
        store.update(contact["id"], {"replied": True, "sequenceStep": contact["sequenceStep"] + 1})
        log.info(f"Auto-sent reply to {contact['workEmail']}")
    else:
        # Queue for Pawel to review in index.html before sending
        store.queue_for_review(contact["id"], reply, approved_body, {"pass": True, "score": verdict["score"]})
        log.info(f"Queued for review: {contact['firstName']} {contact['lastName']} (score {verdict['score']})")


def check_inbox() -> None:
    """Called on schedule. Fetches unread replies and processes each."""
    log.info("Polling inbox...")
    graph = GraphClient()
    store = ContactStore()
    engine = SequenceEngine(store)
    tpl = TemplateEngine()

    replies = graph.get_new_replies()
    log.info(f"Found {len(replies)} new replies")
    for reply in replies:
        try:
            process_reply(graph, store, engine, tpl, reply)
            graph.mark_as_read(reply["message_id"])
        except Exception as e:
            log.error(f"Error processing reply from {reply.get('from_email', '?')}: {e}", exc_info=True)


if __name__ == "__main__":
    log.info(f"Agent starting — AUTO_SEND={AUTO_SEND}, poll every {POLL_INTERVAL}m")
    check_inbox()  # Run once immediately on start
    schedule.every(POLL_INTERVAL).minutes.do(check_inbox)
    while True:
        schedule.run_pending()
        time.sleep(30)
