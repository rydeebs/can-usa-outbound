"""
sequence_engine.py — Sequence step logic and timing.

Determines when each follow-up should be sent based on the number of days
since the previous email, and generates default follow-up bodies when no
custom body has been set in seq_emails.json.

Works entirely through ContactStore — never touches contacts.json directly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from contact_store import ContactStore

log = logging.getLogger("sequence_engine")

# Sequence timing: step index → days since previous step
# Step 0 is the initial email (manually sent via frontend).
# Steps 1–4 are follow-ups the agent can trigger.
SEQUENCE_DAYS = {
    0: 0,   # Initial — sent manually
    1: 4,   # Follow-up A — 4 days after initial
    2: 8,   # Follow-up B — 8 days after initial (4 days after A)
    3: 12,  # Follow-up C — 12 days after initial
    4: 21,  # Follow-up D — 21 days after initial
}

# How many days after the initial send before step N should fire
DAYS_FROM_INITIAL = {
    1: 4,
    2: 8,
    3: 12,
    4: 21,
}


def sequence_reply_subject(contact: dict) -> str:
    """All follow-ups should reply to the original outbound thread."""
    original = (contact.get("subjectLine") or f"{contact.get('firmName', '')} + CAN USA").strip()
    if not original:
        original = "CAN USA"
    return original if original.lower().startswith("re:") else f"Re: {original}"


class SequenceEngine:
    """
    Answers two questions:
      1. "Is this contact due for their next follow-up today?"
      2. "What should the follow-up email say?"
    """

    def __init__(self, store: ContactStore) -> None:
        self._store = store

    # ── Step timing ────────────────────────────────────────────────────────

    def get_due_contacts(self) -> list[tuple[dict, int]]:
        """
        Returns a list of (contact, next_step) tuples for contacts whose
        next sequence step is due today or overdue.

        A step is due when:
          - The contact has emailSent=True and replied=False and paused=False
          - The current step is < 4 (there are follow-ups remaining)
          - The number of days since the initial send >= DAYS_FROM_INITIAL[next_step]

        The initial send date is inferred from seq_emails.json step 0 sentAt field,
        falling back to "today" if not recorded.
        """
        pending = self._store.get_pending_sequence_sends()
        due = []
        now = datetime.now(timezone.utc)

        for contact in pending:
            # The frontend stores sequenceStep as the next step to send:
            # 1 after the initial email, 2 after day-4, etc.
            step_to_send = contact.get("sequenceStep", 0)
            if step_to_send < 1 or step_to_send > 4:
                continue  # All follow-ups sent
            already_sent = self._store.get_seq_emails(contact["id"]).get(str(step_to_send), {})
            if isinstance(already_sent, dict) and already_sent.get("sentAt"):
                log.warning(
                    "%s %s — step %s already has sentAt=%s; advancing pointer without resending",
                    contact.get("firstName", ""),
                    contact.get("lastName", ""),
                    step_to_send,
                    already_sent.get("sentAt"),
                )
                self._store.update(contact["id"], {"sequenceStep": step_to_send + 1})
                continue

            # Find when the initial email was sent
            initial_sent_at = self._get_initial_send_date(contact["id"])
            if initial_sent_at is None:
                # No record — skip to avoid sending prematurely
                continue

            days_required = DAYS_FROM_INITIAL.get(step_to_send, 99)
            due_at = initial_sent_at + timedelta(days=days_required)

            if now >= due_at:
                days_elapsed = (now - initial_sent_at).days
                log.info(
                    f"{contact['firstName']} {contact['lastName']} — "
                    f"step {step_to_send} due at {due_at.isoformat()} "
                    f"({days_elapsed}d elapsed, {days_required}d required)"
                )
                due.append((contact, step_to_send))

        return due

    def _get_initial_send_date(self, contact_id: int) -> Optional[datetime]:
        """Returns the datetime when step 0 was sent, or None if not recorded."""
        seq = self._store.get_seq_emails(contact_id)
        step0 = seq.get("0", {})
        sent_at_str = step0.get("sentAt")
        if not sent_at_str:
            contact = self._store.get(contact_id)
            if contact:
                sent_at_str = (
                    contact.get("initialEmailSentAt")
                    or contact.get("emailSentAt")
                    or contact.get("sentAt")
                )
        if sent_at_str:
            try:
                return datetime.fromisoformat(sent_at_str)
            except ValueError:
                pass
        return None

    # ── Email content ──────────────────────────────────────────────────────

    def get_last_sent_email(self, contact_id: int) -> str:
        """
        Returns the body of the most recently sent email to this contact.
        Used by responder.py as context when generating a reply.
        """
        seq = self._store.get_seq_emails(contact_id)
        contact = self._store.get(contact_id)
        current_step = contact.get("sequenceStep", 0) if contact else 0

        # Walk backward from current step to find the last sent email
        for step in range(current_step, -1, -1):
            entry = seq.get(str(step))
            if entry and entry.get("body"):
                return entry["body"]

        # Fall back to the original email body on the contact record
        if contact:
            return contact.get("refinedEmailBody") or contact.get("emailBody", "")
        return ""

    def get_followup_body(self, contact: dict, step: int) -> tuple[str, str]:
        """
        Returns (subject, body) for a follow-up step.

        Checks seq_emails.json first — if Pawel or the agent has already
        customized the body for this step, use that. Otherwise generates
        a default follow-up body based on the contact's data.

        Returns: (subject_line, email_body)
        """
        # Check for a custom body set in the frontend
        seq = self._store.get_seq_emails(contact["id"])
        custom = seq.get(str(step))
        if custom and custom.get("body"):
            return sequence_reply_subject(contact), custom["body"]

        # Generate a default follow-up
        return self._default_followup(contact, step)

    def _default_followup(self, contact: dict, step: int) -> tuple[str, str]:
        """
        Default follow-up templates. These mirror what the frontend generates
        and serve as a fallback when no custom body exists.
        """
        first = contact["firstName"]
        firm = contact["firmName"]
        sub10a = contact.get("sub10A", 0)
        swarmp = contact.get("wPriorSWARM", 0)
        orig_subject = contact.get("subjectLine", f"{firm} + CAN USA")
        subject = sequence_reply_subject(contact)

        if step == 1:
            body = (
                f"Hi {first},\n\n"
                f"Just following up to make sure my previous note didn't get buried. "
                f"{firm} has {sub10a} buildings in sub-cycle 10A with the 2/21/2027 "
                f"deadline approaching.\n\n"
                f"Happy to keep it to 15 minutes. What works over the next week or two?"
            )

        elif step == 2:
            body = (
                f"Hi {first},\n\n"
                f"One more angle: {firm} has {swarmp} buildings with prior SWARMP "
                f"classifications. At sub-cycle 10A close (2/21/2027), those auto-classify "
                f"as Unsafe if unfiled, triggering mandatory sidewalk sheds or netting.\n\n"
                f"Rope access clears those buildings faster. And if any turn up Unsafe, "
                f"we install DOB-compliant containment netting as the alternative to "
                f"sidewalk sheds.\n\n"
                f"Worth 15 minutes?"
            )

        elif step == 3:
            body = (
                f"Hi {first},\n\n"
                f"I'll be direct: sub-cycle 10A closes 2/21/2027. {firm} has {sub10a} "
                f"buildings to file. Scaffold at 2–4 weeks per building means you're "
                f"compressing against the deadline.\n\n"
                f"Rope access at 2–3 days per building is the only realistic path through "
                f"{sub10a} buildings before February.\n\n"
                f"Do you have 15 minutes this week?"
            )

        elif step == 4:
            body = (
                f"Hi {first},\n\n"
                f"This is my last follow-up.\n\n"
                f"If the 10A deadline (2/21/2027) ever becomes a live problem for {firm}, "
                f"we're the fastest path through it. Just reply and I'll get something "
                f"on the calendar same day.\n\n"
                f"Wishing you a smooth inspection season."
            )

        else:
            subject = f"Re: {orig_subject}"
            body = f"Hi {first},\n\nFollowing up on my previous note. Would a quick call work?"

        return subject, body

    # ── Step advancement ───────────────────────────────────────────────────

    def record_step_sent(
        self, contact_id: int, step: int, subject: str, body: str,
        thread_id: str = None,
    ) -> None:
        """
        Records that a sequence step was sent. Updates sequenceStep on the contact
        and saves the email body to seq_emails.json for future reference.
        """
        self._store.save_seq_email(contact_id, step, subject, body)
        updates = {"sequenceStep": step + 1, "emailSent": True}
        if thread_id:
            updates["gmailThreadId"] = thread_id
        self._store.update(contact_id, updates)
        log.info(f"Recorded step {step} sent for contact {contact_id}")
