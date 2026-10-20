"""Agent behaviour behind the ingress gateway: caller alias header, web search via the tools gateway, hook."""
import importlib
import json
from types import SimpleNamespace

import pytest

from test_skills import ScriptedModel

GOOD = ('<mxfile><diagram id="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" value="EKS" style="shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.eks;" vertex="1" '
        'parent="1"><mxGeometry width="50" height="50" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>')
HOOKED = GOOD.replace('value="EKS"', 'value="EKS (hooked)"')
ALIAS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias"


class FakeTools:
    """Stands in for ToolsGatewayClient; records alias and calls."""
    instances = []

    def __init__(self, alias, hook_result=None, fail=None):
        self.alias, self.calls, self.hook_result, self.fail = alias, [], hook_result, fail
        FakeTools.instances.append(self)

    def find_tool(self, target, tool=None):
        if self.fail:
            from tools_gateway import ToolsGatewayError
            raise ToolsGatewayError(self.fail)
        return {"name": f"{target}___{tool or 'WebSearch'}", "inputSchema": {"properties": {"query": {}}}}

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name.endswith("process_architecture"):
            return {"content": [{"type": "text", "text": json.dumps(self.hook_result)}]}
        return {"content": [{"type": "text", "text": "FSx for ONTAP is available in ap-east-2"}]}


@pytest.fixture()
def gw_agent(monkeypatch):
    monkeypatch.setenv("TOOLS_GATEWAY_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("REQUIRE_CALLER_ALIAS", "true")
    mod = importlib.reload(importlib.import_module("agent"))
    mod._agents.clear()
    FakeTools.instances.clear()
    yield mod
    monkeypatch.delenv("TOOLS_GATEWAY_URL")
    monkeypatch.delenv("REQUIRE_CALLER_ALIAS")
    importlib.reload(mod)


def ctx(alias=None, session="s-123"):
    return SimpleNamespace(session_id=session, request_headers={ALIAS_HEADER.lower(): alias} if alias else {})


def wire(mod, monkeypatch, turns, hook_result=None, fail=None):
    model = ScriptedModel(turns)
    monkeypatch.setattr(mod, "BedrockModel", lambda **kw: model)
    monkeypatch.setattr(mod, "make_tools_client", lambda alias: FakeTools(alias, hook_result, fail))
    return model


def test_alias_header_reaches_response_and_hook(gw_agent, monkeypatch):
    wire(gw_agent, monkeypatch, [{"text": f"```xml\n{GOOD}\n```\n## Overview\nok"}],
         hook_result={"alias": "yhtsao", "xml": HOOKED, "validation": {"ok": True}, "artifact": "s3://b/k.drawio"})
    out = gw_agent.invoke({"prompt": "x", "mode": "aws"}, ctx("yhtsao"))
    assert out["caller"] == {"alias": "yhtsao"}
    assert out["postprocess"] == {"ok": True, "artifact": "s3://b/k.drawio", "alias": "yhtsao", "validation_ok": True}
    assert out["xml"] == HOOKED
    (hook_client,) = FakeTools.instances
    name, args = hook_client.calls[0]
    assert hook_client.alias == "yhtsao" and name == "postprocess-hook___process_architecture"
    assert args["xml"] == GOOD and args["mode"] == "aws" and args["session_id"] == "s-123"


def test_missing_alias_is_rejected_when_required(gw_agent, monkeypatch):
    model = wire(gw_agent, monkeypatch, [{"text": "never called"}])
    out = gw_agent.invoke({"prompt": "x"}, ctx(None))
    assert out["error_code"] == "forbidden" and model.system_prompts == []


def test_hook_failure_does_not_lose_the_diagram(gw_agent, monkeypatch):
    wire(gw_agent, monkeypatch, [{"text": f"```xml\n{GOOD}\n```"}], fail="gateway unreachable")
    out = gw_agent.invoke({"prompt": "x"}, ctx("yhtsao"))
    assert out["xml"] == GOOD and out["postprocess"]["ok"] is False and "gateway unreachable" in out["postprocess"]["error"]


def test_hook_failure_is_fatal_when_required(gw_agent, monkeypatch):
    monkeypatch.setattr(gw_agent, "HOOK_REQUIRED", True)
    wire(gw_agent, monkeypatch, [{"text": f"```xml\n{GOOD}\n```"}], fail="down")
    with pytest.raises(Exception, match="down"):
        gw_agent.invoke({"prompt": "x"}, ctx("yhtsao"))


def test_web_search_goes_through_tools_gateway_with_alias(gw_agent, monkeypatch):
    model = wire(gw_agent, monkeypatch, [
        {"tool": ("web_search", {"query": "FSx ONTAP ap-east-2"})},
        {"text": f"```xml\n{GOOD}\n```"}],
        hook_result={"alias": "yhtsao", "xml": GOOD, "validation": {"ok": True}, "artifact": None})
    out = gw_agent.invoke({"prompt": "x", "enable_search": True}, ctx("yhtsao"))
    assert "web_search" in model.tool_specs[0] and "tavilySearch" not in model.tool_specs[0]
    assert "use the web_search tool" in model.system_prompts[0]
    search_client = FakeTools.instances[0]
    assert search_client.alias == "yhtsao"
    assert search_client.calls == [("web-search-tool___WebSearch", {"query": "FSx ONTAP ap-east-2"})]
    assert out["usage"]["tool_calls"] == 1 and out["postprocess"]["ok"]


def test_no_tools_gateway_keeps_local_behaviour(monkeypatch):
    monkeypatch.delenv("TOOLS_GATEWAY_URL", raising=False)
    mod = importlib.reload(importlib.import_module("agent"))
    model = ScriptedModel([{"text": f"```xml\n{GOOD}\n```"}])
    monkeypatch.setattr(mod, "BedrockModel", lambda **kw: model)
    out = mod.invoke({"prompt": "x"}, SimpleNamespace(session_id=None, request_headers={}))
    assert "postprocess" not in out and out["caller"] == {"alias": None} and out["validation"]["ok"]


def test_cached_session_agent_does_not_keep_previous_alias(gw_agent, monkeypatch):
    monkeypatch.setattr(gw_agent, "REQUIRE_CALLER_ALIAS", False)
    hook = {"alias": "x", "xml": GOOD, "validation": {"ok": True}, "artifact": None}
    wire(gw_agent, monkeypatch, [{"text": f"```xml\n{GOOD}\n```"}] * 2, hook_result=hook)
    assert gw_agent.invoke({"prompt": "x"}, ctx("yhtsao", session="same"))["caller"] == {"alias": "yhtsao"}
    gw_agent.invoke({"prompt": "y"}, ctx(None, session="same"))
    (agent,) = gw_agent._agents.values()
    assert agent.state.get("caller_alias") is None and FakeTools.instances[-1].alias is None
