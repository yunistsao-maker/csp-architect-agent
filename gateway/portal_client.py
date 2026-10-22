"""Try the portal agents (COE / AICOE documents, Terraform) through the private ingress gateway.

Run where the on-prem portal would run (on-prem host over DX, or inside the VPC). Authentication:
  - corporate IdP: export CSP_TOKEN=<access token JWT from your SSO>   (what the real portal sends)
  - Cognito test pool from deploy.py: yhtsao + fake-user-01.. (--user), passwords from gateway/.test-users.json

    # COE: draft a solution design from a whiteboard photo, keep refining it in the same session, then open a PR
    python gateway/portal_client.py coe --doc-type solution-design --attach whiteboard.jpg --session pay-2026-001 \\
        "新的支付閘道上雲，需要 DX 備援與 PrivateLink，RTO 1 小時"
    python gateway/portal_client.py coe --doc-type solution-design --session pay-2026-001 "補上成本估算，月預算 8000 USD"
    python gateway/portal_client.py publish coe --job <job_id>

    # AICOE: review an existing document (text or an uploaded .docx / .pdf)
    python gateway/portal_client.py aicoe --mode review --doc-type use-case-assessment --attach proposal.docx \\
        "請依 AI CoE 檢核表審查"

    # Terraform from a CSP architect job (same caller), then a PR to the infrastructure repo
    python gateway/portal_client.py terraform --architecture-job <csp job_id> --title payments-network \\
        "Implement the network layer only" --publish

    python gateway/portal_client.py checks coe        # negative checks + cross-user isolation (yhtsao vs fake-user-01)
    python gateway/portal_client.py coe --user fake-user-02 --doc-type exception-request "..."   # act as a fake user
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import STATE_FILE, call, check, claims, login, test_password  # noqa: E402

AGENTS = {"coe": "coe-doc", "aicoe": "aicoe-doc", "terraform": "terraform"}


class Auth:
    def __init__(self, state: dict, user: str | None, static_ok: bool = True):
        self.state, self.user = state, user or state.get("test_user")
        self.static = os.environ.get("CSP_TOKEN") if static_ok else None
        self.password = None if self.static else test_password(self.user, primary=user is None)
        self.token = self.static or login(state, self.user, self.password)

    def fresh(self) -> str:
        if not self.static and claims(self.token).get("exp", 0) - time.time() < 120:
            self.token = login(self.state, self.user, self.password)
        return self.token


def url_for(state: dict, agent: str) -> str:
    target = (state.get("agents", {}).get(agent) or {}).get("target") or AGENTS[agent]
    return f"https://{urlparse(state['ingress_gateway_url']).netloc}/{target}/invocations"


def upload(url: str, auth: Auth, path: Path) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix.lower() == ".md":
        ctype = "text/markdown"
    data = path.read_bytes()
    status, up = call(url, {"action": "upload_url", "filename": path.name, "content_type": ctype, "size": len(data)},
                      auth.fresh())
    if status != 200 or not isinstance(up, dict) or up.get("error"):
        sys.exit(f"upload_url failed: HTTP {status} {up}")
    req = urllib.request.Request(up["url"], data=data, method="PUT", headers=up["headers"])
    with urllib.request.urlopen(req, timeout=120) as r:  # to the S3 interface endpoint over DX
        if r.status not in (200, 204):
            sys.exit(f"upload of {path} failed: HTTP {r.status}")
    print(f"  uploaded {path.name} -> {up['key']} (kept {up.get('retention_days')} days)")
    return up["key"]


def wait(url: str, auth: Auth, job_id: str, poll: int = 15, max_wait: int = 8 * 3600) -> dict:
    t0 = time.time()
    while time.time() - t0 < max_wait:
        status, data = call(url, {"action": "status", "job_id": job_id}, auth.fresh(), timeout=60)
        if status != 200 or not isinstance(data, dict) or data.get("status") != "RUNNING":
            return data if isinstance(data, dict) else {"error": f"HTTP {status}: {data}"}
        print(f"  … RUNNING {data.get('elapsed_s')}s", flush=True)
        time.sleep(poll)
    return {"error": f"still running after {max_wait}s", "job_id": job_id}


def save(result: dict, out: Path) -> None:
    for rel, content in (result.get("files") or {}).items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if result.get("files"):
        print(f"  saved {len(result['files'])} file(s) under {out}/")


def show(result: dict) -> None:
    keep = {k: result.get(k) for k in ("status", "agent", "job_id", "session_id", "mode", "doc_type", "verdict",
                                        "publishable", "open_questions", "caller", "error", "error_code")
            if result.get(k) is not None}
    print(json.dumps(keep, ensure_ascii=False, indent=1))
    checks = result.get("checks") or {}
    for f in checks.get("findings", [])[:20]:
        print(f"  [{f['severity']}] {f['rule']} {f['file']}: {f['message']}")
    if checks.get("missing_sections"):
        print("  missing sections:", ", ".join(checks["missing_sections"]))


def publish(url: str, auth: Auth, job_id: str) -> dict:
    status, data = call(url, {"action": "publish", "job_id": job_id}, auth.fresh(), timeout=120)
    if isinstance(data, dict) and data.get("pull_request"):
        print(f"  pull request: {data['pull_request'].get('url')} (branch {data['pull_request'].get('branch')})")
    else:
        print(f"  publish failed: HTTP {status} {data}")
    return data if isinstance(data, dict) else {}


def cross_user_checks(url: str, a: Auth, b: Auth) -> list[bool]:
    """Two Cognito users (e.g. yhtsao and fake-user-01): nothing of A's is reachable for B."""
    out = []
    sid = f"xuser-{uuid.uuid4().hex[:12]}"
    payload = {"prompt": "Say hello in one sentence.", "doc_type": "solution-design", "session_id": sid}
    _, job_a = call(url, payload, a.fresh())
    _, job_b = call(url, payload, b.fresh())  # same session_id while A's job is running
    out.append(check(f"{b.user} gets their own session for the same session_id",
                     isinstance(job_b, dict) and job_b.get("status") == "RUNNING", str(job_b)[:120]))
    if isinstance(job_a, dict) and job_a.get("job_id"):
        jid = job_a["job_id"]
        _, d = call(url, {"action": "status", "job_id": jid}, b.fresh())
        out.append(check(f"{b.user} cannot read {a.user}'s job", isinstance(d, dict) and d.get("error_code") == "not_found"))
        wait(url, a, jid)
        _, d = call(url, {"action": "publish", "job_id": jid}, b.fresh())
        out.append(check(f"{b.user} cannot publish {a.user}'s job", isinstance(d, dict) and d.get("error_code") == "not_found"))
    _, up = call(url, {"action": "upload_url", "filename": "a.png", "content_type": "image/png", "size": 100}, a.fresh())
    if isinstance(up, dict) and up.get("key"):
        _, d = call(url, {"prompt": "x", "doc_type": "solution-design", "attachments": [up["key"]]}, b.fresh())
        out.append(check(f"{b.user} cannot attach {a.user}'s upload",
                         isinstance(d, dict) and d.get("error_code") == "bad_request", str(d)[:120]))
    if isinstance(job_b, dict) and job_b.get("job_id"):
        wait(url, b, job_b["job_id"])
    return out


