"""
linkedin_mcp_client.py — LinkedIn remote MCP client.

This app never stores LinkedIn credentials or automates linkedin.com directly.
It calls a configured LinkedIn MCP server that owns account auth, rate limits,
and the actual connection-request workflow.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger("linkedin_mcp_client")


class LinkedInMCPNotConfigured(RuntimeError):
    """Raised when the LinkedIn MCP integration is not configured."""


class LinkedInMCPError(RuntimeError):
    """Raised when the LinkedIn MCP server returns an error."""


@dataclass
class LinkedInMCPResult:
    ok: bool
    action: str
    tool: Optional[str]
    response: dict[str, Any]


class LinkedInMCPClient:
    """
    Minimal JSON-RPC client for a LinkedIn remote MCP server.

    Env vars:
      LINKEDIN_MCP_SERVER_URL Remote MCP URL for your LinkedIn MCP server
      LINKEDIN_MCP_API_BASE_URL Optional Unipile API base URL/DSN
      LINKEDIN_MCP_API_KEY    Optional bearer/API key for your MCP gateway
      LINKEDIN_MCP_ACCOUNT_ID Optional connected LinkedIn account id
      LINKEDIN_MCP_CONTACT_TOOL Optional exact MCP tool name for contact upsert
      LINKEDIN_MCP_CONNECT_TOOL   Optional exact MCP tool name for LinkedIn invite
      LINKEDIN_MCP_LIST_TOOL  Optional exact MCP tool name for list/campaign add
      LINKEDIN_MCP_LIST_NAME  Optional list name for queued contacts
      LINKEDIN_MCP_CAMPAIGN_NAME Optional campaign name for campaign-aware tools
    """

    def __init__(self, server_url: str | None = None, api_key: str | None = None) -> None:
        self.server_url = (
            server_url
            or os.environ.get("LINKEDIN_MCP_SERVER_URL", "")
            or "https://developer.unipile.com/mcp"
        ).strip()
        self.api_base_url = os.environ.get("LINKEDIN_MCP_API_BASE_URL", "").strip().rstrip("/")
        self.api_key = (api_key or os.environ.get("LINKEDIN_MCP_API_KEY", "")).strip()
        if not self.server_url:
            raise LinkedInMCPNotConfigured("LINKEDIN_MCP_SERVER_URL is required.")

        self._request_id = 0
        self._tools: Optional[list[dict[str, Any]]] = None

    # ── Public workflow ──────────────────────────────────────────────────

    def queue_linkedin_outreach(self, contact: dict, message: str) -> LinkedInMCPResult:
        """
        Pushes a contact to the MCP server and, when a LinkedIn invite tool is
        exposed, asks the MCP server to send/queue the connection note.
        """
        contact_result = self.upsert_contact(contact, required=False)
        linkedin_tool = self._find_tool(
            env_name="LINKEDIN_MCP_CONNECT_TOOL",
            include_any=("linkedin", "invite", "invitation", "connect", "connection"),
            include_one_of=("connect", "connection", "invite", "invitation", "request"),
        )
        if linkedin_tool:
            args = self._args_for_schema(
                linkedin_tool,
                contact=contact,
                message=message,
                extra=self._campaign_context(),
            )
            response = self._call_tool(linkedin_tool["name"], args)
            return LinkedInMCPResult(True, "linkedin_connection", linkedin_tool["name"], response)

        list_tool = self._find_tool(
            env_name="LINKEDIN_MCP_LIST_TOOL",
            include_any=("contact", "lead", "list"),
            include_one_of=("add", "push", "assign", "campaign", "list"),
        )
        if list_tool:
            args = self._args_for_schema(
                list_tool,
                contact=contact,
                message=message,
                extra=self._campaign_context(),
            )
            response = self._call_tool(list_tool["name"], args)
            return LinkedInMCPResult(True, "mcp_list", list_tool["name"], response)

        execute_tool = next((t for t in self._list_tools() if t.get("name") == "execute-request"), None)
        if execute_tool:
            return self._send_unipile_invite(contact, message, execute_tool["name"])

        if not contact_result:
            raise LinkedInMCPError(
                "No LinkedIn invitation, list/campaign, or contact/lead tool found in MCP tools."
            )

        return LinkedInMCPResult(
            True,
            "contact_created",
            contact_result.tool,
            {
                "contact": contact_result.response,
                "warning": (
                    "Contact was pushed to the LinkedIn MCP server, but no invitation "
                    "or list/campaign tool was found in the MCP tool list."
                ),
            },
        )

    def upsert_contact(self, contact: dict, required: bool = True) -> Optional[LinkedInMCPResult]:
        tool = self._find_tool(
            env_name="LINKEDIN_MCP_CONTACT_TOOL",
            include_any=("contact", "lead"),
            include_one_of=("create", "upsert", "add", "push", "import"),
        )
        if not tool:
            if required:
                raise LinkedInMCPError("No contact/lead creation tool found in LinkedIn MCP tools.")
            return None
        args = self._args_for_schema(tool, contact=contact, message=None, extra=self._campaign_context())
        response = self._call_tool(tool["name"], args)
        return LinkedInMCPResult(True, "contact_upsert", tool["name"], response)

    # ── MCP JSON-RPC ─────────────────────────────────────────────────────

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-KEY"] = self.api_key
            headers["X-API-Key"] = self.api_key

        resp = requests.post(
            self.server_url,
            headers=headers,
            json=payload,
            timeout=45,
        )
        if resp.status_code >= 400:
            raise LinkedInMCPError(f"LinkedIn MCP HTTP {resp.status_code}: {resp.text[:300]}")

        data = self._decode_mcp_response(resp)
        if "error" in data:
            raise LinkedInMCPError(json.dumps(data["error"], ensure_ascii=False))
        return data.get("result", {})

    @staticmethod
    def _decode_mcp_response(resp: requests.Response) -> dict[str, Any]:
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return resp.json()
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                value = line.removeprefix("data:").strip()
                if value and value != "[DONE]":
                    return json.loads(value)
        raise LinkedInMCPError("LinkedIn MCP returned an empty event stream.")

    def _initialize(self) -> None:
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "canusa-outbound", "version": "1.0.0"},
                },
            )
        except LinkedInMCPError as e:
            # Some hosted MCP gateways skip explicit initialize for API-key calls.
            log.info("LinkedIn MCP initialize skipped/failed: %s", e)

    def _list_tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._initialize()
            result = self._rpc("tools/list")
            self._tools = result.get("tools", [])
        return self._tools

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    # ── Unipile execute-request fallback ─────────────────────────────────

    def _send_unipile_invite(self, contact: dict, message: str, tool_name: str) -> LinkedInMCPResult:
        if not self.api_base_url:
            raise LinkedInMCPError("LINKEDIN_MCP_API_BASE_URL is required for Unipile execute-request.")
        if not self.api_key:
            raise LinkedInMCPError("LINKEDIN_MCP_API_KEY is required for Unipile execute-request.")

        payload = self._contact_payload(contact)
        account_id = os.environ.get("LINKEDIN_MCP_ACCOUNT_ID", "").strip()
        if not account_id:
            raise LinkedInMCPError("LINKEDIN_MCP_ACCOUNT_ID is required to send Unipile invitations.")

        provider_id = payload.get("providerId") or ""
        if not provider_id:
            identifier = payload.get("publicIdentifier") or payload.get("linkedinUrl") or ""
            if not identifier:
                raise LinkedInMCPError("Contact is missing a LinkedIn URL/public identifier.")
            profile = self._unipile_execute(
                "GET",
                f"{self.api_base_url}/api/v1/users/{identifier}",
                query={"account_id": account_id},
                tool_name=tool_name,
            )
            provider_id = self._extract_unipile_json(profile).get("provider_id", "")
            if not provider_id:
                raise LinkedInMCPError("Unipile profile lookup did not return provider_id.")

        invite = self._unipile_execute(
            "POST",
            f"{self.api_base_url}/api/v1/users/invite",
            body={
                "account_id": account_id,
                "provider_id": provider_id,
                "message": message[:300],
            },
            tool_name=tool_name,
        )
        invite_json = self._extract_unipile_json(invite)
        if invite_json.get("invitation_id") or invite_json.get("object") == "UserInvitationSent":
            return LinkedInMCPResult(True, "linkedin_invitation_sent", tool_name, invite)

        error_type = invite_json.get("type", "")
        if error_type == "errors/already_connected":
            return LinkedInMCPResult(True, "linkedin_already_connected", tool_name, invite)
        if error_type in ("errors/already_invited_recently", "errors/action_already_performed"):
            return LinkedInMCPResult(True, "linkedin_invitation_pending", tool_name, invite)

        detail = invite_json.get("detail") or invite_json.get("title") or json.dumps(invite_json)[:300]
        raise LinkedInMCPError(f"Unipile invitation was not sent: {detail}")

    def _unipile_execute(
        self,
        method: str,
        url: str,
        tool_name: str,
        query: Optional[dict[str, str]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        har: dict[str, Any] = {
            "method": method.lower(),
            "url": url,
            "headers": [
                {"name": "X-API-KEY", "value": self.api_key},
                {"name": "accept", "value": "application/json"},
            ],
        }
        if query:
            har["queryString"] = [{"name": k, "value": v} for k, v in query.items() if v]
        if body is not None:
            har["headers"].append({"name": "content-type", "value": "application/json"})
            har["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}
        return self._call_tool(tool_name, {"harRequest": har})

    @staticmethod
    def _extract_unipile_json(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        continue
        for key in ("data", "result", "response"):
            value = result.get(key)
            if isinstance(value, dict):
                return value
        return result

    # ── Tool/schema helpers ───────────────────────────────────────────────

    def _find_tool(
        self,
        env_name: str,
        include_any: tuple[str, ...],
        include_one_of: tuple[str, ...],
    ) -> Optional[dict[str, Any]]:
        override = os.environ.get(env_name, "").strip()
        tools = self._list_tools()
        if override:
            found = next((t for t in tools if t.get("name") == override), None)
            if found:
                return found
            raise LinkedInMCPError(f"{env_name}={override!r}, but that tool was not listed by LinkedIn MCP.")

        def score(tool: dict[str, Any]) -> int:
            text = f"{tool.get('name', '')} {tool.get('description', '')}".lower()
            if not any(term in text for term in include_any):
                return 0
            return sum(2 for term in include_any if term in text) + sum(
                1 for term in include_one_of if term in text
            )

        ranked = sorted(((score(t), t) for t in tools), key=lambda x: x[0], reverse=True)
        return ranked[0][1] if ranked and ranked[0][0] > 0 else None

    def _args_for_schema(
        self,
        tool: dict[str, Any],
        contact: dict,
        message: Optional[str],
        extra: dict[str, str],
    ) -> dict[str, Any]:
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        args: dict[str, Any] = {}

        contact_payload = self._contact_payload(contact)
        aliases = {
            "firstName": contact_payload["firstName"],
            "first_name": contact_payload["firstName"],
            "firstname": contact_payload["firstName"],
            "lastName": contact_payload["lastName"],
            "last_name": contact_payload["lastName"],
            "lastname": contact_payload["lastName"],
            "fullName": contact_payload["fullName"],
            "full_name": contact_payload["fullName"],
            "name": contact_payload["fullName"],
            "email": contact_payload["email"],
            "workEmail": contact_payload["email"],
            "work_email": contact_payload["email"],
            "linkedinUrl": contact_payload["linkedinUrl"],
            "linkedin_url": contact_payload["linkedinUrl"],
            "linkedin": contact_payload["linkedinUrl"],
            "profileUrl": contact_payload["linkedinUrl"],
            "profile_url": contact_payload["linkedinUrl"],
            "identifier": contact_payload["publicIdentifier"],
            "publicIdentifier": contact_payload["publicIdentifier"],
            "public_identifier": contact_payload["publicIdentifier"],
            "providerId": contact_payload.get("providerId", ""),
            "provider_id": contact_payload.get("providerId", ""),
            "memberUrn": contact_payload.get("memberUrn", ""),
            "member_urn": contact_payload.get("memberUrn", ""),
            "accountId": extra.get("accountId", ""),
            "account_id": extra.get("accountId", ""),
            "company": contact_payload["companyName"],
            "companyName": contact_payload["companyName"],
            "company_name": contact_payload["companyName"],
            "firmName": contact_payload["companyName"],
            "title": contact_payload["jobTitle"],
            "jobTitle": contact_payload["jobTitle"],
            "job_title": contact_payload["jobTitle"],
            "message": message or "",
            "note": message or "",
            "connectionNote": message or "",
            "connection_note": message or "",
            "listName": extra.get("listName", ""),
            "list_name": extra.get("listName", ""),
            "campaignName": extra.get("campaignName", ""),
            "campaign_name": extra.get("campaignName", ""),
        }

        for prop in properties:
            value = aliases.get(prop)
            if value:
                args[prop] = value

        for prop in required:
            if prop in args:
                continue
            lower = prop.lower()
            if "contact" in lower or "lead" in lower or lower in {"data", "payload"}:
                args[prop] = contact_payload
            elif "message" in lower or "note" in lower:
                args[prop] = message or ""
            elif lower in {"identifier", "publicidentifier", "public_identifier", "profile"}:
                args[prop] = contact_payload["publicIdentifier"] or contact_payload["linkedinUrl"]
            elif lower in {"providerid", "provider_id"}:
                args[prop] = contact_payload.get("providerId") or contact_payload["publicIdentifier"]
            elif lower in {"accountid", "account_id"}:
                args[prop] = extra.get("accountId", "")
            elif "list" in lower:
                args[prop] = extra.get("listName", "")
            elif "campaign" in lower:
                args[prop] = extra.get("campaignName", "")

        if not properties:
            args = {**contact_payload, "message": message or "", **extra}
        return {k: v for k, v in args.items() if v not in (None, "")}

    @staticmethod
    def _linkedin_public_identifier(linkedin_url: str) -> str:
        value = (linkedin_url or "").strip()
        if not value:
            return ""
        if "/" not in value and "linkedin.com" not in value:
            return value
        parsed = urlparse(value if "://" in value else f"https://{value}")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() == "in":
            return parts[1]
        return parts[-1] if parts else ""

    @staticmethod
    def _contact_payload(contact: dict) -> dict[str, Any]:
        first = (contact.get("firstName") or "").strip()
        last = (contact.get("lastName") or "").strip()
        full = (contact.get("fullName") or f"{first} {last}").strip()
        linkedin_url = (contact.get("linkedinUrl") or "").strip()
        return {
            "externalId": str(contact.get("id", "")),
            "firstName": first,
            "lastName": last,
            "fullName": full,
            "email": (contact.get("workEmail") or "").strip(),
            "linkedinUrl": linkedin_url,
            "publicIdentifier": LinkedInMCPClient._linkedin_public_identifier(linkedin_url),
            "providerId": (contact.get("linkedinProviderId") or contact.get("provider_id") or "").strip(),
            "memberUrn": (contact.get("linkedinMemberUrn") or contact.get("member_urn") or "").strip(),
            "companyName": (contact.get("firmName") or "").strip(),
            "jobTitle": (contact.get("jobTitle") or "").strip(),
            "source": "CAN USA outbound",
            "notes": (
                f"FISP: {contact.get('totalUnfiled', 0)} unfiled, "
                f"{contact.get('sub10A', 0)} in 10A, "
                f"{contact.get('wPriorSWARM', 0)} prior SWARMP."
            ),
        }

    @staticmethod
    def _campaign_context() -> dict[str, str]:
        return {
            "accountId": os.environ.get("LINKEDIN_MCP_ACCOUNT_ID", ""),
            "listName": os.environ.get("LINKEDIN_MCP_LIST_NAME", "CAN USA LinkedIn Outreach"),
            "campaignName": os.environ.get("LINKEDIN_MCP_CAMPAIGN_NAME", "CAN USA LinkedIn Outreach"),
        }
