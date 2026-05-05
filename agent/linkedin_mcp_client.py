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
      LINKEDIN_MCP_API_KEY    Optional bearer/API key for your MCP gateway
      LINKEDIN_MCP_CONTACT_TOOL Optional exact MCP tool name for contact upsert
      LINKEDIN_MCP_CONNECT_TOOL   Optional exact MCP tool name for LinkedIn invite
      LINKEDIN_MCP_LIST_TOOL  Optional exact MCP tool name for list/campaign add
      LINKEDIN_MCP_LIST_NAME  Optional list name for queued contacts
      LINKEDIN_MCP_CAMPAIGN_NAME Optional campaign name for campaign-aware tools
    """

    def __init__(self, server_url: str | None = None, api_key: str | None = None) -> None:
        self.server_url = (server_url or os.environ.get("LINKEDIN_MCP_SERVER_URL", "")).strip()
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
            include_any=("linkedin",),
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
            elif "list" in lower:
                args[prop] = extra.get("listName", "")
            elif "campaign" in lower:
                args[prop] = extra.get("campaignName", "")

        if not properties:
            args = {**contact_payload, "message": message or "", **extra}
        return {k: v for k, v in args.items() if v not in (None, "")}

    @staticmethod
    def _contact_payload(contact: dict) -> dict[str, Any]:
        first = (contact.get("firstName") or "").strip()
        last = (contact.get("lastName") or "").strip()
        full = (contact.get("fullName") or f"{first} {last}").strip()
        return {
            "externalId": str(contact.get("id", "")),
            "firstName": first,
            "lastName": last,
            "fullName": full,
            "email": (contact.get("workEmail") or "").strip(),
            "linkedinUrl": (contact.get("linkedinUrl") or "").strip(),
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
            "listName": os.environ.get("LINKEDIN_MCP_LIST_NAME", "CAN USA LinkedIn Outreach"),
            "campaignName": os.environ.get("LINKEDIN_MCP_CAMPAIGN_NAME", "CAN USA LinkedIn Outreach"),
        }
