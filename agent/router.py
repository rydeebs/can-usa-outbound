"""
router.py — Step 1 of the pipeline.
Classifies incoming replies into categories for downstream handling.
Uses claude-haiku-4-5 by default (fast, cheap, classification task only).
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
import anthropic

log = logging.getLogger("router")
client = anthropic.Anthropic()
PROMPT = (Path(__file__).parent / "prompts" / "router.md").read_text()


def classify_reply(reply_text: str, contact: dict) -> dict:
    """
    Returns routing dict with keys:
    category, urgency, key_points, recommended_model, auto_send_safe
    """
    user_message = f"""
Contact: {contact['firstName']} {contact['lastName']} at {contact['firmName']}
Sequence step: {contact.get('sequenceStep', 0)} of 4
Their reply:
---
{reply_text}
---
Classify this reply.
"""
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        system=PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"Router returned non-JSON: {raw}")
        result = {
            "category": "UNKNOWN",
            "urgency": "MEDIUM",
            "key_points": [],
            "recommended_model": "claude-sonnet-4-5",
            "auto_send_safe": False,
        }
    log.info(f"Classified as {result.get('category')} / {result.get('urgency')}")
    return result
