"""Test the private, JWT-authenticated path to the CSP architect agent.

Run from a host INSIDE the VPC that has the gateway endpoint (EC2, Cloud9, or a CloudShell VPC environment).
Needs gateway/.deploy-state.json from gateway/deploy.py and the test user's password.

    python gateway/client.py "Hub-spoke vWAN with ExpressRoute and private AKS" --mode azure
    python gateway/client.py --all-checks          # positive + negative security checks
    python gateway/client.py --job 3f2a...          # resume polling a job you submitted earlier

A gateway call ends after 15 minutes, so the client submits a job (returns in a second or two) and polls
{"action": "status", "job_id": ...} until it finishes. The token is refreshed by logging in again when it is close
to expiry, so jobs longer than the 60-minute token lifetime still work. --sync waits in one call (<15 min only).

Checks (--all-checks):
  1. Cognito login as the test user (yhtsao) -> access token; the alias claim is printed
  2. Job submitted and finished through the gateway -> caller.alias == yhtsao, post-processing hook ran
  3. No token                    -> rejected (401)
  4. Token with a forged signature -> rejected (401)
  5. Direct InvokeAgentRuntime with your IAM credentials -> AccessDenied (runtime resource policy)
  6. Status of a job id that is not yours -> not_found
  Outside the VPC, check 2 is rejected (403) by the gateway resource policy (aws:SourceVpce).
"""

import argparse
import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import boto3

STATE_FILE = Path(__file__).resolve().parent / ".deploy-state.json"


def login(state: dict, user: str, password: str) -> str:
    cog = boto3.client("cognito-idp", region_name=state["region"])
    auth = cog.initiate_auth(ClientId=state["client_id"], AuthFlow="USER_PASSWORD_AUTH",
                             AuthParameters={"USERNAME": user, "PASSWORD": password})
    return auth["AuthenticationResult"]["AccessToken"]


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def invoke_url(state: dict) -> str:
    host = urlparse(state["ingress_gateway_url"]).netloc
    return f"https://{host}/{state['runtime_target']}/invocations"


def call(url: str, payload: dict, token: str | None, timeout: int = 120) -> tuple[int, dict | str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body.strip().startswith("{") else body)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return e.code, body[:500]


class Session:
    """Holds the access token and logs in again shortly before it expires."""

    def __init__(self, state: dict, user: str, password: str):
        self.state, self.user, self.password = state, user, password
        self.token = login(state, user, password)

    def fresh_token(self) -> str:
        if claims(self.token).get("exp", 0) - time.time() < 120:
            self.token = login(self.state, self.user, self.password)
        return self.token


