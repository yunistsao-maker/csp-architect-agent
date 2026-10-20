"""Offline tests for the gateway Lambdas: alias interceptor (HTTP + MCP payloads) and the post-processing hook."""
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


interceptor = load("alias_interceptor", ROOT / "gateway/lambdas/alias_interceptor/handler.py")
hook = load("postprocess_hook", ROOT / "gateway/lambdas/postprocess/handler.py")


def jwt(claims: dict, sig: str = "c2ln") -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256', 'kid': 'k'})}.{enc(claims)}.{sig}"


def ctx(custom=None):
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom or {}))


def http_event(headers: dict, body: dict | None = None) -> dict:
    raw = base64.b64encode(json.dumps(body or {"prompt": "x"}).encode()).decode()
    return {"interceptorInputVersion": "1.0", "http": {"gatewayRequest": {
        "path": "/csp-architect/invocations", "httpMethod": "POST", "headers": headers, "body": raw}}}


COGNITO = {"sub": "1234", "client_id": "abc", "username": "yhtsao", "token_use": "access", "iss": "https://cognito"}


# ---------------------------------------------------------------- ingress (HTTP payload)
def test_ingress_adds_alias_header_and_drops_authorization():
    ev = http_event({"Authorization": f"Bearer {jwt(COGNITO)}", "Content-Type": "application/json"})
    out = interceptor.lambda_handler(ev, ctx({"REQUEST_ID": "r1"}))
    req = out["http"]["transformedGatewayRequest"]
    assert out["interceptorOutputVersion"] == "1.0"
    assert req["headers"][interceptor.RUNTIME_ALIAS_HEADER] == "yhtsao"
    assert not any(k.lower() == "authorization" for k in req["headers"])  # gateway signs with its IAM role
    assert req["body"] == ev["http"]["gatewayRequest"]["body"]


def test_ingress_client_cannot_spoof_alias_header():
    ev = http_event({"authorization": f"Bearer {jwt(COGNITO)}",
                     "x-amzn-bedrock-agentcore-runtime-custom-user-alias": "admin"})
    headers = interceptor.lambda_handler(ev, ctx())["http"]["transformedGatewayRequest"]["headers"]
    values = [v for k, v in headers.items() if k.lower() == interceptor.RUNTIME_ALIAS_HEADER.lower()]
    assert values == ["yhtsao"]


@pytest.mark.parametrize("claims", [{"sub": "1"}, {"username": "bad alias!"}, {"username": ""}])
def test_ingress_rejects_tokens_without_usable_alias(claims):
    out = interceptor.lambda_handler(http_event({"Authorization": f"Bearer {jwt(claims)}"}), ctx())
    resp = out["http"]["transformedGatewayResponse"]
    assert resp["statusCode"] == 403 and "transformedGatewayRequest" not in out["http"]
    assert json.loads(base64.b64decode(resp["body"]))["error_code"] == "forbidden"


def test_ingress_rejects_missing_or_malformed_token():
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not.a"}):
        assert interceptor.lambda_handler(http_event(headers), ctx())["http"]["transformedGatewayResponse"]["statusCode"] == 403


def test_alias_claim_order_is_configurable(monkeypatch):
    monkeypatch.setenv("ALIAS_CLAIMS", "alias,username")
    token = jwt({**COGNITO, "alias": "eunice"})
    out = interceptor.lambda_handler(http_event({"Authorization": f"Bearer {token}"}), ctx())
    assert out["http"]["transformedGatewayRequest"]["headers"][interceptor.RUNTIME_ALIAS_HEADER] == "eunice"


# ---------------------------------------------------------------- tools gateway (MCP payload)
def mcp_event(headers: dict, method="tools/call", args=None):
    return {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {
        "path": "/mcp", "httpMethod": "POST", "headers": headers,
        "body": {"jsonrpc": "2.0", "id": 7, "method": method,
                 "params": {"name": "web-search-tool___WebSearch", "arguments": args or {"query": "ap-east-2 FSx"}}}}}}


def test_tools_call_forwards_alias_and_logs(capsys):
    out = interceptor.lambda_handler(mcp_event({"x-user-alias": "yhtsao", "Authorization": "AWS4-HMAC-SHA256 ..."}), ctx())
    req = out["mcp"]["transformedGatewayRequest"]
    assert req["headers"] == {"x-user-alias": "yhtsao"}
    assert req["body"]["params"]["arguments"]["query"] == "ap-east-2 FSx"
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["alias"] == "yhtsao" and log["tool"] == "web-search-tool___WebSearch" and log["query"] == "ap-east-2 FSx"


def test_tools_call_without_alias_is_denied():
    out = interceptor.lambda_handler(mcp_event({}), ctx())
    body = out["mcp"]["transformedGatewayResponse"]["body"]
    assert body["id"] == 7 and body["error"]["code"] == -32001


def test_tools_call_without_alias_allowed_when_not_required(monkeypatch):
    monkeypatch.setenv("REQUIRE_ALIAS", "false")
    assert "transformedGatewayRequest" in interceptor.lambda_handler(mcp_event({}), ctx())["mcp"]


def test_tools_list_passes_without_alias():
    out = interceptor.lambda_handler(mcp_event({}, method="tools/list"), ctx())
    assert out["mcp"]["transformedGatewayRequest"]["body"]["method"] == "tools/list"


def test_unknown_payload_passes_through():
    assert interceptor.lambda_handler({"interceptorInputVersion": "1.0"}, ctx()) == {"interceptorOutputVersion": "1.0"}


# ---------------------------------------------------------------- post-processing hook
XML = ('<mxfile><diagram id="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
       '<mxCell id="a" style="shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.elastic_container_registry;" '
       'vertex="1" parent="1"><mxGeometry width="50" height="50" as="geometry"/></mxCell></root></mxGraphModel>'
       '</diagram></mxfile>')


def test_hook_reads_propagated_alias_and_fixes_icons(capsys, monkeypatch):
    monkeypatch.delenv("ARTIFACT_BUCKET", raising=False)
    out = hook.lambda_handler({"xml": XML, "mode": "aws", "session_id": "s1"},
                              ctx({"bedrockAgentCorePropagatedHeaders": {"X-User-Alias": "yhtsao"}}))
    assert out["alias"] == "yhtsao" and out["validation"]["ok"] and "resIcon=mxgraph.aws4.ecr;" in out["xml"]
    assert out["artifact"] is None
    log = json.loads(capsys.readouterr().out.strip())
    assert log == {**log, "component": "postprocess-hook", "alias": "yhtsao", "icons_fixed": 1, "valid": True}


def test_hook_accepts_json_encoded_headers_and_stores(monkeypatch):
    monkeypatch.setenv("ARTIFACT_BUCKET", "b")
    stored = {}
    monkeypatch.setattr(hook, "_store", lambda bucket, alias, mode, xml, expl: stored.update(
        bucket=bucket, alias=alias) or "s3://b/diagrams/x.drawio")
    out = hook.lambda_handler({"xml": XML, "mode": "aws"},
                              ctx({"bedrockAgentCorePropagatedHeaders": json.dumps({"x-user-alias": "yhtsao"})}))
    assert stored == {"bucket": "b", "alias": "yhtsao"} and out["artifact"].startswith("s3://b/")


def test_hook_without_alias_or_xml():
    out = hook.lambda_handler({}, ctx())
    assert out["alias"] == "unknown" and out["validation"]["ok"] is False
