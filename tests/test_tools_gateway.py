"""ToolsGatewayClient: SigV4-signed MCP JSON-RPC with the caller alias header (fake HTTP opener, no AWS)."""
import json

import boto3
import pytest

from tools_gateway import ToolsGatewayClient, ToolsGatewayError, parse_mcp_response, tool_json, tool_text


class FakeResp:
    def __init__(self, body: str, content_type="application/json", headers=None):
        self._body = body.encode()
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGateway:
    """Records requests and answers like an AgentCore MCP gateway."""

    def __init__(self, tools, call_result=None, sse=False):
        self.requests, self.tools, self.call_result, self.sse = [], tools, call_result, sse

    def __call__(self, req, timeout=None):
        msg = json.loads(req.data)
        self.requests.append((dict(req.header_items()), msg))
        if "id" not in msg:
            return FakeResp("", headers={})
        if msg["method"] == "initialize":
            return FakeResp(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-03-26"}}),
                            headers={"Mcp-Session-Id": "sess-1"})
        if msg["method"] == "tools/list":
            cursor = msg["params"].get("cursor")
            page = self.tools[1:] if cursor else self.tools[:1]
            result = {"tools": page, **({} if cursor else {"nextCursor": "p2"})}
        else:
            result = self.call_result
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        if self.sse:
            return FakeResp(f"event: message\ndata: {body}\n\n", content_type="text/event-stream")
        return FakeResp(body)


SESSION = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret", region_name="us-east-1")
TOOLS = [{"name": "web-search-tool___WebSearch", "inputSchema": {"properties": {"query": {}}}},
         {"name": "postprocess-hook___process_architecture", "inputSchema": {"properties": {"xml": {}}}}]


def client(gw, alias="yhtsao"):
    return ToolsGatewayClient("https://gw.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
                              "us-east-1", alias, session=SESSION, opener=gw)


def test_requests_are_sigv4_signed_and_carry_alias():
    gw = FakeGateway(TOOLS, {"content": [{"type": "text", "text": "ok"}]})
    c = client(gw)
    assert tool_text(c.call_tool("web-search-tool___WebSearch", {"query": "q"})) == "ok"
    methods = [m.get("method") for _, m in gw.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    headers, msg = gw.requests[-1]
    lower = {k.lower(): v for k, v in headers.items()}
    assert lower["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock-agentcore/aws4_request" in lower["authorization"]
    assert lower["x-user-alias"] == "yhtsao" and lower["mcp-session-id"] == "sess-1"
    assert msg["params"] == {"name": "web-search-tool___WebSearch", "arguments": {"query": "q"}}


def test_url_gets_mcp_suffix():
    assert client(FakeGateway(TOOLS)).url.endswith(".amazonaws.com/mcp")
    assert ToolsGatewayClient("https://x.example.com", session=SESSION).url == "https://x.example.com/mcp"


def test_list_tools_paginates_and_find_tool():
    c = client(FakeGateway(TOOLS))
    assert [t["name"] for t in c.list_tools()] == [t["name"] for t in TOOLS]
    assert c.find_tool("postprocess-hook", "process_architecture")["name"].endswith("process_architecture")
    with pytest.raises(ToolsGatewayError):
        c.find_tool("nope")


def test_sse_responses_are_parsed():
    gw = FakeGateway(TOOLS, {"content": [{"type": "text", "text": '{"alias": "yhtsao"}'}]}, sse=True)
    assert tool_json(client(gw).call_tool("x", {})) == {"alias": "yhtsao"}


def test_tool_errors_raise():
    gw = FakeGateway(TOOLS, {"isError": True, "content": [{"type": "text", "text": "boom"}]})
    with pytest.raises(ToolsGatewayError, match="boom"):
        client(gw).call_tool("x", {})


def test_parse_errors_and_wrappers():
    with pytest.raises(ToolsGatewayError):
        parse_mcp_response("text/event-stream", b"event: ping\n\n")
    wrapped = {"content": [{"type": "text", "text": json.dumps({"statusCode": 200, "body": {"a": 1}})}]}
    assert tool_json(wrapped) == {"a": 1}
    assert tool_json({"structuredContent": {"b": 2}}) == {"b": 2}