def wait_for_job(url: str, sess: Session, job_id: str, poll: int, max_wait: int) -> tuple[int, dict | str]:
    t0, status, data = time.time(), 0, {}
    while time.time() - t0 < max_wait:
        status, data = call(url, {"action": "status", "job_id": job_id}, sess.fresh_token(), timeout=60)
        if status != 200 or not isinstance(data, dict) or data.get("status") != "RUNNING":
            return status, data
        print(f"  … {data.get('status')} {data.get('elapsed_s')}s", flush=True)
        time.sleep(poll)
    return status, {"error": f"still running after {max_wait}s; resume with --job {job_id}", "job_id": job_id}


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  · ' + detail) if detail else ''}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prompt", nargs="?", default="Three-tier web app on AWS: ALB, ECS Fargate, Aurora, private subnets in 2 AZs")
    ap.add_argument("--mode", default="aws")
    ap.add_argument("--skills", action="store_true")
    ap.add_argument("--search", action="store_true", help="let the agent use web search (tools gateway connector)")
    ap.add_argument("--user", help="default: the deployed test user (yhtsao)")
    ap.add_argument("--all-checks", action="store_true")
    ap.add_argument("-o", "--out", default="gateway-diagram")
    ap.add_argument("--sync", action="store_true", help="wait in one gateway call (fails if it takes >15 minutes)")
    ap.add_argument("--job", help="poll an existing job instead of submitting a new one")
    ap.add_argument("--poll", type=int, help="seconds between status calls (default: server hint, 15)")
    ap.add_argument("--max-wait", type=int, default=8 * 3600, help="give up polling after this many seconds")
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text())
    user = args.user or state["test_user"]
    password = os.environ.get("CSP_TEST_PASSWORD") or getpass.getpass(f"Password for {user}: ")
    url = invoke_url(state)
    print(f"Gateway invoke URL: {url}")

    results = []
    sess = Session(state, user, password)
    c = claims(sess.token)
    results.append(check("1. Cognito login", True, f"username={c.get('username')} client_id={c.get('client_id')}"))

    payload = {"prompt": args.prompt, "mode": args.mode, "async": not args.sync}
    if args.skills:
        payload["skills"] = True
    if args.search:
        payload["enable_search"] = True
    t0 = time.time()
    if args.job:
        job_id, status = args.job, 200
    else:
        status, data = call(url, payload, sess.fresh_token(), timeout=900 if args.sync else 120)
        job_id = data.get("job_id") if isinstance(data, dict) else None
        print(f"  submitted in {round(time.time() - t0, 1)}s: HTTP {status}"
              + (f", job {job_id}" if job_id else ""))
    if status == 403:
        print("  ! 403 from the gateway: are you running inside the VPC with the endpoint? (aws:SourceVpce policy)")
    if job_id:
        poll = args.poll or (data.get("poll_after_s") if not args.job and isinstance(data, dict) else None) or 15
        status, data = wait_for_job(url, sess, job_id, poll, args.max_wait)
    secs = round(time.time() - t0, 1)
    ok = status == 200 and isinstance(data, dict) and not data.get("error")
    detail = f"HTTP {status}, {data.get('status', 'sync') if isinstance(data, dict) else '?'} after {secs}s"
    results.append(check("2. Diagram through the gateway with JWT", ok, detail))
    if isinstance(data, dict):
        alias = (data.get("caller") or {}).get("alias")
        results.append(check("   alias reached the runtime", alias == user, f"caller.alias={alias}"))
        pp = data.get("postprocess") or {}
        results.append(check("   post-processing hook ran", bool(pp.get("ok")),
                             f"alias seen by hook={pp.get('alias')} artifact={pp.get('artifact')}"))
        if data.get("xml"):
            Path(f"{args.out}.drawio").write_text(data["xml"], encoding="utf-8")
            Path(f"{args.out}.md").write_text(data.get("explanation", ""), encoding="utf-8")
            print(f"  saved {args.out}.drawio / {args.out}.md")
        if data.get("error"):
            print(f"  agent error: {data.get('error_code')} {data['error']}")
    else:
        print(f"  response: {data}")

    if args.all_checks:
        probe = {"action": "status", "job_id": "0" * 32}
        status, _ = call(url, probe, None, timeout=60)
        results.append(check("3. No token is rejected", status in (401, 403), f"HTTP {status}"))
        head, body, _sig = sess.fresh_token().split(".")
        forged = f"{head}.{body}.{base64.urlsafe_b64encode(b'not-a-signature').decode().rstrip('=')}"
        status, _ = call(url, probe, forged, timeout=60)
        results.append(check("4. Forged signature is rejected", status in (401, 403), f"HTTP {status}"))
        try:
            boto3.client("bedrock-agentcore", region_name=state["region"]).invoke_agent_runtime(
                agentRuntimeArn=state["runtime_arn"], runtimeSessionId=f"direct-bypass-test-{int(time.time())}-0000000000",
                payload=json.dumps({"prompt": "bypass test", "async": False}))
            results.append(check("5. Direct runtime invoke is denied", False, "call succeeded: resource policy not applied"))
        except Exception as e:  # noqa: BLE001
            denied = "AccessDenied" in str(e)
            results.append(check("5. Direct runtime invoke is denied", denied, type(e).__name__))
        status, data = call(url, probe, sess.fresh_token(), timeout=60)
        code = data.get("error_code") if isinstance(data, dict) else None
        results.append(check("6. Someone else's / unknown job is not readable", code == "not_found",
                             f"HTTP {status} {code}"))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    print("Logs: CloudWatch Logs Insights on /aws/lambda/csp-arch-alias-interceptor and /aws/lambda/csp-arch-postprocess-hook:")
    print(f"  fields @timestamp, component, gateway, decision, alias, tool, query | filter alias = '{user}' | sort @timestamp desc")
    print(f"Job records: s3://{state.get('artifact_bucket')}/jobs/ (expire after 7 days)")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
