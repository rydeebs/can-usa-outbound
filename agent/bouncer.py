"""
bouncer.py — Hard bounce detection and contact removal.

Detects messages from mailer-daemon / delivery failure notifications,
extracts the invalid email address, removes the contact from app_state,
and logs the removed contact to a permanent bounces table in Postgres.

Called from main.py for every unread message before other processing.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("bouncer")

DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("POSTGRES_URL") or
    os.environ.get("POSTGRESQL_URL") or
    os.environ.get("DATABASE_PRIVATE_URL") or
    os.environ.get("POSTGRES_PRIVATE_URL") or
    ""
)

# ── Sender patterns that indicate a bounce notification ──────────────────
BOUNCE_SENDERS = [
    "mailer-daemon@googlemail.com",
    "mailer-daemon@google.com",
    "mailer-daemon",
    "postmaster@",
    "mail-delivery-subsystem",
    "noreply@bounce",
    "delivery-status",
    "mail delivery subsystem",
]

# ── Subject patterns for bounce emails ────────────────────────────────────
BOUNCE_SUBJECTS = [
    "delivery status notification",
    "undeliverable",
    "mail delivery failed",
    "failed delivery",
    "address not found",
    "delivery failure",
    "returned mail",
    "message not delivered",
    "delivery incomplete",
]

# ── Regex patterns to extract the bounced email address from the body ─────
BOUNCE_EMAIL_PATTERNS = [
    # "Your message wasn't delivered to email@domain.com because"
    r"wasn't delivered to\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    # "The email account that you tried to reach does not exist. email@domain.com"
    r"does not exist.*?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    # "Final-Recipient: rfc822; email@domain.com"
    r"Final-Recipient:.*?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    # "Original-Recipient: rfc822; email@domain.com"
    r"Original-Recipient:.*?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    # "to: email@domain.com" in a delivery report context
    r"(?:delivered to|recipient|address)[:\s]+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    # Generic: any email address in a bounce body
    r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
]


# ── Postgres helpers ───────────────────────────────────────────────────────

def _get_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _init_bounces_table() -> None:
    """Create the bounced_contacts table if it doesn't exist."""
    if not DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bounced_contacts (
                        id              SERIAL PRIMARY KEY,
                        email           TEXT NOT NULL,
                        full_name       TEXT,
                        firm_name       TEXT,
                        tier            INTEGER,
                        subject_line    TEXT,
                        bounced_at      TIMESTAMP DEFAULT NOW(),
                        bounce_reason   TEXT,
                        contact_data    JSONB
                    );
                    CREATE INDEX IF NOT EXISTS idx_bounced_email
                        ON bounced_contacts(email);
                """)
        conn.close()
        log.info("Bounces table ready.")
    except Exception as e:
        log.error(f"Bounces table init error: {e}")


def _log_bounce(contact: dict, bounce_reason: str) -> None:
    """Save the removed contact to the bounced_contacts table."""
    if not DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bounced_contacts
                        (email, full_name, firm_name, tier, subject_line, bounce_reason, contact_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                """, (
                    contact.get("workEmail", "").lower(),
                    (contact.get("fullName") or
                     f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()),
                    contact.get("firmName", ""),
                    contact.get("tier"),
                    contact.get("subjectLine", ""),
                    bounce_reason[:500],
                    json.dumps(contact, ensure_ascii=False),
                ))
        conn.close()
        log.info(f"Logged bounce for {contact.get('workEmail')}")
    except Exception as e:
        log.warning(f"Could not log bounce: {e}")


def _get_bounces(limit: int = 100) -> list[dict]:
    """Returns the list of bounced contacts from Postgres."""
    if not DATABASE_URL:
        return []
    try:
        import psycopg2.extras
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT email, full_name, firm_name, tier, subject_line,
                       bounced_at, bounce_reason
                FROM bounced_contacts
                ORDER BY bounced_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"Could not fetch bounces: {e}")
        return []


# ── Bounce detection ───────────────────────────────────────────────────────

def is_bounce_message(reply: dict) -> bool:
    """Returns True if this message looks like a delivery failure notification."""
    from_email = reply.get("from_email", "").lower()
    subject    = reply.get("subject", "").lower()

    sender_match = any(s in from_email for s in BOUNCE_SENDERS)
    subject_match = any(s in subject for s in BOUNCE_SUBJECTS)

    return sender_match or subject_match


def extract_bounced_email(body: str) -> Optional[str]:
    """
    Tries to extract the invalid email address from a bounce notification body.
    Returns the email address string or None if not found.
    """
    if not body:
        return None

    for pattern in BOUNCE_EMAIL_PATTERNS:
        matches = re.findall(pattern, body, re.IGNORECASE | re.DOTALL)
        for match in matches:
            email = match.lower().strip()
            # Skip the mailer-daemon sender itself and common false positives
            if any(skip in email for skip in [
                "mailer-daemon", "postmaster", "google.com",
                "googleapis", "googlemail", "bounce", "noreply"
            ]):
                continue
            # Basic email validation
            if re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
                return email

    return None


