"""COE / AICOE document agents: templates, prompts, validation + repair, review rendering, sessions (scripted model)."""
import json
from types import SimpleNamespace

import pytest

import doc_agent
import jobs
from portal.runtime import RunContext
from test_skills import ScriptedModel

H = "x-amzn-bedrock-agentcore-runtime-custom-user-alias"


class HistoryModel(ScriptedModel):
    """Also records how many messages the model was sent (i.e. how much history the agent restored)."""

    async def stream(self, messages, *a, **kw):
        self.history_lengths = getattr(self, "history_lengths", []) + [len(messages)]
        async for ev in super().stream(messages, *a, **kw):
            yield ev


def doc_for(domain, doc_type, drop=()):
    cfg = doc_agent.load_domain(domain)
    t = cfg["doc_types"][doc_type]
    body = "\n\n".join(f"## {s['heading']}\n內容 {i}" for i, s in enumerate(t["sections"]) if s["heading"] not in drop)
    return f"# 支付閘道上雲\n\n{body}"


def review_for(domain, doc_type, verdict="approve_with_conditions", status="pass", findings=None):
    cfg = doc_agent.load_domain(domain)
    rows = [{"id": i["id"], "status": status, "evidence": "§6"} for i in doc_agent.checklist_for(cfg, doc_type)]
    return {"verdict": verdict, "summary": "ok", "checklist": rows, "findings": findings or []}


@pytest.mark.parametrize("domain", ["coe", "aicoe"])
def test_templates_load_and_checklists_cover_every_doc_type(domain):
    cfg = doc_agent.load_domain(domain)
    for dt in cfg["doc_types"]:
        items = doc_agent.checklist_for(cfg, dt)
        assert items and any(i["id"].startswith("GOV") for i in items)
        prompt = doc_agent.system_prompt(cfg, dt, "prepare", None)
        assert all(s["heading"] in prompt for s in cfg["doc_types"][dt]["sections"]) and "Traditional Chinese" in prompt
        assert all(i["id"] in doc_agent.system_prompt(cfg, dt, "review", "English") for i in items)


def test_check_document_finds_missing_required_sections():
    ok = doc_agent.check_document(doc_agent.load_domain("coe"), "solution-design", doc_for("coe", "solution-design"))
    assert ok["ok"] and not ok["missing_sections"]
    bad = doc_agent.check_document(doc_agent.load_domain("coe"), "solution-design", doc_for(
        "coe", "solution-design", drop=("6. 網路設計 Network design", "14. 上線計畫 Rollout and migration plan")))
    assert not bad["ok"] and bad["missing_sections"] == ["6. 網路設計 Network design"]
    assert bad["optional_missing"] == ["14. 上線計畫 Rollout and migration plan"]


def test_check_review_rules():
    cfg = doc_agent.load_domain("coe")
    good = review_for("coe", "exception-request")
    assert doc_agent.check_review(cfg, "exception-request", good, [])["ok"]
    r = review_for("coe", "exception-request", verdict="approve", status="fail")
    r["checklist"].pop()
    r["findings"] = [{"id": "F1", "checklist_id": "EXC-02", "severity": "high", "section": "4", "issue": "13 months"}]
    errors = doc_agent.check_review(cfg, "exception-request", r, [])["errors"]
    joined = " | ".join(errors)
    assert "without a status" in joined and "missing recommendation" in joined
    assert "'approve' is not allowed" in joined and "GOV-01 is fail but has no finding" in joined
    data, errs = doc_agent.parse_review("<review>```json\n" + json.dumps(good) + "\n```</review>")
    assert data == good and not errs
    assert doc_agent.parse_review("no tags")[1] and doc_agent.parse_review("<review>{oops</review>")[1]


def rc(**kw):
    return RunContext(kind=kw.pop("kind", "coe"), alias="yhtsao", job_id="ab" * 16, session_id=None, **kw)


def test_prepare_with_repair_turn(monkeypatch):
    first = "<document>\n" + doc_for("coe", "solution-design", drop=("7. 身分與存取 Identity and access",)) + "\n</document>"
    second = ("<document>\n" + doc_for("coe", "solution-design") + "\n</document>\n"
              '<open_questions>["DX 第二條線路的頻寬?", "RPO?"]</open_questions>\nDone.')
    model = ScriptedModel([{"text": first}, {"text": second}])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("coe", {"prompt": "支付閘道", "doc_type": "solution-design", "title": "Payments Gateway"}, rc())
    assert out["checks"]["ok"] and out["checks"]["repair_turns"] == 1 and out["publishable"]
    assert "missing required section heading: ## 7. 身分與存取 Identity and access" in model.seen[1]["content"][0]["text"]
    assert out["open_questions"] == ["DX 第二條線路的頻寬?", "RPO?"]
    (path, content), = out["files"].items()
    assert path.startswith("docs/coe/solution-design/") and path.endswith("-payments-gateway.md")
    assert "## 7. 身分與存取 Identity and access" in content and out["usage"]["model_calls"] == 2


