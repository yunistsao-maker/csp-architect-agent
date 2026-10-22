"""Fill mode of the COE / AICOE agents: answer a form from project files, with confidence, and follow-up edits."""
import json
from types import SimpleNamespace

import pytest

import doc_agent
import form_fill
import jobs
from portal.runtime import RunContext
from test_skills import ScriptedModel

H = "x-amzn-bedrock-agentcore-runtime-custom-user-alias"
CONTEXT = "PROJECT FILES (shared by all agents in this project)\n### payments-prd.md (upload)\n雙 AZ，VPN 到地端，TLS 1.2"

FORM = {"name": "golive-check", "title": "上線自檢表", "source": "Working aid; the signed form prevails.", "items": [
    {"id": "C01", "section": "Identity", "q": "Is MFA required outside the company?", "type": "yn",
     "choices": ["Y*", "Y", "N", "NA"], "expect": "Y", "guide": "Y*: capable but not done, give the schedule.",
     "detail": "Reason / Remark"},
    {"id": "C02", "section": "Data", "q": "Is on-prem traffic encrypted (VPN or TLS 1.2+)?", "type": "yn",
     "choices": ["Y", "N"], "expect": "Y", "detail": "Reason / Remark"},
    {"id": "Q8-1", "section": "Sensitive data", "q": "Is employee personal data used for training?", "type": "single",
     "options": ["Yes", "Yes, with contractually explicit consent", "No"], "veto": True, "reject": [0]},
    {"id": "Q9", "section": "Technical", "q": "AI capability", "type": "multi", "options": ["ML", "GenAI", "RAG"],
     "other": True},
    {"id": "Q1", "section": "Overview", "q": "Problem statement", "type": "text"},
]}


def rc(**kw):
    return RunContext(kind=kw.pop("kind", "coe"), alias="yhtsao", job_id="ab" * 16, session_id=None, **kw)


def fill(items, summary="ok", ask=()):
    return {"text": "<fill>" + json.dumps({"summary": summary, "ask": list(ask), "items": items}, ensure_ascii=False)
                    + "</fill>"}


FIRST = [
    {"id": "C01", "answer": "Y*", "note": "AAD 整合排程 2027Q1", "confidence": 70, "basis": "payments-prd.md", "exception": ""},
    {"id": "C02", "answer": "Y", "note": "PRD：VPN 與 TLS 1.2", "confidence": 92, "basis": "payments-prd.md"},
    {"id": "Q8-1", "selected": None, "note": "", "confidence": 20, "basis": ""},
    {"id": "Q9", "selected": [1, 2], "note": "RAG 客服", "confidence": 80, "basis": "payments-prd.md"},
    {"id": "Q1", "text": "退款客服自動化", "confidence": 60, "basis": "payments-prd.md"},
]


def test_blank_form_is_filled_and_only_answers_come_back(monkeypatch):
    model = ScriptedModel([fill(FIRST, "填好 5 題", ["Q8-1：是否用員工個資訓練？"])])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("coe", {"prompt": "開始填寫", "mode": "fill", "form": FORM, "context": CONTEXT,
                                "language": "zh-TW"}, rc())
    assert out["checks"]["ok"] and out["publishable"] and out["checks"]["repair_turns"] == 0
    system = model.system_prompts[0]
    assert "Traditional Chinese" in system and "雙 AZ" in system and '"C01"' in system and "Give EVERY item a row" in system
    assert model.seen[0]["content"][0]["text"] == "開始填寫"  # the form travels in the system prompt, not the history
    items = {i["id"]: i for i in out["form"]["items"]}
    assert items["C01"]["answer"] == "Y*" and items["C01"]["guide"].startswith("Y*:") and items["C01"]["section"] == "Identity"
    assert items["Q9"]["selected"] == [1, 2] and items["Q1"]["text"] == "退款客服自動化"
    assert out["form"]["title"] == "上線自檢表" and out["ask"] == ["Q8-1：是否用員工個資訓練？"]
    assert out["counts"] == {"items": 5, "answered": 4, "veto": 0, "exception": 0, "schedule": 1}
    (path, md), = out["files"].items()
    assert path.startswith("docs/coe/forms/") and path.endswith("-golive-check.md")
    assert "| Identity | C01 | Is MFA required outside the company? | Y* | AAD 整合排程 2027Q1 |  |" in md
    assert "give the schedule" not in md and "70" not in md  # no gray instructions, no confidence


