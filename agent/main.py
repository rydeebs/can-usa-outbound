"""
CAN USA Email Agent — main.py
Entry point. Starts the inbox polling loop.
See AGENTS.md for setup. See SOUL.md for Pawel's voice.
"""

from __future__ import annotations
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

# Bootstrap token cache from env var (Railway doesn't persist files)
if os.environ.get("TOKEN_CACHE_JSON"):
    Path(__file__).parent.joinpath("token_cache.json").write_text(
        os.environ["TOKEN_CACHE_JSON"]
    )

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

import time
import schedule
# Ensure agent/ directory is in path whether run directly or imported from server.py
import sys as _sys, os as _os
_agent_dir = _os.path.dirname(_os.path.abspath(__file__))
if _agent_dir not in _sys.path:
    _sys.path.insert(0, _agent_dir)

from graph_client import GraphClient
from router import classify_reply
from responder import generate_reply
from evaluator import evaluate_reply
from contact_store import ContactStore
from sequence_engine import SequenceEngine
from template_engine import TemplateEngine
from alerter import check_hot_lead, check_at_risk, check_new_inbound

AUTO_SEND    = os.getenv("AUTO_SEND", "false").lower() == "true"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
MAX_RETRIES  = 2


def process_reply(
    graph: GraphClient,
    store: ContactStore,
    engine: SequenceEngine,
    tpl: TemplateEngine,
    reply: dict,
    all_contacts: list[dict],
) -> None:
    """
    Full pipeline for a single incoming reply.
    routing → alert checks → prompt chaining → evaluator-optimizer → send/queue
    """
    contact = store.get_by_email(reply["from_email"])

    # ── New inbound (unknown sender enquiring about our services) ────────
    if not contact:
        check_new_inbound(reply, all_contacts, graph)
        log.info(f"Unknown sender {reply['from_email']} — checked for inbound signal")
        return

    # ── Step 1: Route ────────────────────────────────────────────────────
    route = classify_reply(reply_text=reply["body"], contact=contact)
    log.info(
        f"Routed {contact['firstName']} {contact['lastName']}: "
        f"{route['category']} / {route['urgency']}"
    )

    # ── Step 2: Alert checks ─────────────────────────────────────────────
    check_hot_lead(contact, reply["body"], route, graph)
    check_at_risk(contact, reply["body"], route, graph)

    # Hard opt-outs — send close, no response needed
    if route["category"] == "HARD_OBJECTION":
        body = (
            "Understood — I'll take you off our list. "
            "If the 10A window ever gets tight, we're easy to find."
        )
        graph.send_email(
            to=contact["workEmail"],
            subject=f"Re: {reply['subject']}",
            body=body,
        )
        store.update(contact["id"], {"paused": True, "replied": True})
        log.info(f"Sent opt-out close to {contact['workEmail']}")
        return

    # Out-of-office — log only
    if route["category"] == "OUT_OF_OFFICE":
        log.info(f"Out of office from {contact['workEmail']} — logged")
        store.update(contact["id"], {"ooo_detected": True})
        return

    # ── Step 3: Generate reply ────────────────────────────────────────────
    previous_email = engine.get_last_sent_email(contact["id"])
    draft_body = generate_reply(
        contact=contact,
        reply_text=reply["body"],
        previous_email=previous_email,
        route=route,
        model=route["recommended_model"],
    )

    # ── Step 4: Evaluate (evaluator-optimizer loop) ───────────────────────
    approved_body = None
    verdict = {"score": 0, "pass": False}
    for attempt in range(MAX_RETRIES + 1):
        verdict = evaluate_reply(draft=draft_body, route=route)
        log.info(
            f"Evaluator: {verdict['score']}/10 "
            f"pass={verdict['pass']} flags={verdict['flags']}"
        )
        if verdict["pass"] and verdict["score"] >= 7:
            approved_body = draft_body
            break
        if attempt < MAX_RETRIES and verdict.get("fix_instructions"):
            draft_body = generate_reply(
                contact=contact,
                reply_text=reply["body"],
                previous_email=previous_email,
                route=route,
                model=route["recommended_model"],
                fix_instructions=verdict["fix_instructions"],
            )
        else:
            log.warning(
                f"Draft failed evaluation after {attempt + 1} attempts — "
                "queuing for Pawel review"
            )
            store.queue_for_review(contact["id"], reply, draft_body, verdict)
            return

    # ── Step 5: Send or queue ─────────────────────────────────────────────
    html_content = tpl.apply(contact, approved_body, reply["subject"])

    if AUTO_SEND and route["auto_send_safe"]:
        graph.send_email(
            to=contact["workEmail"],
            subject=f"Re: {reply['subject']}",
            body=approved_body,
            html=html_content,
        )
        store.update(contact["id"], {
            "replied": True,
            "sequenceStep": contact["sequenceStep"] + 1,
        })
        log.info(f"Auto-sent reply to {contact['workEmail']}")
    else:
        store.queue_for_review(
            contact["id"], reply, approved_body,
            {"pass": True, "score": verdict["score"]},
        )
        log.info(
            f"Queued for review: {contact['firstName']} {contact['lastName']} "
            f"(score {verdict['score']})"
        )


def check_inbox() -> None:
    """Called on schedule. Fetches all unread mail and processes each."""
    log.info("Polling inbox...")
    graph  = GraphClient()
    store  = ContactStore()
    engine = SequenceEngine(store)
    tpl    = TemplateEngine()

    # Load contacts once — shared across all replies for inbound detection
    all_contacts = store.all()

    # get_new_replies now returns ALL unread mail (not just Re: subjects)
    # so we can detect new inbound enquiries too
    replies = graph.get_new_replies()
    log.info(f"Found {len(replies)} unread messages")

    for reply in replies:
        try:
            process_reply(graph, store, engine, tpl, reply, all_contacts)
            graph.mark_as_read(reply["message_id"])
        except Exception as e:
            log.error(
                f"Error processing message from {reply.get('from_email','?')}: {e}",
                exc_info=True,
            )


if __name__ == "__main__":
    log.info(f"Agent starting — AUTO_SEND={AUTO_SEND}, poll every {POLL_INTERVAL}m")
    check_inbox()
    schedule.every(POLL_INTERVAL).minutes.do(check_inbox)
    while True:
        schedule.run_pending()
        time.sleep(30)