def test_prepare_that_stays_invalid_is_not_publishable(monkeypatch):
    model = ScriptedModel([{"text": "sorry"}, {"text": "still nothing"}])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("aicoe", {"prompt": "x", "doc_type": "model-change"}, rc(kind="aicoe"))
    assert not out["publishable"] and out["files"] == {} and out["checks"]["errors"]


def test_review_mode_with_attachment(monkeypatch):
    review = review_for("aicoe", "use-case-assessment", verdict="revise", status="pass", findings=[
        {"id": "F1", "checklist_id": "OTHER", "severity": "high", "section": "6. 資料 Data", "issue": "個資未遮罩",
         "recommendation": "加入遮罩"}])
    model = ScriptedModel([{"text": f"<review>{json.dumps(review, ensure_ascii=False)}</review>"}])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    doc = {"document": {"format": "docx", "name": "proposal", "source": {"bytes": b"PK"}}}
    out = doc_agent.run("aicoe", {"prompt": "請審查", "mode": "review", "doc_type": "use-case-assessment",
                                  "title": "客服 GenAI"}, rc(kind="aicoe", attachments=[doc]))
    assert out["verdict"] == "revise" and out["checks"]["ok"] and out["checks"]["counts"]["high"] == 1
    assert model.seen[0]["content"][1] == doc  # the uploaded document went to the model
    (path, report), = out["files"].items()
    assert path.startswith("docs/aicoe/reviews/use-case-assessment/")
    assert "| F1 | high | OTHER | 6. 資料 Data | 個資未遮罩 | 加入遮罩 |" in report and "Verdict: **revise**" in report


@pytest.mark.parametrize("payload,code", [
    ({"doc_type": "nope"}, "bad_request"), ({"doc_type": "solution-design", "mode": "edit"}, "bad_request"),
    ({"doc_type": "solution-design", "mode": "review"}, "bad_request")])
def test_payload_validation(payload, code):
    assert doc_agent.validate_payload("coe", payload)["error_code"] == code


def test_coe_entrypoint_multi_turn_session_end_to_end(aws, monkeypatch):
    """Two jobs in one session through the real portal app: turn 2 sees turn 1's draft (DynamoDB)."""
    import coe_agent
    portal = coe_agent.portal
    for k, v in {"session_table": "sessions", "require_alias": True, "async_default": True}.items():
        monkeypatch.setattr(portal.settings, k, v)
    monkeypatch.setattr(portal, "_table", aws["table"])
    monkeypatch.setattr(portal, "_job_store", jobs.MemoryJobStore())
    monkeypatch.setattr(portal, "spawn", lambda fn, *a: fn(*a))
    v1 = "<document>\n" + doc_for("coe", "solution-design") + "\n</document><open_questions>[]</open_questions>"
    v2 = v1.replace("內容 10", "月成本 8000 USD")
    models = [HistoryModel([{"text": v1}]), HistoryModel([{"text": v2}])]
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: models.pop(0))
    c = SimpleNamespace(request_headers={H: "yhtsao"})
    j1 = coe_agent.invoke({"prompt": "draft", "doc_type": "solution-design", "session_id": "pay-2026-001"}, c)["job_id"]
    second_model = models[0]
    j2 = coe_agent.invoke({"prompt": "補成本", "doc_type": "solution-design", "session_id": "pay-2026-001"}, c)["job_id"]
    assert second_model.history_lengths == [3]  # turn 1 (user + assistant) restored from DynamoDB + the new prompt
    out1 = coe_agent.invoke({"action": "status", "job_id": j1}, c)
    out2 = coe_agent.invoke({"action": "status", "job_id": j2}, c)
    assert out1["status"] == out2["status"] == "SUCCEEDED" and "月成本 8000 USD" in out2["document"]
    msgs = [i for i in aws["table"].scan()["Items"] if i["SK"].startswith("MSG#coe#")]
    assert len(msgs) == 4  # user, assistant, user, assistant persisted for the next turn / microVM