def test_invalid_reply_gets_a_repair_turn(monkeypatch):
    bad = [{"id": "C01", "answer": "Maybe"}, {"id": "Q8-1", "selected": 7}, {"id": "ZZ", "answer": "Y"}]
    model = ScriptedModel([fill(bad), fill(FIRST)])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("coe", {"prompt": "填", "mode": "fill", "form": FORM}, rc())
    feedback = model.seen[1]["content"][0]["text"]
    for problem in ("C01: answer 'Maybe' is not one of Y*, Y, N, NA", "Q8-1: selected must be an index in 0..2",
                    "ids not in the form: ZZ", "items without a row", "C02"):
        assert problem in feedback
    assert out["checks"]["ok"] and out["checks"]["repair_turns"] == 1


def test_follow_up_changes_only_the_items_the_user_named(monkeypatch):
    current = form_fill.merge(form_fill.normalize_form(FORM), {"items": FIRST})[0]
    model = ScriptedModel([fill([{"id": "Q8-1", "selected": 0, "note": "使用者：會用員工資料，沒有同意書", "confidence": 95,
                                  "basis": "user", "exception": ""}], "Q8-1 改為 Yes，觸發否決")])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("aicoe", {"prompt": "Q8-1 會用員工資料，沒有同意書", "mode": "fill", "form": current}, rc(kind="aicoe"))
    assert "already filled in" in model.system_prompts[0] and '"selected":[1,2]' in model.system_prompts[0]
    assert out["updated"] == ["Q8-1"] and out["counts"]["veto"] == 1
    items = {i["id"]: i for i in out["form"]["items"]}
    assert items["Q8-1"]["selected"] == 0 and items["C01"] == current["items"][0]
    assert out["files"] and next(iter(out["files"])).startswith("docs/aicoe/forms/")


def test_nothing_to_change_is_valid(monkeypatch):
    current = form_fill.merge(form_fill.normalize_form(FORM), {"items": FIRST})[0]
    model = ScriptedModel([fill([], "C02 的依據是 PRD 第 3 節。")])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("coe", {"prompt": "C02 的依據是什麼？", "mode": "fill", "form": current}, rc())
    assert out["checks"]["ok"] and out["updated"] == [] and out["summary"] == "C02 的依據是 PRD 第 3 節。"


def test_attached_form_is_read_into_items(monkeypatch):
    rows = [{"id": "1", "section": "安全", "q": "是否使用 SSO？", "type": "yn", "choices": ["Y", "N", "NA"], "expect": "Y",
             "guide": "Y：列出 IdP", "answer": "Y", "note": "Entra ID", "confidence": 88, "basis": "arch.drawio"},
            {"id": "2", "section": "AI", "q": "否決：社會評分？", "type": "yn", "choices": ["Y", "N"], "expect": "N", "veto": True,
             "answer": "Y", "confidence": 90, "basis": "user"}]
    reply = {"text": "<fill>" + json.dumps({"title": "Vendor AI Questionnaire", "summary": "2 items", "ask": [],
                                            "items": rows}, ensure_ascii=False) + "</fill>"}
    model = ScriptedModel([reply])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    doc = {"document": {"format": "xlsx", "name": "vendor form", "source": {"bytes": b"PK"}}}
    out = doc_agent.run("aicoe", {"prompt": "請填這份表單", "mode": "fill", "language": "en"}, rc(kind="aicoe", attachments=[doc]))
    assert "read the form in the attached file" in model.system_prompts[0] and "English" in model.system_prompts[0]
    assert model.seen[0]["content"][1] == doc
    assert out["form"]["name"] == "vendor-ai-questionnaire" and [i["id"] for i in out["form"]["items"]] == ["1", "2"]
    assert out["form"]["items"][1]["veto"] is True and out["counts"]["veto"] == 1 and out["updated"] == ["1", "2"]


