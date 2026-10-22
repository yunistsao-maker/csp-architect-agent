"""AgentCore Gateway REQUEST interceptor: turn the caller's JWT into a user-alias header.

Attached (with passRequestHeaders=true) to both gateways:

* Ingress gateway (HTTP payload, AgentCore Runtime target, CUSTOM_JWT inbound)
    - The gateway has already validated the JWT signature, issuer, expiry and client before invoking us.
    - We read the alias claim, add `X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias` (the runtime only
      accepts custom headers with this prefix), and DROP the inbound Authorization header so the gateway signs
      the outbound call with its own IAM role (an interceptor-returned Authorization header would override it).
    - No usable alias -> short-circuit with 403; the runtime is never called.

* Tools gateway (MCP payload, AWS_IAM inbound from the runtime)
    - Reads the `x-user-alias` header the agent sends, writes an audit log line per tools/call
      (tool name + web-search query), forwards only the allowlisted alias header to the target.

Configuration (environment variables):
    ALIAS_CLAIMS   comma-separated claim names tried in order (default: alias,username,cognito:username,preferred_username)
    REQUIRE_ALIAS  "true" (default) rejects tools/call on the tools gateway when the alias header is missing
"""

import base64
import json
import os
import re

RUNTIME_ALIAS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias"
TOOLS_ALIAS_HEADER = "x-user-alias"
AGENT_HEADER = "x-agent-kind"
AGENT_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def _claims_from_bearer(authorization: str | None) -> dict:
    """Decode the JWT payload. The gateway's CUSTOM_JWT authorizer verified the signature before this runs."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return {}
    parts = authorization.split(" ", 1)[1].strip().split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def alias_from_claims(claims: dict) -> str | None:
    for name in [c.strip() for c in os.environ.get(
            "ALIAS_CLAIMS", "alias,username,cognito:username,preferred_username").split(",") if c.strip()]:
        value = claims.get(name)
        if isinstance(value, str) and ALIAS_RE.match(value):
            return value
    return None


def _header(headers: dict | None, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def _without(headers: dict, *names: str) -> dict:
    """Copy of headers minus the given names (case-insensitive). Only target-allowlisted headers are forwarded."""
    drop = {n.lower() for n in names}
    return {k: v for k, v in (headers or {}).items() if k.lower() not in drop}


def _log(**fields):
    print(json.dumps({"component": "alias-interceptor", **fields}, ensure_ascii=False))


def _request_id(context) -> str | None:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    return custom.get("REQUEST_ID")


def handle_http(event: dict, context) -> dict:
    req = event["http"].get("gatewayRequest") or {}
    headers = req.get("headers") or {}
    claims = _claims_from_bearer(_header(headers, "Authorization"))
    alias = alias_from_claims(claims)
    if not alias:
        _log(gateway="ingress", decision="deny", reason="no alias claim", path=req.get("path"),
             request_id=_request_id(context), claim_names=sorted(claims))
        body = json.dumps({"error": "Caller token has no usable alias claim", "error_code": "forbidden"})
        return {"interceptorOutputVersion": "1.0", "http": {"transformedGatewayResponse": {
            "statusCode": 403, "contentType": "application/json",
            "body": base64.b64encode(body.encode()).decode()}}}

    out_headers = _without(headers, "authorization", RUNTIME_ALIAS_HEADER.lower())
    out_headers[RUNTIME_ALIAS_HEADER] = alias  # interceptor headers win, so a client cannot spoof the alias
    _log(gateway="ingress", decision="allow", alias=alias, path=req.get("path"), client_id=claims.get("client_id"),
         token_use=claims.get("token_use"), request_id=_request_id(context))
    # Authorization is intentionally omitted: the gateway signs the runtime call with its IAM role.
    return {"interceptorOutputVersion": "1.0", "http": {"transformedGatewayRequest": {
        "headers": out_headers, "body": req.get("body")}}}


def tool_acl() -> dict | None:
    """TOOL_ACL: {"terraform": ["web-search-tool", "git-pr-terraform"], "coe": [...], "_default": [...]}.

    Keys are agent kinds (x-agent-kind, set by agent code); values are gateway target names. Unset = allow all.
    The CSP architect agent sends no kind and gets "_default"."""
    raw = os.environ.get("TOOL_ACL")
    return json.loads(raw) if raw else None


def _mcp_error(body: dict, code: int, message: str) -> dict:
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
        "statusCode": 200, "body": {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": code, "message": message}}}}}


def handle_mcp(event: dict, context) -> dict:
    req = event["mcp"].get("gatewayRequest") or {}
    headers = req.get("headers") or {}
    body = req.get("body") or {}
    alias = _header(headers, TOOLS_ALIAS_HEADER)
    method = body.get("method")
    if method == "tools/call":
        params = body.get("params") or {}
        args = params.get("arguments") or {}
        if not (alias and ALIAS_RE.match(alias)) and os.environ.get("REQUIRE_ALIAS", "true").lower() == "true":
            _log(gateway="tools", decision="deny", reason="missing x-user-alias", tool=params.get("name"))
            return _mcp_error(body, -32001, "x-user-alias header is required")
        kind = _header(headers, AGENT_HEADER)
        kind = kind if kind and AGENT_RE.match(kind) else "_default"
        acl = tool_acl()
        target = str(params.get("name") or "").split("___", 1)[0]
        if acl is not None and target not in acl.get(kind, []):
            _log(gateway="tools", decision="deny", reason="tool not allowed for agent", agent=kind, alias=alias,
                 tool=params.get("name"))
            return _mcp_error(body, -32003, f"tool '{params.get('name')}' is not allowed for agent '{kind}'")
        _log(gateway="tools", decision="allow", alias=alias, agent=kind, tool=params.get("name"),
             query=args.get("query") if isinstance(args.get("query"), str) else None,
             files=len(args["files"]) if isinstance(args.get("files"), list) else None)
    out_headers = _without(headers, "authorization")
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {
        "headers": out_headers, "body": body}}}


def lambda_handler(event, context):
    if "http" in event:
        return handle_http(event, context)
    if "mcp" in event:
        return handle_mcp(event, context)
    _log(decision="pass", reason="unknown payload shape", keys=sorted(event))
    return {"interceptorOutputVersion": "1.0"}
