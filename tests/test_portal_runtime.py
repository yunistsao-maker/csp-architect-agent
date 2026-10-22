"""PortalAgentApp: alias, async jobs, sessions + lock, uploads, publish as PR (moto S3/DynamoDB, fake tools gateway)."""
import json
from types import SimpleNamespace

import pytest

import jobs
from portal.runtime import PortalAgentApp, Settings

H = "x-amzn-bedrock-agentcore-runtime-custom-user-alias"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50


def ctx(alias="yhtsao"):
    return SimpleNamespace(session_id="rt-session", request_headers={H: alias} if alias else {})


class FakeTools:
    calls = []

    def __init__(self, alias, result=None):
        self.alias, self.result = alias, result

    def find_tool(self, target, tool=None):
        return {"name": f"{target}___{tool}"}

    def call_tool(self, name, args):
        FakeTools.calls.append((self.alias, name, args))
        return {"content": [{"type": "text", "text": json.dumps(self.result or {
            "url": "https://gitlab.corp/x/-/merge_requests/7", "number": 7, "branch": "agent/coe/yhtsao/abc"})}]}


@pytest.fixture()
def portal(aws):
    seen = {}

    def handler(payload, rc):
        seen.setdefault("contexts", []).append(rc)
        if payload.get("explode"):
            raise RuntimeError("model unavailable")
        out = {"files": {"docs/coe/a.md": "# A\n"}, "summary": "draft", "publishable": not payload.get("bad")}
        if rc.session_manager:
            out["history"] = rc.session_manager.session_id
        return out

    p = PortalAgentApp("coe", handler, settings=Settings(
        require_alias=True, async_default=True, session_table="sessions", upload_bucket="uploads-bucket",
        session_bucket="uploads-bucket", tools_gateway_url="https://tools.example/mcp", git_target="git-pr"))
    p._job_store, p._table = jobs.MemoryJobStore(), aws["table"]
    import boto3
    from botocore.config import Config
    p._s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
    p.spawned = []
    p.spawn = lambda fn, *a: p.spawned.append((fn, a))
    p.tools_client = lambda alias: FakeTools(alias)
    p.seen = seen
    FakeTools.calls = []
    return p


def drain(p):
    while p.spawned:
        fn, a = p.spawned.pop(0)
        fn(*a)


def test_alias_is_required(portal):
    assert portal.invoke({"prompt": "x"}, ctx(None))["error_code"] == "forbidden"
    assert portal.invoke({"action": "drop"}, ctx())["error_code"] == "bad_request"


def test_async_job_with_session_and_attachment(portal, aws):
    aws["s3"].put_object(Bucket="uploads-bucket", Key="uploads/yhtsao/k/a.png", Body=PNG, ContentType="image/png")
    sub = portal.invoke({"prompt": "x", "session_id": "pay-2026-001", "attachments": ["uploads/yhtsao/k/a.png"]}, ctx())
    assert sub["status"] == "RUNNING" and sub["session_id"] == "pay-2026-001" and sub["agent"] == "coe"
    busy = portal.invoke({"prompt": "y", "session_id": "pay-2026-001"}, ctx())
    assert busy["error_code"] == "session_busy"
    assert portal.invoke({"prompt": "y", "session_id": "pay-2026-001"}, ctx("eve"))["status"] == "RUNNING"  # own key
    drain(portal)
    done = portal.invoke({"action": "status", "job_id": sub["job_id"]}, ctx())
    assert done["status"] == "SUCCEEDED" and done["history"] == "pay-2026-001"
    assert done["attachments"] == [{"key": "uploads/yhtsao/k/a.png", "kind": "image", "format": "png", "bytes": len(PNG)}]
    rc = portal.seen["contexts"][0]
    assert rc.alias == "yhtsao" and rc.user_message("hi")[1]["image"]["format"] == "png"
    assert portal.invoke({"prompt": "z", "session_id": "pay-2026-001"}, ctx())["status"] == "RUNNING"  # lock released
    assert portal.invoke({"action": "status", "job_id": sub["job_id"]}, ctx("eve"))["error_code"] == "not_found"


def test_handler_crash_fails_the_job_and_frees_the_session(portal):
    sub = portal.invoke({"prompt": "x", "session_id": "crash-session", "explode": True}, ctx())
    drain(portal)
    out = portal.invoke({"action": "status", "job_id": sub["job_id"]}, ctx())
    assert out["status"] == "FAILED" and out["error_code"] == "internal_error" and "model unavailable" in out["error"]
    assert portal.invoke({"prompt": "x", "session_id": "crash-session"}, ctx())["status"] == "RUNNING"
    assert portal.app.get_async_task_info()["active_count"] == 0


