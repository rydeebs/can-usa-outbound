"""
contact_store.py — Contact database layer.

Reads and writes from the same PostgreSQL database that server.py uses.
The entire app state (contacts, seqEmails, etc.) is stored as a JSONB
blob in the app_state table — this is the single source of truth.

Falls back to local data/contacts.json if DATABASE_URL is not set
(for local development without Postgres).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("contact_store")

DATA_DIR       = Path(__file__).parent.parent / "data"
CONTACTS_FILE  = DATA_DIR / "contacts.json"
SEQ_FILE       = DATA_DIR / "seq_emails.json"
REPLY_LOG_FILE = DATA_DIR / "reply_log.json"

DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("POSTGRES_URL") or
    os.environ.get("POSTGRESQL_URL") or
    os.environ.get("DATABASE_PRIVATE_URL") or
    os.environ.get("POSTGRES_PRIVATE_URL") or
    ""
)


# ── Postgres helpers ───────────────────────────────────────────────────────

def _get_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _read_state() -> dict:
    """Read the full app state from Postgres."""
    if not DATABASE_URL:
        return _read_state_file()
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
        log.error(f"Postgres read error: {e}")
    return {"contacts": [], "seqEmails": {}}


def _write_state(state: dict) -> None:
    """Write the full app state back to Postgres."""
    if not DATABASE_URL:
        _write_state_file(state)
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
        log.error(f"Postgres write error: {e}")
        raise


# ── File fallback (local dev) ──────────────────────────────────────────────

def _read_state_file() -> dict:
    contacts, seq = [], {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONTACTS_FILE.exists():
        try:
            contacts = json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if SEQ_FILE.exists():
        try:
            seq = json.loads(SEQ_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"contacts": contacts, "seqEmails": seq}


def _write_state_file(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "contacts" in state:
        CONTACTS_FILE.write_text(
            json.dumps(state["contacts"], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    if "seqEmails" in state:
        SEQ_FILE.write_text(
            json.dumps(state["seqEmails"], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )


# ── ContactStore ───────────────────────────────────────────────────────────

class ContactStore:
    """
    Unified interface for reading and writing contacts.
    Reads/writes to PostgreSQL (or local JSON in dev).
    """

    # ── Reading ────────────────────────────────────────────────────────────

    def all(self) -> list[dict]:
        return _read_state().get("contacts", [])

    def get(self, contact_id: int) -> Optional[dict]:
        return next(
            (c for c in self.all() if c.get("id") == contact_id), None
        )

    def get_by_email(self, email: str) -> Optional[dict]:
        email = email.lower().strip()
        return next(
            (c for c in self.all() if c.get("workEmail", "").lower() == email),
            None,
        )

    def get_pending_sequence_sends(self) -> list[dict]:
        return [
            c for c in self.all()
            if c.get("emailSent")
            and not c.get("replied")
            and not c.get("paused")
            and c.get("sequenceStep", 0) < 4
        ]

    def get_for_review(self) -> list[dict]:
        return [c for c in self.all() if c.get("pendingReview")]

    # ── Writing ────────────────────────────────────────────────────────────

    def update(self, contact_id: int, changes: dict) -> None:
        state = _read_state()
        contacts = state.get("contacts", [])
        updated = False
        for i, c in enumerate(contacts):
            if c.get("id") == contact_id:
                contacts[i] = {**c, **changes}
                updated = True
                break
        if not updated:
            log.warning(f"update() called with unknown contact_id={contact_id}")
            return
        state["contacts"] = contacts
        _write_state(state)
        log.debug(f"Updated contact {contact_id}: {list(changes.keys())}")

    def queue_for_review(
        self,
        contact_id: int,
        original_reply: dict,
        generated_body: str,
        evaluation: dict,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.update(contact_id, {
            "pendingReview":      True,
            "pendingReplyBody":   generated_body,
            "pendingReplySubject": f"Re: {original_reply.get('subject', '')}",
            "pendingReplyAt":     now,
            "lastReplyFrom":      original_reply.get("from_email", ""),
            "lastReplyText":      original_reply.get("body", "")[:500],
            "evaluationScore":    evaluation.get("score"),
        })
        self._append_reply_log({
            "timestamp":       now,
            "contactId":       contact_id,
            "fromEmail":       original_reply.get("from_email"),
            "replyText":       original_reply.get("body", "")[:300],
            "generatedBody":   generated_body,
            "evaluationPass":  evaluation.get("pass"),
            "evaluationScore": evaluation.get("score"),
            "evaluationFlags": evaluation.get("flags", []),
        })
        log.info(
            f"Queued reply for review — contact {contact_id}, "
            f"score {evaluation.get('score')}"
        )

    def mark_reply_sent(self, contact_id: int) -> None:
        contact = self.get(contact_id)
        if not contact:
            return
        self.update(contact_id, {
            "pendingReview":      False,
            "pendingReplyBody":   None,
            "pendingReplySubject": None,
            "replied":            True,
            "sequenceStep":       contact.get("sequenceStep", 0) + 1,
        })

    # ── Sequence email history ─────────────────────────────────────────────

    def get_seq_emails(self, contact_id: int) -> dict:
        state = _read_state()
        return state.get("seqEmails", {}).get(str(contact_id), {})

    def save_seq_email(
        self, contact_id: int, step: int, subject: str, body: str
    ) -> None:
        state = _read_state()
        seq = state.get("seqEmails", {})
        key = str(contact_id)
        if key not in seq:
            seq[key] = {}
        seq[key][str(step)] = {
            "subject": subject,
            "body": body,
            "sentAt": datetime.now(timezone.utc).isoformat(),
        }
        state["seqEmails"] = seq
        _write_state(state)

    # ── Reply log ──────────────────────────────────────────────────────────

    def _append_reply_log(self, entry: dict) -> None:
        """Append to reply log stored in Postgres state or local file."""
        try:
            state = _read_state()
            log_data = state.get("replyLog", [])
            log_data.insert(0, entry)
            log_data = log_data[:500]  # cap at 500 entries
            state["replyLog"] = log_data
            _write_state(state)
        except Exception as e:
            log.warning(f"Could not append reply log: {e}")