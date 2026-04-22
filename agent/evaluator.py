"""
evaluator.py — Step 3 of the pipeline.
Quality-checks the draft reply. If it fails, main.py asks the responder to fix it.
Uses claude-haiku-4-5 — evaluation is a structured task, not creative.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
import anthropic

log = logging.getLogger("evaluator")
client = anthropic.Anthropic()
PROMPT = (Path(__file__).parent / "prompts" / "evaluator.md").read_text()


def evaluate_reply(draft: str, route: dict) -> dict:
    """
    Returns verdict dict with keys: pass, score, flags, fix_instructions
    """
    user_message = f"""
Reply category: {route.get('category')}
Key points that must be addressed: {', '.join(route.get('key_points', []))}

Draft email to evaluate:
---
{draft}
---
Evaluate this draft.
"""
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        system=PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"Evaluator returned non-JSON: {raw}")
        result = {"pass": False, "score": 5, "flags": ["parse_error"], "fix_instructions": "Rewrite the response cleanly."}
    if not result.get("pass"):
        log.warning(f"Evaluation failed: {result.get('flags')} — {result.get('fix_instructions')}")
    return result
