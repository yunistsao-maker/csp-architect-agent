"""Minimal MCP client for the tools gateway: JSON-RPC over streamable HTTP, signed with SigV4 (IAM only).

The runtime calls the tools gateway with its own execution role (the gateway's inbound authorizer is AWS_IAM),
and forwards the end user's alias in the `x-user-alias` header so web search and the post-processing hook
can log who asked. No bearer tokens are used on this hop.
"""

from __future__ import annotations

import itertools
import json
import os
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ALIAS_HEADER = "x-user-alias"
# Which portal agent is calling (set by agent code, not the model). The tools gateway interceptor uses it to allow
# each agent only its own tools, e.g. only the Terraform agent may open PRs in the infrastructure repo.
AGENT_HEADER = "x-agent-kind"
PROTOCOL_VERSION = "2025-03-26"
SIGNING_SERVICE = "bedrock-agentcore"


class ToolsGatewayError(RuntimeError):
    pass


def parse_mcp_response(content_type: str, raw: bytes) -> dict:
    """Return the JSON-RPC message from a JSON or SSE (text/event-stream) response body."""
    text = raw.decode("utf-8")
    if "text/event-stream" in (content_type or ""):
        message = None
        for event in text.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if data:
                candidate = json.loads(data)
                if "result" in candidate or "error" in candidate:
                    message = candidate
        if message is None:
            raise ToolsGatewayError("no JSON-RPC response in event stream")
        return message
    return json.loads(text)


class ToolsGatewayClient:
    def __init__(self, url: str, region: str | None = None, alias: str | None = None,
                 session: boto3.Session | None = None, timeout: int = 300, opener=None, agent_kind: str | None = None):
        self.url = url.rstrip("/")
        if not self.url.endswith("/mcp"):
            self.url += "/mcp"
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.alias = alias
        self.agent_kind = agent_kind
        self.session = session or boto3.Session()
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._ids = itertools.count(1)
        self._mcp_session: str | None = None
        self._tools: list[dict] | None = None

    # -- transport -------------------------------------------------------------------------------------
    def _post(self, message: dict) -> tuple[dict | None, dict]:
        body = json.dumps(message).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Mcp-Protocol-Version": PROTOCOL_VERSION}
        if self.alias:
            headers[ALIAS_HEADER] = self.alias
        if self.agent_kind:
            headers[AGENT_HEADER] = self.agent_kind
        if self._mcp_session:
            headers["Mcp-Session-Id"] = self._mcp_session
        creds = self.session.get_credentials().get_frozen_credentials()
        aws_req = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
        SigV4Auth(creds, SIGNING_SERVICE, self.region).add_auth(aws_req)
        req = urllib.request.Request(self.url, data=body, headers=dict(aws_req.headers), method="POST")
        with self._open(req, timeout=self.timeout) as resp:
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if "id" not in message or not raw.strip():
            return None, resp_headers  # notification
        return parse_mcp_response(resp_headers.get("content-type", ""), raw), resp_headers

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        if self._mcp_session is None and method != "initialize":
            self.initialize()
        msg, _ = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}})
        if "error" in msg:
            raise ToolsGatewayError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def initialize(self) -> dict:
        msg, headers = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "csp-architect-agent", "version": "1.0"}}})
        self._mcp_session = headers.get("mcp-session-id", "")
        if self._mcp_session:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if msg and "error" in msg:
            raise ToolsGatewayError(f"initialize: {msg['error']}")
        return (msg or {}).get("result", {})

    # -- MCP operations --------------------------------------------------------------------------------
    def list_tools(self) -> list[dict]:
        if self._tools is None:
            tools, cursor = [], None
            while True:
                result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
                tools += result.get("tools", [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self._tools = tools
        return self._tools

    def find_tool(self, target: str, tool: str | None = None) -> dict:
        """Gateway tool names are `<target>___<tool>`; return the first match for the target (and tool)."""
        for t in self.list_tools():
            name = t.get("name", "")
            if name.startswith(f"{target}___") and (tool is None or name == f"{target}___{tool}"):
                return t
        raise ToolsGatewayError(f"no tool for target '{target}' on the tools gateway")

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ToolsGatewayError(f"{name}: {tool_text(result)[:500]}")
        return result


def tool_text(result: dict) -> str:
    return "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def tool_json(result: dict) -> dict:
    """Structured result of a Lambda target (JSON text) or the structuredContent block."""
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    text = tool_text(result)
    data = json.loads(text) if text else {}
    # Lambda targets may wrap the handler's return value as {"statusCode":..., "body": ...}
    if isinstance(data, dict) and "body" in data and "statusCode" in data:
        body = data["body"]
        data = json.loads(body) if isinstance(body, str) else body
    return data
