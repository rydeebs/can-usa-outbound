"""
template_engine.py — HTML email template engine.

Loads HTML template files from the /templates directory, one per
Tier × Template combination (T1-A.html through T3-C.html).

Substitutes {{placeholders}} with real contact data and email content,
then returns the finished HTML string ready to pass to graph_client.send_email().

If no template exists for a contact's tier+template combo, returns None —
the caller (main.py) falls back to sending plain text.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("template_engine")

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class TemplateEngine:
    """
    Loads and applies HTML email templates.

    Templates live in /templates/T{tier}-{tpl}.html
    e.g.:  templates/T1-B.html  (Tier 1, Template B — Backlog-led)
           templates/T2-A.html  (Tier 2, Template A — LinkedIn hook)

    Design your templates in Claude Artifacts or any HTML editor.
    Use the {{placeholder}} syntax anywhere in the HTML.
    """

    def __init__(self) -> None:
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public interface ───────────────────────────────────────────────────

    def apply(
        self,
        contact: dict,
        email_body: str,
        subject: str,
        signature_html: str = "",
    ) -> Optional[str]:
        """
        Returns the finished HTML string for this contact, or None if no
        template exists for their tier+template combo.

        Args:
            contact:        Contact dict from ContactStore
            email_body:     Plain-text email body from the responder agent
            subject:        Email subject line
            signature_html: HTML signature from the frontend settings (optional)

        Returns:
            Rendered HTML string, or None if no template is configured.
        """
        tier = contact.get("tier", 1)
        tpl = contact.get("templateUsed", "B")
        template_html = self._load_template(tier, tpl)

        if template_html is None:
            log.debug(f"No HTML template for T{tier}-{tpl} — falling back to plain text")
            return None

        return self._render(
            template_html=template_html,
            contact=contact,
            email_body=email_body,
            subject=subject,
            signature_html=signature_html,
        )

    def has_template(self, tier: int, tpl: str) -> bool:
        """Returns True if a template file exists for this tier+template combo."""
        return self._template_path(tier, tpl).exists()

    def list_templates(self) -> list[str]:
        """Returns a list of all configured template keys, e.g. ['T1-B', 'T2-A']."""
        return [
            f.stem
            for f in TEMPLATES_DIR.glob("T?-?.html")
        ]

    # ── Template loading ───────────────────────────────────────────────────

    def _template_path(self, tier: int, tpl: str) -> Path:
        return TEMPLATES_DIR / f"T{tier}-{tpl}.html"

    def _load_template(self, tier: int, tpl: str) -> Optional[str]:
        path = self._template_path(tier, tpl)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            log.error(f"Could not read template {path}: {e}")
            return None

    # ── Rendering ──────────────────────────────────────────────────────────

    def _render(
        self,
        template_html: str,
        contact: dict,
        email_body: str,
        subject: str,
        signature_html: str,
    ) -> str:
        """
        Substitutes all {{placeholders}} in the template HTML.

        Available placeholders (tell your email designer to use these):

        Contact data:
          {{firstName}}       — e.g. "Richard"
          {{lastName}}        — e.g. "Koenigsberg"
          {{fullName}}        — e.g. "Richard Koenigsberg"
          {{firmName}}        — e.g. "Koenigsberg Engineering"
          {{workEmail}}       — e.g. "r.koenigsberg@koenigsbergeng.com"

        FISP data:
          {{totalUnfiled}}    — e.g. "397"
          {{sub10A}}          — e.g. "153"
          {{sub10B}}          — e.g. "124"
          {{sub10C}}          — e.g. "120"
          {{wPriorSWARM}}     — e.g. "89"
          {{deadline10A}}     — "2/21/2027"
          {{deadline10B}}     — "2/21/2028"
          {{deadline10C}}     — "2/21/2029"
          {{tier}}            — "1", "2", or "3"

        Email content:
          {{subject}}         — the email subject line
          {{emailBody}}       — plain-text body converted to HTML paragraphs
          {{emailBodyPlain}}  — plain-text body as-is (use inside <pre> tags)
          {{signature}}       — HTML signature from the frontend settings

        Sender (from SENDER_EMAIL env or defaults):
          {{senderName}}      — e.g. "Pawel Wojcik"
          {{senderTitle}}     — e.g. "Special Rigger | CAN USA"
          {{senderEmail}}     — e.g. "pawel@canusa.com"
          {{senderPhone}}     — e.g. "(212) 555-0180"
        """
        # Convert plain-text body to HTML paragraphs
        email_body_html = _plain_to_html_paragraphs(email_body)

        # Build the substitution map
        subs = {
            # Contact
            "firstName":    contact.get("firstName", ""),
            "lastName":     contact.get("lastName", ""),
            "fullName":     f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
            "firmName":     contact.get("firmName", ""),
            "workEmail":    contact.get("workEmail", ""),

            # FISP
            "totalUnfiled": str(contact.get("totalUnfiled", 0)),
            "sub10A":       str(contact.get("sub10A", 0)),
            "sub10B":       str(contact.get("sub10B", 0)),
            "sub10C":       str(contact.get("sub10C", 0)),
            "wPriorSWARM":  str(contact.get("wPriorSWARM", 0)),
            "deadline10A":  "2/21/2027",
            "deadline10B":  "2/21/2028",
            "deadline10C":  "2/21/2029",
            "tier":         str(contact.get("tier", 1)),

            # Email content
            "subject":         subject,
            "emailBody":       email_body_html,
            "emailBodyPlain":  email_body,
            "signature":       signature_html or "",

            # Sender defaults (can be overridden by template)
            "senderName":   "Pawel Wojcik",
            "senderTitle":  "Special Rigger | CAN USA",
            "senderEmail":  "pawel@canusa.com",
            "senderPhone":  "(212) 555-0180",
        }

        rendered = template_html
        for key, value in subs.items():
            rendered = rendered.replace("{{" + key + "}}", value)

        # Warn about any remaining unresolved placeholders
        remaining = re.findall(r"\{\{(\w+)\}\}", rendered)
        if remaining:
            log.warning(f"Unresolved template placeholders: {remaining}")

        return rendered


# ── Helpers ────────────────────────────────────────────────────────────────

def _plain_to_html_paragraphs(plain: str) -> str:
    """
    Converts a plain-text email body (with blank-line paragraph breaks)
    into HTML <p> tags suitable for embedding in an HTML email template.

    Single newlines become <br>, blank lines become new <p> blocks.
    """
    if not plain:
        return ""

    # Split on double newlines (paragraph breaks)
    paragraphs = re.split(r"\n\n+", plain.strip())
    html_parts = []
    for para in paragraphs:
        # Within a paragraph, single newlines become <br>
        para_html = para.replace("\n", "<br/>")
        html_parts.append(f"<p style='margin:0 0 14px 0;'>{para_html}</p>")

    return "\n".join(html_parts)