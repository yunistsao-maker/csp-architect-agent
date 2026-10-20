"""Async job mode: submit returns at once, the work runs in the background, status calls read the job store."""
import importlib
import json
import time
from types import SimpleNamespace

import pytest

import jobs
from test_skills import ScriptedModel

GOOD = ('<mxfile><diagram id="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" value="EKS" style="shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.eks;" vertex="1" '
        'parent="1"><mxGeometry width="50" height="50" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>')
ALIAS_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-user-alias"


def ctx(alias="yhtsao", session="s-1"):
    return SimpleNamespace(session_id=session, request_headers={ALIAS_HEADER: alias} if alias else {})


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.delenv("TOOLS_GATEWAY_URL", raising=False)
    mod = importlib.reload(importlib.import_module("agent"))
    monkeypatch.setattr(mod, "_job_store", jobs.MemoryJobStore())
    spawned = []
    monkeypatch.setattr(mod, "spawn", lambda fn, *a: spawned.append((fn, a)))
    mod.spawned = spawned
    return mod


def run_spawned(mod):
    while mod.spawned:
        fn, a = mod.spawned.pop(0)
        fn(*a)


def test_submit_returns_job_id_then_status_has_the_result(agent, monkeypatch):
    model = ScriptedModel([{"text": f"```xml\n{GOOD}\n```\n## Overview\nok"}])
    monkeypatch.setattr(agent, "BedrockModel", lambda **kw: model)
    sub = agent.invoke({"prompt": "3-tier app", "mode": "aws", "async": True}, ctx())
    assert sub["status"] == "RUNNING" and jobs.JOB_ID_RE.match(sub["job_id"]) and sub["caller"] == {"alias": "yhtsao"}
    assert model.system_prompts == []  # nothing generated inside the submit call

    running = agent.invoke({"action": "status", "job_id": sub["job_id"]}, ctx(session="other-microvm"))
    assert running["status"] == "RUNNING" and "xml" not in running and running["request"]["mode"] == "aws"

    run_spawned(agent)
    done = agent.invoke({"action": "status", "job_id": sub["job_id"]}, ctx(session="third"))
    assert done["status"] == "SUCCEEDED" and done["xml"] == GOOD and done["validation"]["ok"]
    assert done["caller"] == {"alias": "yhtsao"} and done["elapsed_s"] >= 0


def test_only_the_submitting_alias_can_read_a_job(agent, monkeypatch):
    monkeypatch.setattr(agent, "generate", lambda p, a, s: {"xml": GOOD, "caller": {"alias": a}})
    job_id = agent.invoke({"prompt": "x", "async": True}, ctx("yhtsao"))["job_id"]
    run_spawned(agent)
    for other in (ctx("someone-else"), ctx(None)):
        out = agent.invoke({"action": "status", "job_id": job_id}, other)
        assert out["error_code"] == "not_found" and "xml" not in out


@pytest.mark.parametrize("job_id", [None, "", "../../etc/passwd", "A" * 32, jobs.new_job_id()])
def test_bad_or_unknown_job_ids(agent, job_id):
    assert agent.invoke({"action": "status", "job_id": job_id}, ctx())["error_code"] == "not_found"


def test_failures_reach_a_terminal_state(agent, monkeypatch):
    def boom(p, a, s):
        raise RuntimeError("bedrock throttled")
    monkeypatch.setattr(agent, "generate", boom)
    job_id = agent.invoke({"prompt": "x", "async": True}, ctx())["job_id"]
    run_spawned(agent)
    out = agent.invoke({"action": "status", "job_id": job_id}, ctx())
    assert out["status"] == "FAILED" and out["error_code"] == "internal_error" and "throttled" in out["error"]
    assert agent.app.get_async_task_info()["active_count"] == 0  # ping back to Healthy


