"""
responder.py — Step 2 of the pipeline.
Generates the email reply using SOUL.md as identity context + responder.md as task instructions.
Model is determined by the router (haiku for simple, sonnet for nuanced).
"""
from __future__ import annotations
import logging
from pathlib import Path
import anthropic

log = logging.getLogger("responder")
client = anthropic.Anthropic()

SOUL = (Path(__file__).parent.parent / "SOUL.md").read_text()
RESPONDER_PROMPT = (Path(__file__).parent / "prompts" / "responder.md").read_text()

# Combine: SOUL.md gives identity, responder.md gives task instructions
SYSTEM_PROMPT = f"{SOUL}\n\n---\n\n{RESPONDER_PROMPT}"


def generate_reply(
    contact: dict,
    reply_text: str,
    previous_email: str,
    route: dict,
    model: str = "claude-sonnet-4-5",
    fix_instructions: str = "",
) -> str:
    """
    Returns the email body as a plain string.
    No sign-off — the caller appends the signature.
    """
    user_parts = [
        f"Contact: {contact['firstName']} {contact['lastName']} at {contact['firmName']}",
        f"Tier: {contact.get('tier', 3)} | Template: {contact.get('templateUsed', 'B')}",
        f"Total unfiled: {contact.get('totalUnfiled', 0)} | Sub-10A: {contact.get('sub10A', 0)} | SWARMP: {contact.get('wPriorSWARM', 0)}",
        f"Sequence step: {contact.get('sequenceStep', 0)} of 4",
        f"Reply category: {route.get('category')} | Urgency: {route.get('urgency')}",
        f"Key points to address: {', '.join(route.get('key_points', []))}",
        "",
        "--- Previous email Pawel sent ---",
        previous_email or "(no previous email on record)",
        "",
        "--- Contact's reply ---",
        reply_text,
    ]

    if fix_instructions:
        user_parts += ["", f"--- Fix instructions from evaluator ---", fix_instructions]

    user_parts.append("\nWrite Pawel's reply. Return only the email body.")

    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n".join(user_parts)}],
    )
    body = response.content[0].text.strip()
    log.info(f"Generated reply for {contact['firstName']} ({len(body.split())} words) using {model}")
    return body