def test_unparseable_reply_is_not_publishable(monkeypatch):
    model = ScriptedModel([{"text": "sorry"}, {"text": "<fill>{oops</fill>"}])
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: model)
    out = doc_agent.run("coe", {"prompt": "x", "mode": "fill", "form": FORM}, rc())
    assert not out["publishable"] and out["files"] == {} and out["form"]["items"][0]["id"] == "C01"
    assert "not valid JSON" in out["summary"]


@pytest.mark.parametrize("payload,needle", [
    ({"mode": "fill"}, "attach the form"),
    ({"mode": "fill", "form": {"items": "x"}}, "'items' list"),
    ({"mode": "fill", "form": {"items": [{"id": "a", "q": "?"}, {"id": "a", "q": "!"}]}}, "unique"),
    ({"mode": "fill", "form": {"items": [{"id": "", "q": ""}]}}, "no items"),
    ({"mode": "fill", "form": FORM, "context": ["x"]}, "'context'"),
])
def test_fill_payload_validation(payload, needle):
    problem = doc_agent.validate_payload("coe", payload)
    assert problem["error_code"] == "bad_request" and needle in problem["error"]
    assert doc_agent.validate_payload("coe", {"mode": "fill", "form": FORM}) is None  # no doc_type needed


def test_flags_and_cleaning():
    clean = form_fill.clean_item({"id": "x", "q": "?", "type": "yn", "choices": ["Y", "N", "maybe"], "answer": "NA",
                                  "confidence": "250", "selected": [1]})
    assert clean["choices"] == ["Y", "N"] and clean["answer"] is None and clean["confidence"] == 100
    assert form_fill.clean_item({"id": "y", "q": "?", "type": "single"})["type"] == "text"  # no options
    f = form_fill.normalize_form(FORM)["items"]
    assert [form_fill.flag({**f[0], "answer": a}) for a in ("Y", "Y*", "N", "NA")] == [None, "schedule", "exception", None]
    assert form_fill.flag({**f[2], "selected": 0}) == "veto" and form_fill.flag({**f[2], "selected": 1}) is None
    assert form_fill.language_name("zh-TW", "x") == "Traditional Chinese" and form_fill.language_name(None, "d") == "d"


def test_fill_through_the_portal_entrypoint_with_a_session(aws, monkeypatch):
    """Async job through the real portal app; the second turn sees the first turn's conversation."""
    import aicoe_agent
    portal = aicoe_agent.portal
    for k, v in {"session_table": "sessions", "require_alias": True, "async_default": True}.items():
        monkeypatch.setattr(portal.settings, k, v)
    monkeypatch.setattr(portal, "_table", aws["table"])
    monkeypatch.setattr(portal, "_job_store", jobs.MemoryJobStore())
    monkeypatch.setattr(portal, "spawn", lambda fn, *a: fn(*a))
    models = [ScriptedModel([fill(FIRST)]), ScriptedModel([fill([{"id": "C02", "answer": "N", "note": "還沒有 VPN"}])])]
    monkeypatch.setattr(doc_agent, "BedrockModel", lambda **kw: models.pop(0))
    c = SimpleNamespace(request_headers={H: "yhtsao"})
    base = {"mode": "fill", "session_id": "pay-2026-001-aicoe", "language": "zh-TW", "context": CONTEXT}
    j1 = aicoe_agent.invoke({**base, "prompt": "開始填寫", "form": FORM}, c)["job_id"]
    r1 = aicoe_agent.invoke({"action": "status", "job_id": j1}, c)
    assert r1["status"] == "SUCCEEDED" and r1["publishable"]  # a finished job's status carries the result fields
    second = models[0]
    j2 = aicoe_agent.invoke({**base, "prompt": "C02 其實還沒有 VPN", "form": r1["form"]}, c)["job_id"]
    r2 = aicoe_agent.invoke({"action": "status", "job_id": j2}, c)
    assert r2["status"] == "SUCCEEDED" and r2["updated"] == ["C02"] and r2["counts"]["exception"] == 1
    assert len(second.seen) == 1 and "上線自檢表" in second.system_prompts[0]
