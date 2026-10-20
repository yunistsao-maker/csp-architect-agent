"""gateway/client.py polling logic with a fake gateway (no network, no Cognito)."""
import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gateway_client", ROOT / "gateway/client.py")
client = importlib.util.module_from_spec(spec)
sys.modules["gateway_client"] = client
spec.loader.exec_module(client)


def token(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc({'username': 'yhtsao', 'exp': exp})}.sig"


def test_polls_until_done_and_refreshes_an_expiring_token(monkeypatch):
    logins = []
    monkeypatch.setattr(client, "login", lambda st, u, p: logins.append(u) or token(time.time() + (60 if len(logins) == 1 else 3600)))
    answers = iter([{"status": "RUNNING", "elapsed_s": 1}, {"status": "RUNNING", "elapsed_s": 2},
                    {"status": "SUCCEEDED", "xml": "<mxfile/>", "caller": {"alias": "yhtsao"}}])
    seen = []
    monkeypatch.setattr(client, "call", lambda url, payload, tok, timeout=120: (seen.append((payload, tok)), (200, next(answers)))[1])
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    sess = client.Session({}, "yhtsao", "pw")
    status, data = client.wait_for_job("https://gw/csp-architect/invocations", sess, "ab" * 16, poll=15, max_wait=60)
    assert status == 200 and data["status"] == "SUCCEEDED"
    assert [p for p, _ in seen] == [{"action": "status", "job_id": "ab" * 16}] * 3
    assert len(logins) == 2  # first token expired within 120 s, so the client logged in again once


def test_gives_up_after_max_wait(monkeypatch):
    monkeypatch.setattr(client, "login", lambda *a: token(time.time() + 3600))
    monkeypatch.setattr(client, "call", lambda *a, **k: (200, {"status": "RUNNING"}))
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(client.time, "time", lambda: next(clock))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    sess = client.Session({}, "yhtsao", "pw")
    _, data = client.wait_for_job("u", sess, "cd" * 16, poll=15, max_wait=500)
    assert "--job " + "cd" * 16 in data["error"]