def handle_bounce(
    reply: dict,
    store,  # ContactStore instance
    graph=None,
) -> Optional[str]:
    """
    Main entry point called from main.py.

    1. Checks if the message is a bounce notification
    2. Extracts the bounced email address
    3. Finds the contact in app_state
    4. Removes them from contacts
    5. Logs to bounced_contacts table
    6. Creates an alert for the platform UI

    Returns the bounced email address if handled, None otherwise.
    """
    if not is_bounce_message(reply):
        return None

    body          = reply.get("body", "")
    bounced_email = extract_bounced_email(body)

    if not bounced_email:
        log.warning(f"Bounce detected but could not extract email from: {body[:200]}")
        return None

    log.info(f"Bounce detected for: {bounced_email}")

    # Find the contact
    contact = store.get_by_email(bounced_email)
    if not contact:
        log.info(f"Bounced email {bounced_email} not in contacts — already removed or never added")
        _log_bounce(
            {"workEmail": bounced_email, "firmName": "Unknown", "tier": None},
            f"Hard bounce. Original error: {body[:300]}"
        )
        return bounced_email

    # Log to Postgres before removing
    _log_bounce(contact, f"Hard bounce — address not found. Notification: {body[:300]}")

    # Remove from contacts in app_state
    try:
        state = _read_app_state()
        contacts = state.get("contacts", [])
        before = len(contacts)
        contacts = [
            c for c in contacts
            if c.get("workEmail", "").lower() != bounced_email
        ]
        after = len(contacts)
        if before != after:
            state["contacts"] = contacts
            _write_app_state(state)
            log.info(f"Removed bounced contact {bounced_email} from contacts ({before} → {after})")
        else:
            log.warning(f"Contact {bounced_email} not found in state for removal")
    except Exception as e:
        log.error(f"Error removing bounced contact: {e}", exc_info=True)

    # Write to alerts log so it shows in the platform Alerts tab
    _write_bounce_alert(contact, body)

    # Send email notification
    if graph and os.environ.get("ALERT_EMAIL"):
        name = contact.get("fullName") or f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
        firm = contact.get("firmName", "")
        try:
            graph.send_email(
                to=os.environ["ALERT_EMAIL"],
                subject=f"⚠️ Bounced email — {name} at {firm} removed",
                body=(
                    f"Hard bounce detected and contact removed.\n\n"
                    f"Email:   {bounced_email}\n"
                    f"Contact: {name}\n"
                    f"Firm:    {firm}\n"
                    f"Tier:    {contact.get('tier')}\n\n"
                    f"Bounce notification:\n{body[:800]}\n\n"
                    f"The contact has been removed from your contacts list and logged "
                    f"in the Bounced contacts log (Alerts tab → Bounced)."
                ),
            )
        except Exception as e:
            log.warning(f"Could not send bounce alert email: {e}")

    return bounced_email


# ── App state helpers (duplicated here to avoid circular imports) ──────────

def _read_app_state() -> dict:
    if not DATABASE_URL:
        return {"contacts": []}
    try:
        import psycopg2.extras
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT data FROM app_state WHERE id = 1;")
            row = cur.fetchone()
        conn.close()
        if row:
            d = row["data"]
            return d if isinstance(d, dict) else json.loads(d)
    except Exception as e:
        log.error(f"State read error: {e}")
    return {"contacts": []}


def _write_app_state(state: dict) -> None:
    if not DATABASE_URL:
        return
    try:
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
    except Exception as e:
        log.error(f"State write error: {e}")
        raise


def _write_bounce_alert(contact: dict, bounce_body: str) -> None:
    """Write a bounce alert to data/alerts.json for the platform Alerts tab."""
    try:
        alerts_file = Path(__file__).parent.parent / "data" / "alerts.json"
        alerts_file.parent.mkdir(parents=True, exist_ok=True)
        alerts = []
        if alerts_file.exists():
            try:
                alerts = json.loads(alerts_file.read_text(encoding="utf-8"))
            except Exception:
                alerts = []

        name  = contact.get("fullName") or f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
        firm  = contact.get("firmName", "")
        email = contact.get("workEmail", "")

        alert = {
            "id":        f"bounce_{email.replace('@','_')}_{datetime.now(timezone.utc).isoformat()}",
            "type":      "BOUNCE",
            "emoji":     "⚠️",
            "title":     f"Bounced — {name} at {firm} removed",
            "detail":    f"Email address not found: {email}",
            "subject":   "Hard bounce — address not found",
            "contact":   {
                "name":  name,
                "firm":  firm,
                "email": email,
                "tier":  contact.get("tier"),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read":      False,
        }
        alerts.insert(0, alert)
        alerts = alerts[:200]
        alerts_file.write_text(
            json.dumps(alerts, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"Could not write bounce alert: {e}")