def test_foreign_or_missing_attachments(portal):
    assert portal.invoke({"prompt": "x", "attachments": ["uploads/eve/k/a.png"]}, ctx())["error_code"] == "bad_request"
    sub = portal.invoke({"prompt": "x", "attachments": ["uploads/yhtsao/k/missing.png"]}, ctx())
    drain(portal)
    out = portal.invoke({"action": "status", "job_id": sub["job_id"]}, ctx())
    assert out["status"] == "FAILED" and "not found" in out["error"]


def test_sync_mode_and_bad_session_ids(portal):
    out = portal.invoke({"prompt": "x", "async": False, "session_id": "sync-session-1"}, ctx())
    assert out["files"] and out["caller"] == {"alias": "yhtsao"}
    assert portal.invoke({"prompt": "x", "async": False, "session_id": "sync-session-1"}, ctx())["files"]  # released
    assert portal.invoke({"prompt": "x", "session_id": "no"}, ctx())["error_code"] == "bad_request"


def test_upload_url(portal):
    portal._presign_s3 = None  # the real presigning client
    up = portal.invoke({"action": "upload_url", "filename": "board.jpg", "content_type": "image/jpeg", "size": 2048}, ctx())
    assert up["key"].startswith("uploads/yhtsao/") and up["method"] == "PUT"
    # regional host, so on-prem DNS resolves it to the S3 interface endpoint (never the global s3.amazonaws.com)
    assert up["url"].startswith("https://uploads-bucket.s3.us-east-1.amazonaws.com/uploads/yhtsao/")
    assert "X-Amz-Signature=" in up["url"]
    bad = portal.invoke({"action": "upload_url", "filename": "a.exe", "content_type": "application/x-msdownload",
                         "size": 10}, ctx())
    assert bad["error_code"] == "bad_request"


def test_publish_opens_one_pr_per_job(portal):
    sub = portal.invoke({"prompt": "x"}, ctx())
    drain(portal)
    assert portal.invoke({"action": "publish", "job_id": sub["job_id"]}, ctx("eve"))["error_code"] == "not_found"
    pr = portal.invoke({"action": "publish", "job_id": sub["job_id"], "title": "CoE review: payments"}, ctx())
    assert pr["pull_request"]["number"] == 7
    again = portal.invoke({"action": "publish", "job_id": sub["job_id"]}, ctx())
    assert again["pull_request"] == pr["pull_request"] and len(FakeTools.calls) == 1
    alias, name, args = FakeTools.calls[0]
    assert alias == "yhtsao" and name == "git-pr___open_pull_request"
    assert args["files"] == [{"path": "docs/coe/a.md", "content": "# A\n"}] and args["title"] == "CoE review: payments"
    assert "yhtsao" in args["body"] and sub["job_id"] in args["body"]
    status = portal.invoke({"action": "status", "job_id": sub["job_id"]}, ctx())
    assert status["pull_request"]["number"] == 7


def test_publish_refuses_failed_checks_and_running_jobs(portal):
    sub = portal.invoke({"prompt": "x", "bad": True}, ctx())
    assert portal.invoke({"action": "publish", "job_id": sub["job_id"]}, ctx())["error_code"] == "bad_request"  # running
    drain(portal)
    assert portal.invoke({"action": "publish", "job_id": sub["job_id"]}, ctx())["error_code"] == "checks_failed"
    assert FakeTools.calls == []


def test_sync_request_keeps_renewing_its_lease(aws):
    """A synchronous request longer than the lease must still hold the session (lease renewed meanwhile)."""
    import time as _t
    seen = {}

    def slow(payload, rc):
        _t.sleep(2.5)
        seen["second"] = p.invoke({"prompt": "y", "async": False, "session_id": "long-sync-1"}, ctx())
        return {"ok": True}

    p = PortalAgentApp("coe", slow, settings=Settings(require_alias=True, session_table="sessions",
                                                      heartbeat_seconds=0.2, stale_seconds=1.5))
    p._job_store, p._table = jobs.MemoryJobStore(), aws["table"]
    p._context = lambda payload, alias, job_id, sid: __import__("portal.runtime", fromlist=["RunContext"]).RunContext(
        "coe", alias, job_id, sid)
    out = p.invoke({"prompt": "x", "async": False, "session_id": "long-sync-1"}, ctx())
    assert out["ok"] and seen["second"]["error_code"] == "session_busy"


def test_publish_only_from_the_agent_that_made_the_job(portal):
    sub = portal.invoke({"prompt": "x"}, ctx())
    drain(portal)
    record = portal.job_store.get(sub["job_id"])
    portal.job_store.put({**record, "request": {**record["request"], "agent": "terraform"}})
    assert portal.invoke({"action": "publish", "job_id": sub["job_id"]}, ctx())["error_code"] == "not_found"
