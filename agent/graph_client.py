"""
graph_client.py — Microsoft Graph API client.

Uses Delegated auth (acts as Pawel) via a stored MSAL token cache.
Run agent/auth.py once to authenticate. The token refreshes silently
every time this client is used. No browser interaction needed after
the initial sign-in.

If the token expires (90 days of inactivity), re-run auth.py.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

import msal
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("graph_client")

GRAPH_BASE   = "https://graph.microsoft.com/v1.0"
TOKEN_FILE   = Path(__file__).parent / "token_cache.json"
PROCESSED_FOLDER_NAME = "Agent Processed"

SCOPES = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Mail.ReadWrite",
]


class GraphClient:
    """
    Delegated-auth Graph API client.
    Reads token_cache.json written by auth.py.
    Silently refreshes the access token on every call.
    """

    def __init__(self) -> None:
        self._client_id   = os.environ["AZURE_CLIENT_ID"]
        self._tenant_id   = os.environ.get("AZURE_TENANT_ID", "common")
        self._sender      = os.environ["SENDER_EMAIL"]
        self._cache       = msal.SerializableTokenCache()
        self._processed_folder_id: Optional[str] = None

        if TOKEN_FILE.exists():
            self._cache.deserialize(TOKEN_FILE.read_text())
        else:
            raise FileNotFoundError(
                f"Token cache not found at {TOKEN_FILE}. "
                "Run 'python auth.py' first to authenticate as Pawel."
            )

        self._app = msal.PublicClientApplication(
            client_id=self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
            token_cache=self._cache,
        )

    # ── Auth ───────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        accounts = self._app.get_accounts()
        if not accounts:
            raise RuntimeError(
                "No account in token cache. Run 'python auth.py' to sign in."
            )
        result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError(
                f"Token refresh failed: {result.get('error_description', 'unknown')}. "
                "Re-run 'python auth.py' to re-authenticate."
            )
        # Persist refreshed token back to disk
        TOKEN_FILE.write_text(self._cache.serialize())
        return result["access_token"]

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            f"{GRAPH_BASE}{path}", headers=self._headers(),
            params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> requests.Response:
        resp = requests.post(
            f"{GRAPH_BASE}{path}", headers=self._headers(),
            json=payload, timeout=30
        )
        resp.raise_for_status()
        return resp

    def _patch(self, path: str, payload: dict) -> requests.Response:
        resp = requests.patch(
            f"{GRAPH_BASE}{path}", headers=self._headers(),
            json=payload, timeout=30
        )
        resp.raise_for_status()
        return resp

    # ── Connection test ────────────────────────────────────────────────────

    def test_connection(self) -> None:
        me = self._get("/me")
        log.info(
            f"Graph API connected as {me.get('userPrincipalName')} "
            f"({me.get('displayName')})"
        )

    # ── Inbox polling ──────────────────────────────────────────────────────

    def get_new_replies(self) -> list[dict]:
        """
        Returns unread messages in Pawel's inbox that are replies
        to our outbound emails (subject starts with Re:).
        """
        try:
            data = self._get(
                "/me/mailFolders/inbox/messages",
                params={
                    "$filter": "isRead eq false",
                    "$select": "id,subject,from,body,receivedDateTime,conversationId",
                    "$orderby": "receivedDateTime asc",
                    "$top": "50",
                },
            )
        except requests.HTTPError as e:
            log.error(f"Failed to fetch inbox: {e}")
            return []

        results = []
        for msg in data.get("value", []):
            subject = msg.get("subject", "")
            # Process ALL unread mail — alerter.py decides what to do with
            # non-reply subjects (new inbound detection)
            from_addr = (
                msg.get("from", {}).get("emailAddress", {})
                   .get("address", "").lower().strip()
            )
            raw_body = msg.get("body", {}).get("content", "")
            results.append({
                "message_id":  msg["id"],
                "from_email":  from_addr,
                "from_name":   msg.get("from", {}).get("emailAddress", {}).get("name", ""),
                "subject":     subject,
                "body":        _strip_html(raw_body),
                "thread_body": _strip_html(raw_body),
                "received_at": msg.get("receivedDateTime", ""),
            })

        log.info(f"Found {len(results)} unread replies")
        return results

    # ── Sending ────────────────────────────────────────────────────────────

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[list[str]] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> None:
        content_type = "HTML" if html else "Text"
        content      = html if html else body
        recipients   = [{"emailAddress": {"address": to}}]
        cc_recips    = [{"emailAddress": {"address": a}} for a in (cc or [])]

        if reply_to_message_id:
            self._post(
                f"/me/messages/{reply_to_message_id}/reply",
                {"message": {
                    "body": {"contentType": content_type, "content": content},
                    "toRecipients": recipients,
                }, "saveToSentItems": True},
            )
        else:
            self._post(
                "/me/sendMail",
                {"message": {
                    "subject": subject,
                    "body": {"contentType": content_type, "content": content},
                    "toRecipients": recipients,
                    "ccRecipients": cc_recips,
                }, "saveToSentItems": True},
            )
        log.info(f"Sent to {to} — {subject!r} ({content_type})")

    # ── Message management ─────────────────────────────────────────────────

    def mark_as_read(self, message_id: str) -> None:
        try:
            self._patch(f"/me/messages/{message_id}", {"isRead": True})
        except Exception as e:
            log.warning(f"Could not mark {message_id} as read: {e}")

    def move_to_processed(self, message_id: str) -> None:
        try:
            folder_id = self._get_or_create_processed_folder()
            self._post(f"/me/messages/{message_id}/move", {"destinationId": folder_id})
        except Exception as e:
            log.warning(f"Could not move {message_id}: {e}")

    def _get_or_create_processed_folder(self) -> str:
        if self._processed_folder_id:
            return self._processed_folder_id
        data = self._get(
            "/me/mailFolders",
            params={"$filter": f"displayName eq '{PROCESSED_FOLDER_NAME}'"},
        )
        folders = data.get("value", [])
        if folders:
            self._processed_folder_id = folders[0]["id"]
        else:
            result = self._post(
                "/me/mailFolders",
                {"displayName": PROCESSED_FOLDER_NAME}
            )
            self._processed_folder_id = result.json()["id"]
            log.info(f"Created folder: {PROCESSED_FOLDER_NAME}")
        return self._processed_folder_id


# ── Helpers ────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    for tag in ["</p>", "</div>", "<br>", "<br/>", "<br />"]:
        html = html.replace(tag, "\n")
    plain = re.sub(r"<[^>]+>", "", html)
    plain = (plain
        .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\n{3,}", "\n\n", plain).strip()