def negative_checks(url: str, auth: Auth, other: "Auth | None" = None) -> bool:
    alias = claims(auth.token).get("username") or claims(auth.token).get("preferred_username") or "x"
    results = []
    _, d = call(url, {"prompt": "x", "doc_type": "solution-design", "mode": "prepare",
                      "attachments": [f"uploads/not-{alias}/2026/01/01/{uuid.uuid4().hex}/a.png"]}, auth.fresh())
    results.append(check("Someone else's attachment is refused", isinstance(d, dict) and d.get("error_code") == "bad_request",
                         str(d)[:120]))
    _, d = call(url, {"action": "publish", "job_id": "0" * 32}, auth.fresh())
    results.append(check("Unknown / foreign job cannot be published", isinstance(d, dict) and d.get("error_code") == "not_found"))
    sid = f"checks-{uuid.uuid4().hex[:12]}"
    payload = {"prompt": "Say hello in one sentence.", "doc_type": "solution-design", "session_id": sid}
    _, first = call(url, payload, auth.fresh())
    _, second = call(url, payload, auth.fresh())
    results.append(check("A second request in a busy session is refused",
                         isinstance(second, dict) and second.get("error_code") == "session_busy", str(second)[:120]))
    if isinstance(first, dict) and first.get("job_id"):
        wait(url, auth, first["job_id"])
    _, d = call(url, {"action": "upload_url", "filename": "x.exe", "content_type": "application/x-msdownload",
                      "size": 10}, auth.fresh())
    results.append(check("Executable uploads are refused", isinstance(d, dict) and d.get("error_code") == "bad_request"))
    if other:
        results += cross_user_checks(url, auth, other)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return all(results)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", help="coe | aicoe | terraform | publish | checks")
    ap.add_argument("rest", nargs="*", help="prompt (or agent name for publish / checks)")
    ap.add_argument("--mode", choices=["prepare", "review"], default="prepare")
    ap.add_argument("--doc-type")
    ap.add_argument("--document", type=Path, help="review mode: Markdown / text file to review inline")
    ap.add_argument("--attach", type=Path, action="append", default=[], help="image or document to upload (repeatable)")
    ap.add_argument("--session", help="session_id: keep a multi-turn conversation (stored in DynamoDB, 90 days)")
    ap.add_argument("--title")
    ap.add_argument("--language")
    ap.add_argument("--architecture-job", help="terraform: CSP architect job_id to implement")
    ap.add_argument("--job", help="publish: the job to publish")
    ap.add_argument("--publish", action="store_true", help="open a pull request when the job succeeds")
    ap.add_argument("--user", help="Cognito test user (default: the primary user, yhtsao)")
    ap.add_argument("--other-user", help="checks: second user for isolation checks (default: first fake user)")
    ap.add_argument("-o", "--out", type=Path, default=Path("portal-out"))
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text())
    auth = Auth(state, args.user)
    if args.command in ("publish", "checks"):
        agent = (args.rest or ["coe"])[0]
        url = url_for(state, agent)
        if args.command == "checks":
            fakes = [u for u in state.get("test_users", []) if u != auth.user]
            other_user = args.other_user or (fakes[0] if fakes else None)
            other = Auth(state, other_user, static_ok=False) if other_user else None
            sys.exit(0 if negative_checks(url, auth, other) else 1)
        if not args.job:
            sys.exit("publish needs --job")
        sys.exit(0 if publish(url, auth, args.job).get("pull_request") else 1)
    if args.command not in AGENTS:
        sys.exit(f"unknown agent {args.command}; use {', '.join(AGENTS)}")
    url = url_for(state, args.command)
    payload = {"prompt": " ".join(args.rest) or "Please prepare the document from the attached material."}
    if args.command in ("coe", "aicoe"):
        payload.update(mode=args.mode, doc_type=args.doc_type or ("solution-design" if args.command == "coe"
                                                                   else "use-case-assessment"))
        if args.document:
            payload["document"] = args.document.read_text(encoding="utf-8")
    if args.architecture_job:
        payload["architecture_job_id"] = args.architecture_job
    for key, val in (("session_id", args.session), ("title", args.title), ("language", args.language)):
        if val:
            payload[key] = val
    if args.attach:
        payload["attachments"] = [upload(url, auth, p) for p in args.attach]
    status, sub = call(url, payload, auth.fresh())
    if status != 200 or not isinstance(sub, dict) or not sub.get("job_id") or sub.get("error"):
        sys.exit(f"submit failed: HTTP {status} {sub}")
    print(f"  job {sub['job_id']} submitted")
    result = wait(url, auth, sub["job_id"], sub.get("poll_after_s") or 15)
    show(result)
    save(result, args.out / sub["job_id"][:8])
    if args.publish and result.get("status") == "SUCCEEDED":
        publish(url, auth, sub["job_id"])
    sys.exit(0 if result.get("status") == "SUCCEEDED" else 1)


if __name__ == "__main__":
    main()
