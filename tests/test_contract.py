"""The runtimes keep the published API contract (contract/v1): schema and fixtures are valid, every example is
accepted, and what the runtimes really return validates. A change that breaks a consumer fails here first."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import doc_agent
import form_fill
import jobs
import terraform_agent
from portal.api import API_VERSION
from test_skills import ScriptedModel

ROOT = Path(__file__).resolve().parent.parent
V1 = ROOT / "contract" / "v1"
SCHEMA = json.loads((V1 / "api.schema.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((V1 / "fixtures" / "examples.json").read_text(encoding="utf-8"))["examples"]
FLAGS = json.loads((V1 / "fixtures" / "form-flags.json").read_text(encoding="utf-8"))["cases"]
H = "x-amzn-bedrock-agentcore-runtime-custom-user-alias"
CALLER = SimpleNamespace(request_headers={H: "yhtsao"})
KNOWN_CODES = set(SCHEMA["$defs"]["error"]["properties"]["error_code"]["examples"])


def validate(name: str, instance: dict) -> None:
    Draft202012Validator({"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{name}"}).validate(instance)


def test_schema_and_version():
    Draft202012Validator.check_schema(SCHEMA)
    assert (ROOT / "contract" / "VERSION").read_text().strip() == API_VERSION
    assert API_VERSION.split(".")[0] == "1" and "/v1/" in SCHEMA["$id"]


@pytest.mark.parametrize("ex", EXAMPLES, ids=[e["name"] for e in EXAMPLES])
def test_examples_are_valid(ex):
    validate(ex["schema"], ex["instance"])


def test_every_message_shape_has_an_example():
    shapes = {k for k in SCHEMA["$defs"] if "." in k or k in ("error", "jobStarted", "status", "statusRequest", "uploadUrl",
                                                                 "uploadUrlRequest", "publishRequest", "published")}
    missing = shapes - {e["schema"] for e in EXAMPLES} - {"doc.prepare.request", "doc.prepare.result"}
    assert not missing, missing


def test_example_requests_are_accepted_by_the_runtimes():
    diagram = importlib.import_module("agent")
    for ex in EXAMPLES:
        p = ex["instance"]
        if ex["schema"] == "doc.fill.request":
            assert doc_agent.validate_payload("coe", p) is None, ex["name"]
        elif ex["schema"] == "terraform.request":
            assert terraform_agent.validate_payload(p) is None, ex["name"]
        elif ex["schema"] == "diagram.request":
            assert diagram._check_request(p) is None, ex["name"]


@pytest.mark.parametrize("case", FLAGS, ids=[c["name"] for c in FLAGS])
def test_form_flag_rules(case):
    assert form_fill.flag(form_fill.clean_item(case["item"])) == case["flag"]


# ------------------------------------------------------------------ real runtime output
@pytest.fixture
def portal_app(monkeypatch):
    def use(module):
        p = module.portal
        for k, v in {"require_alias": True, "async_default": True, "session_table": None}.items():
            monkeypatch.setattr(p.settings, k, v)
        monkeypatch.setattr(p, "_job_store", jobs.MemoryJobStore())
        monkeypatch.setattr(p, "spawn", lambda fn, *a: fn(*a))
        return module
    return use


def run_job(module, payload):
    started = module.invoke(payload, CALLER)
    validate("jobStarted", started)
    status = module.invoke({"action": "status", "job_id": started["job_id"], "api_version": "1"}, CALLER)
    validate("status", status)
    return started, status


def test_fill_job_matches_the_contract(portal_app, monkeypatch):
    import coe_agent
    form = next(e["instance"]["form"] for e in EXAMPLES if e["name"] == "fill a blank form")
    rows = [{"id": "C-01", "answer": "Y", "note": "SSO", "confidence": 90, "basis": "prd.md"},
            {"id": "Q8-1", "selected": None, "confidence": 20}, {"id": "Q1", "text": "Refunds", "confidence": 60}]
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: ScriptedModel([{"text": "<fill>" + json.dumps(
        {"summary": "ok", "ask": ["Q8-1: employee data?"], "items": rows}) + "</fill>"}]))
    started, status = run_job(portal_app(coe_agent), {"mode": "fill", "prompt": "fill", "form": form, "api_version": "1",
                                                       "trace_id": "req-42", "context": "PROJECT FILES"})
    assert started["api_version"] == API_VERSION and started["trace_id"] == "req-42"
    assert status["status"] == "SUCCEEDED" and status["trace_id"] == "req-42"
    validate("doc.fill.result", status)


def test_terraform_job_matches_the_contract(portal_app, monkeypatch):
    from test_terraform_agent import GOOD, blocks
    monkeypatch.setattr(terraform_agent, "BedrockModel", lambda **kw: ScriptedModel([{"text": blocks(GOOD) + "\nDone."}]))
    _, status = run_job(portal_app(terraform_agent), {"prompt": "VPC", "title": "payments-network", "trace_id": "req-9"})
    assert status["status"] == "SUCCEEDED"
    validate("terraform.result", status)


def test_diagram_matches_the_contract(monkeypatch):
    from test_agent import GOOD, StubAgent, reply
    mod = importlib.reload(importlib.import_module("agent"))
    monkeypatch.setattr(mod, "Agent", StubAgent)
    monkeypatch.setattr(mod, "BedrockModel", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(mod, "REQUIRE_CALLER_ALIAS", True)
    monkeypatch.setattr(mod, "_job_store", jobs.MemoryJobStore())
    monkeypatch.setattr(mod, "spawn", lambda fn, *a: fn(*a))
    StubAgent.queue = [reply(GOOD), reply(GOOD)]
    sync = mod.invoke({"prompt": "ECR", "mode": "aws", "async": False, "trace_id": "req-1"}, CALLER)
    validate("diagram.result", sync)
    assert sync["api_version"] == API_VERSION and sync["trace_id"] == "req-1"
    started = mod.invoke({"prompt": "ECR", "mode": "software-architecture", "async": True, "api_version": "1", "trace_id": "req-2"}, CALLER)
    validate("jobStarted", started)
    status = mod.invoke({"action": "status", "job_id": started["job_id"]}, CALLER)
    validate("status", status)
    validate("diagram.result", status)
    assert status["trace_id"] == "req-2"


def test_errors_match_the_contract(portal_app):
    import aicoe_agent
    mod = portal_app(aicoe_agent)
    diagram = importlib.import_module("agent")
    errors = [
        mod.invoke({"action": "nope"}, CALLER),
        mod.invoke({"prompt": "x", "mode": "fill"}, CALLER),
        mod.invoke({"prompt": "x", "mode": "fill", "form": {"items": []}}, CALLER),
        mod.invoke({"prompt": "x", "mode": "fill", "form": {"items": [{"id": "a", "q": "?"}]}, "api_version": "2"}, CALLER),
        mod.invoke({"action": "status", "job_id": "f" * 32}, CALLER),
        mod.invoke({"action": "publish", "job_id": "f" * 32}, CALLER),
        diagram.invoke({}, CALLER),
        diagram.invoke({"prompt": "x", "mode": "visio"}, CALLER),
        diagram.invoke({"prompt": "x", "api_version": "2"}, CALLER),
    ]
    for e in errors:
        validate("error", e)
        assert e["error_code"] in KNOWN_CODES and e["api_version"] == API_VERSION
    assert errors[3]["error_code"] == errors[8]["error_code"] == "unsupported_api_version"