def test_max_tokens_job_is_failed_with_its_error_code(agent, monkeypatch):
    monkeypatch.setattr(agent, "generate", lambda p, a, s: {"error": "too big", "error_code": "max_tokens"})
    job_id = agent.invoke({"prompt": "x", "async": True}, ctx())["job_id"]
    run_spawned(agent)
    assert agent.invoke({"action": "status", "job_id": job_id}, ctx())["error_code"] == "max_tokens"


def test_runtime_is_busy_while_the_job_runs(agent, monkeypatch):
    seen = {}

    def gen(p, a, s):
        seen["active"] = agent.app.get_async_task_info()["active_count"]
        seen["ping"] = agent.app.get_current_ping_status().value
        return {"xml": GOOD}
    monkeypatch.setattr(agent, "generate", gen)
    agent.invoke({"prompt": "x", "async": True}, ctx())
    run_spawned(agent)
    assert seen == {"active": 1, "ping": "HealthyBusy"}


def test_async_default_and_sync_override(agent, monkeypatch):
    monkeypatch.setattr(agent, "ASYNC_DEFAULT", True)
    monkeypatch.setattr(agent, "generate", lambda p, a, s: {"xml": GOOD, "sync": True})
    assert "job_id" in agent.invoke({"prompt": "x"}, ctx())
    assert agent.invoke({"prompt": "x", "async": False}, ctx()) == {"xml": GOOD, "sync": True}
    assert agent.invoke({"action": "delete"}, ctx())["error_code"] == "bad_request"
    assert agent.invoke({"async": True}, ctx())["error"].startswith("Missing 'prompt'")


def test_real_background_thread(monkeypatch):
    monkeypatch.delenv("TOOLS_GATEWAY_URL", raising=False)
    mod = importlib.reload(importlib.import_module("agent"))
    monkeypatch.setattr(mod, "_job_store", jobs.MemoryJobStore())
    monkeypatch.setattr(mod, "generate", lambda p, a, s: (time.sleep(0.2), {"xml": GOOD})[1])
    job_id = mod.invoke({"prompt": "x", "async": True}, ctx())["job_id"]
    for _ in range(50):
        out = mod.invoke({"action": "status", "job_id": job_id}, ctx())
        if out["status"] != "RUNNING":
            break
        time.sleep(0.05)
    assert out["status"] == "SUCCEEDED" and out["xml"] == GOOD


# ------------------------------------------------------------------------------------------- jobs module
def test_stopped_heartbeat_is_reported_as_lost():
    rec = jobs.new_record(jobs.new_job_id(), "yhtsao", {})
    assert jobs.public_view(rec, stale_after=300, at=rec["heartbeat_at"] + 299)["status"] == "RUNNING"
    lost = jobs.public_view(rec, stale_after=300, at=rec["heartbeat_at"] + 301)
    assert lost["status"] == "LOST" and lost["error_code"] == "job_lost"


def test_heartbeat_refreshes_and_never_outlives_the_job():
    store = jobs.MemoryJobStore()
    rec = jobs.new_record(jobs.new_job_id(), "yhtsao", {})
    store.put(rec)
    with jobs.Heartbeat(store, rec, interval=0.02):
        time.sleep(0.1)
    assert store.get(rec["job_id"])["heartbeat_at"] > rec["heartbeat_at"]
    store.put({**rec, "status": "SUCCEEDED"})
    time.sleep(0.06)
    assert store.get(rec["job_id"])["status"] == "SUCCEEDED"


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": SimpleNamespace(read=lambda: self.objects[(Bucket, Key)])}


def test_s3_store_layout():
    s3 = FakeS3()
    store = jobs.S3JobStore("bucket", client=s3)
    rec = jobs.new_record("ab" * 16, "yhtsao", {"mode": "aws"})
    store.put(rec)
    assert list(s3.objects) == [("bucket", f"jobs/{'ab' * 16}.json")]
    assert store.get(rec["job_id"]) == json.loads(json.dumps(rec)) and store.get("cd" * 16) is None
