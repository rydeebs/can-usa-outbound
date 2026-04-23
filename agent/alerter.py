"""
alerter.py — Alert system for the CAN USA email agent.

Detects three situations and sends email notifications + writes to alerts log:

  🟢 HOT_LEAD     — contact shows strong buying signals (pricing, availability,
                    wants to meet, positive language)
  🔴 AT_RISK      — deal going cold (multiple objections, long silence after
                    an interested reply, soft opt-out language)
  📬 NEW_INBOUND  — email from an unknown sender (not in contacts) that mentions
                    FISP, facade, rope access, Local Law 11, or inspection

Alerts are:
  1. Emailed to ALERT_EMAIL immediately via Graph API (so Pawel gets a push
     notification on his phone even if the browser isn't open)
  2. Written to data/alerts.json so the platform can display them in-app
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("alerter")

# ── Config ─────────────────────────────────────────────────────────────────
# Who receives alert emails. Defaults to SENDER_EMAIL (Pawel's own inbox)
# but can be a separate address e.g. your email or both via comma separation.
ALERT_EMAIL   = os.environ.get("ALERT_EMAIL", os.environ.get("SENDER_EMAIL", ""))
SENDER_EMAIL  = os.environ.get("SENDER_EMAIL", "")

# Path to alerts log (written to data/ alongside contacts.json)
ALERTS_FILE   = Path(__file__).parent.parent / "data" / "alerts.json"

# ── Keyword signals ─────────────────────────────────────────────────────────
# Words/phrases that indicate strong buying intent
HOT_SIGNALS = [
    "how much", "what's the cost", "what does it cost", "pricing",
    "price", "quote", "proposal", "rate", "budget",
    "available", "availability", "schedule", "when can you",
    "let's talk", "set up a call", "book a meeting", "calendar",
    "interested", "sounds good", "makes sense", "let's do it",
    "move forward", "next steps", "sign", "contract", "agreement",
    "can you start", "start date", "timeline",
]

# Words/phrases that indicate a deal going cold or at risk
RISK_SIGNALS = [
    "not interested", "no longer", "not in the market",
    "already have", "going with someone else", "decided to go",
    "using another", "chose another", "not a fit", "not the right time",
    "maybe next year", "budget has been cut", "budget freeze",
    "on hold", "put on hold", "paused our", "not moving forward",
    "remove me", "unsubscribe", "stop emailing", "please don't",
    "not relevant", "wrong person", "wrong contact",
]

# Keywords that suggest an inbound email is about our services
INBOUND_SIGNALS = [
    "fisp", "local law 11", "facade inspection", "façade inspection",
    "rope access", "building inspection", "cycle 10", "qewi",
    "swarmp", "unsafe", "local law", "ll11", "exterior wall",
    "scaffolding", "sidewalk shed", "sprat", "inspection program",
    "structural inspection", "canusa", "can usa",
]


# ── Alert creation ──────────────────────────────────────────────────────────

def _append_alert(alert: dict) -> None:
    """Appends one alert to data/alerts.json."""
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    alerts = []
    if ALERTS_FILE.exists():
        try:
            alerts = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            alerts = []
    alerts.insert(0, alert)           # newest first
    alerts = alerts[:200]             # cap at 200 alerts
    ALERTS_FILE.write_text(json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8")


def _send_alert_email(graph, subject: str, body: str) -> None:
    """Sends an alert email to ALERT_EMAIL via Graph API."""
    if not ALERT_EMAIL or not graph:
        return
    try:
        graph.send_email(to=ALERT_EMAIL, subject=subject, body=body)
        log.info(f"Alert email sent: {subject}")
    except Exception as e:
        log.warning(f"Could not send alert email: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Hot lead detection ──────────────────────────────────────────────────────

def check_hot_lead(
    contact: dict,
    reply_text: str,
    route: dict,
    graph=None,
) -> bool:
    """
    Returns True and fires an alert if this reply looks like a hot lead.
    Called after routing, before generating a response.
    """
    text_lower = reply_text.lower()
    urgency    = route.get("urgency", "LOW")
    category   = route.get("category", "UNKNOWN")

    # Already classified as INTERESTED with HIGH urgency → definitely hot
    is_hot = (
        category == "INTERESTED" and urgency == "HIGH"
    ) or (
        category in ("PRICING", "INTERESTED")
        and any(sig in text_lower for sig in HOT_SIGNALS)
    )

    if not is_hot:
        return False

    name     = f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
    firm     = contact.get("firmName", "Unknown firm")
    email    = contact.get("workEmail", "")
    preview  = reply_text[:300].replace("\n", " ").strip()

    alert = {
        "id":        f"hot_{contact.get('id','')}_{_now_iso()}",
        "type":      "HOT_LEAD",
        "emoji":     "🟢",
        "title":     f"Hot lead — {name} at {firm}",
        "detail":    f'"{preview}..."',
        "contact":   {"name": name, "firm": firm, "email": email},
        "category":  category,
        "urgency":   urgency,
        "timestamp": _now_iso(),
        "read":      False,
    }
    _append_alert(alert)
    log.info(f"🟢 HOT LEAD: {name} at {firm}")

    subj = f"🟢 Hot lead — {name} at {firm}"
    body = (
        f"Hot lead detected by the CAN USA agent.\n\n"
        f"Contact:  {name}\n"
        f"Firm:     {firm}\n"
        f"Email:    {email}\n"
        f"Category: {category} / {urgency}\n\n"
        f"Their reply:\n{reply_text[:800]}\n\n"
        f"Action: Log in to the platform and review the generated response before sending.\n"
        f"Platform: https://can-usa-outbound-production.up.railway.app"
    )
    _send_alert_email(graph, subj, body)
    return True


# ── At-risk detection ───────────────────────────────────────────────────────

def check_at_risk(
    contact: dict,
    reply_text: str,
    route: dict,
    graph=None,
) -> bool:
    """
    Returns True and fires an alert if this deal looks at risk.
    """
    text_lower = reply_text.lower()
    category   = route.get("category", "UNKNOWN")

    is_at_risk = (
        category == "SOFT_OBJECTION"
        and any(sig in text_lower for sig in RISK_SIGNALS)
    ) or (
        category == "HARD_OBJECTION"  # always at risk / lost
    )

    if not is_at_risk:
        return False

    name    = f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
    firm    = contact.get("firmName", "Unknown firm")
    email   = contact.get("workEmail", "")
    preview = reply_text[:300].replace("\n", " ").strip()

    alert = {
        "id":        f"risk_{contact.get('id','')}_{_now_iso()}",
        "type":      "AT_RISK",
        "emoji":     "🔴",
        "title":     f"At risk — {name} at {firm}",
        "detail":    f'"{preview}..."',
        "contact":   {"name": name, "firm": firm, "email": email},
        "category":  category,
        "timestamp": _now_iso(),
        "read":      False,
    }
    _append_alert(alert)
    log.info(f"🔴 AT RISK: {name} at {firm}")

    subj = f"🔴 At risk — {name} at {firm}"
    body = (
        f"Deal at risk — the CAN USA agent flagged a concerning reply.\n\n"
        f"Contact:  {name}\n"
        f"Firm:     {firm}\n"
        f"Email:    {email}\n"
        f"Category: {category}\n\n"
        f"Their reply:\n{reply_text[:800]}\n\n"
        f"Action: Log in and review — a personal call may be the right move here.\n"
        f"Platform: https://can-usa-outbound-production.up.railway.app"
    )
    _send_alert_email(graph, subj, body)
    return True


# ── New inbound detection ────────────────────────────────────────────────────

def check_new_inbound(
    reply: dict,
    known_contacts: list[dict],
    graph=None,
) -> bool:
    """
    Handles two inbound scenarios:

    A) EXISTING CONTACT, new thread (subject they started, not a reply to ours):
       - They emailed Pawel directly (not via Re: to our outbound)
       - Could be: asking a question, referral, inbound from flyer
       - Action: alert + route to a new inbound flow

    B) UNKNOWN SENDER with service-related keywords:
       - Not in contacts at all
       - Action: alert + suggest adding to contacts as (New)
    """
    from_email = reply.get("from_email", "").lower()
    subject    = reply.get("subject", "").lower()
    body       = reply.get("body", "").lower()
    combined   = subject + " " + body

    # Skip system/bounce emails
    skip_domains = ["mailer-daemon", "postmaster", "noreply", "no-reply",
                    "notifications@", "bounce", "donotreply"]
    if any(s in from_email for s in skip_domains):
        return False

    # Build lookup of known contacts by email
    known_email_map = {
        c.get("workEmail", "").lower(): c
        for c in known_contacts
        if c.get("workEmail")
    }

    existing_contact = known_email_map.get(from_email)
    is_existing      = existing_contact is not None

    # For existing contacts: flag if this is a NEW thread (not Re: to our outbound)
    # i.e. subject does NOT start with "re:" — they reached out to Pawel directly
    is_reply_thread = subject.startswith("re:")

    if is_existing and is_reply_thread:
        # This is handled by process_reply() — skip here
        return False

    # For unknown senders or existing contacts starting new threads:
    # Only alert if the email mentions our services OR if it's an existing contact
    has_signal = any(sig in combined for sig in INBOUND_SIGNALS)

    if not is_existing and not has_signal:
        return False  # Unknown sender, no service keywords — ignore

    # Build contact info for the alert
    from_name = reply.get("from_name", from_email)
    preview   = reply.get("body", "")[:300].replace("\n", " ").strip()
    subject_orig = reply.get("subject", "")

    if is_existing:
        c = existing_contact
        name      = f"{c.get('firstName','')} {c.get('lastName','')}".strip()
        firm      = c.get("firmName", "")
        tier      = c.get("tier", 3)
        title     = f"Inbound from existing contact — {name} at {firm}"
        detail    = f"Subject: {subject_orig}\n\n{preview}"
        alert_type = "NEW_INBOUND"
        contact_info = {
            "name": name, "firm": firm, "email": from_email,
            "tier": tier, "isExisting": True, "isNew": False
        }
        log.info(f"📬 INBOUND (existing contact): {name} at {firm}")
        email_body = (
            f"Inbound email from an EXISTING contact who reached out to Pawel directly.\n\n"
            f"Contact:  {name}\n"
            f"Firm:     {firm}\n"
            f"Email:    {from_email}\n"
            f"Subject:  {subject_orig}\n\n"
            f"Their message:\n{reply.get('body','')[:800]}\n\n"
            f"Action: Log in and respond — this is a warm contact reaching out.\n"
            f"Platform: https://can-usa-outbound-production.up.railway.app"
        )
    else:
        # Unknown sender
        domain    = from_email.split("@")[-1] if "@" in from_email else ""
        firm_hint = domain.split(".")[0].capitalize() if domain else "Unknown"
        title     = f"New inbound enquiry — {from_name or from_email}"
        detail    = f"From: {from_email}\nSubject: {subject_orig}\n\n{preview}"
        alert_type = "NEW_INBOUND"
        contact_info = {
            "name": from_name, "firm": firm_hint, "email": from_email,
            "isExisting": False, "isNew": True
        }
        log.info(f"📬 NEW INBOUND (unknown): {from_email}")
        email_body = (
            f"New inbound enquiry from an UNKNOWN sender — not in your contacts.\n\n"
            f"From:     {from_name} <{from_email}>\n"
            f"Firm hint: {firm_hint}\n"
            f"Subject:  {subject_orig}\n\n"
            f"Message:\n{reply.get('body','')[:800]}\n\n"
            f"Action: If this is a good prospect, log in and add them to your contacts.\n"
            f"Platform: https://can-usa-outbound-production.up.railway.app"
        )

    alert = {
        "id":        f"inbound_{from_email.replace('@','_')}_{_now_iso()}",
        "type":      alert_type,
        "emoji":     "📬",
        "title":     title,
        "detail":    detail,
        "subject":   subject_orig,
        "contact":   contact_info,
        "timestamp": _now_iso(),
        "read":      False,
    }
    _append_alert(alert)
    _send_alert_email(
        graph,
        f"📬 {'Inbound from '+contact_info.get('name','existing contact') if is_existing else 'New inbound — '+from_email}",
        email_body,
    )
    return True



# ── Unread alerts count (used by server.py for the /api/alerts endpoint) ────

def get_unread_count() -> int:
    if not ALERTS_FILE.exists():
        return 0
    try:
        alerts = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        return sum(1 for a in alerts if not a.get("read"))
    except Exception:
        return 0


def get_alerts(limit: int = 50) -> list[dict]:
    if not ALERTS_FILE.exists():
        return []
    try:
        alerts = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        return alerts[:limit]
    except Exception:
        return []


def mark_all_read() -> None:
    if not ALERTS_FILE.exists():
        return
    try:
        alerts = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        for a in alerts:
            a["read"] = True
        ALERTS_FILE.write_text(json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not mark alerts as read: {e}")