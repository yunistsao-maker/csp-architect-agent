"""Offline tests for agent.py: the Strands Agent is stubbed, no AWS access needed."""
import importlib
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from strands.types.exceptions import MaxTokensReachedException

VALID = ('<mxfile host="x"><diagram id="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
         '<mxCell id="a" value="ECR" style="shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{icon};" vertex="1" parent="1">'
         '<mxGeometry width="78" height="78" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>')
GOOD = VALID.format(icon="ecr")
ALIASED = VALID.format(icon="elastic_container_registry")   # auto-fixable
UNKNOWN = VALID.format(icon="made_up_service")             # needs a repair turn


def reply(xml: str, expl: str = "## Architecture Overview\nok") -> str:
    return f"Create a diagram about X.\n\n```xml\n{xml}\n```\n\n{expl}"


class StubAgent:
    """Mimics strands.Agent: replies come from a queue; an Exception in the queue is raised."""
    queue: list = []
    instances: list = []

    def __init__(self, **kw):
        self.kw, self.calls, self.messages = kw, [], []
        StubAgent.instances.append(self)

    def __call__(self, prompt):
        self.calls.append(prompt)
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        item = StubAgent.queue.pop(0)
        if isinstance(item, Exception):
            self.messages.append({"role": "assistant", "content": [{"text": "<mxfile>...truncated"}]})
            raise item
        self.messages.append({"role": "assistant", "content": [{"text": item}]})
        return SimpleNamespace(message={"content": [{"text": item}]})


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("MAX_REPAIR_TURNS", raising=False)
    mod = importlib.reload(importlib.import_module("agent"))
    monkeypatch.setattr(mod, "Agent", StubAgent)
    monkeypatch.setattr(mod, "BedrockModel", lambda **kw: SimpleNamespace(**kw))
    StubAgent.instances.clear()
    StubAgent.queue = []
    mod._agents.clear()
    return mod


# ---------- payload validation ----------
def test_missing_prompt(agent):
    assert agent.invoke({}) == {"error": "Missing 'prompt' in payload"}


def test_invalid_mode(agent):
    assert "Invalid mode" in agent.invoke({"prompt": "x", "mode": "oracle"})["error"]


@pytest.mark.parametrize("mode", ["aws", "azure", "gcp", "software-architecture", "business-process"])
def test_all_modes_accepted(agent, mode):
    StubAgent.queue = [reply(GOOD)]
    out = agent.invoke({"prompt": "x", "mode": mode})
    assert out["mode"] == mode and out["xml"] == GOOD


# ---------- system prompt building ----------
@pytest.mark.parametrize("mode", ["aws", "azure", "gcp", "software-architecture", "business-process"])
def test_language_and_search_lines_present(agent, mode):
    raw = (agent.PROMPT_DIR / agent.MODES[mode]).read_text()
    assert agent.LANG_LINE in raw, f"{mode}: 'language' payload would be silently ignored"
    assert agent.SEARCH_LINE in raw, f"{mode}: model would be told to call a tool it doesn't have"
    sp = agent.build_system_prompt(mode, "Traditional Chinese", False)
    assert "Respond in the following languages: Traditional Chinese." in sp and "tavilySearch" not in sp


def test_search_requires_key(agent):
    StubAgent.queue = [reply(GOOD)]
    agent.invoke({"prompt": "x", "enable_search": True})
    assert StubAgent.instances[-1].kw["tools"] == []


# ---------- validation, auto-fix and repair ----------
def test_valid_diagram_has_clean_validation(agent):
    StubAgent.queue = [reply(GOOD)]
    out = agent.invoke({"prompt": "x"})
    v = out["validation"]
    assert v["ok"] and v["repair_turns"] == 0 and v["auto_fixed"] == [] and v["vertices"] == 1


def test_known_wrong_icon_is_auto_fixed_without_extra_turn(agent):
    StubAgent.queue = [reply(ALIASED)]
    out = agent.invoke({"prompt": "x"})
    assert "resIcon=mxgraph.aws4.ecr;" in out["xml"]
    assert out["validation"]["ok"] and out["validation"]["repair_turns"] == 0
    assert out["validation"]["auto_fixed"][0]["from"] == "elastic_container_registry"
    assert len(StubAgent.instances[-1].calls) == 1


def test_unknown_icon_triggers_one_repair_turn(agent):
    StubAgent.queue = [reply(UNKNOWN), reply(GOOD, "## Architecture Overview\n" + "fixed " * 60)]
    out = agent.invoke({"prompt": "x"})
    calls = StubAgent.instances[-1].calls
    assert len(calls) == 2 and "made_up_service" in calls[1]
    assert out["xml"] == GOOD and out["validation"]["ok"] and out["validation"]["repair_turns"] == 1
    assert "fixed fixed" in out["explanation"]


def test_repair_turns_are_capped(agent, monkeypatch):
    monkeypatch.setattr(agent, "MAX_REPAIR_TURNS", 1)
    StubAgent.queue = [reply(UNKNOWN), reply(UNKNOWN)]
    out = agent.invoke({"prompt": "x"})
    assert not out["validation"]["ok"] and out["validation"]["repair_turns"] == 1
    assert out["validation"]["icon_errors"][0]["name"] == "made_up_service"


def test_repair_can_be_disabled(agent, monkeypatch):
    monkeypatch.setattr(agent, "MAX_REPAIR_TURNS", 0)
    StubAgent.queue = [reply(UNKNOWN)]
    out = agent.invoke({"prompt": "x"})
    assert out["validation"]["repair_turns"] == 0 and len(StubAgent.instances[-1].calls) == 1


def test_malformed_xml_triggers_repair(agent):
    bad = GOOD.replace('<mxCell id="1" parent="0"/>', '<mxCell id="1" parent="0">')
    StubAgent.queue = [reply(bad), reply(GOOD)]
    out = agent.invoke({"prompt": "x"})
    assert out["validation"]["ok"] and out["validation"]["repair_turns"] == 1
    ET.fromstring(out["xml"])


def test_no_diagram_means_no_validation(agent):
    StubAgent.queue = ["Which region should the DR site use?"]
    out = agent.invoke({"prompt": "x"})
    assert out["xml"] == "" and out["validation"] is None and len(StubAgent.instances[-1].calls) == 1


# ---------- max_tokens ----------
def test_max_tokens_returns_structured_error(agent):
    StubAgent.queue = [MaxTokensReachedException("max_tokens")]
    out = agent.invoke({"prompt": "x", "mode": "gcp"})
    assert out["error_code"] == "max_tokens" and out["mode"] == "gcp" and "one page per concern" in out["error"]


def test_max_tokens_rolls_back_session_history(agent):
    ctx = SimpleNamespace(session_id="s1")
    StubAgent.queue = [reply(GOOD), MaxTokensReachedException("max_tokens")]
    agent.invoke({"prompt": "draw"}, ctx)
    out = agent.invoke({"prompt": "add everything"}, ctx)
    assert out["error_code"] == "max_tokens"
    assert len(StubAgent.instances[0].messages) == 2  # truncated turn removed, first turn kept


def test_max_tokens_during_repair_keeps_first_diagram(agent):
    StubAgent.queue = [reply(UNKNOWN), MaxTokensReachedException("max_tokens")]
    out = agent.invoke({"prompt": "x"})
    assert out["xml"] == UNKNOWN and "max_tokens" in out["validation"]["note"]


# ---------- extraction ----------
def test_extract_fenced_xml(agent):
    out = agent.extract_diagram_content(reply(GOOD, "## Architecture Overview\nText"))
    assert out["xml"] == GOOD and "```" not in out["explanation"] and out["explanation"].startswith("Create a diagram")


def test_extract_bare_graphmodel_is_wrapped(agent):
    gm = '<mxGraphModel><root><mxCell id="0"/></root></mxGraphModel>'
    out = agent.extract_diagram_content(f"```\n{gm}\n```\nexpl")
    ET.fromstring(out["xml"])
    assert out["xml"].startswith("<mxfile") and out["explanation"] == "expl"


def test_two_diagrams_keep_the_last_one(agent):
    second = GOOD.replace('id="d"', 'id="after"')
    out = agent.extract_diagram_content(f"Before:\n```xml\n{GOOD}\n```\nAfter:\n```xml\n{second}\n```\nDone")
    ET.fromstring(out["xml"])
    assert 'id="after"' in out["xml"] and "<mxfile" not in out["explanation"]


@pytest.mark.parametrize("label", ["drawio", "html", "", "xml"])
def test_any_fence_label_is_removed(agent, label):
    out = agent.extract_diagram_content(f"```{label}\n{GOOD}\n```\n## Overview")
    assert out["xml"] == GOOD and out["explanation"] == "## Overview"


def test_prose_mention_of_mxfile_is_ignored(agent):
    out = agent.extract_diagram_content(f"The <mxfile> holds two pages.\n```xml\n{GOOD}\n```")
    assert out["xml"] == GOOD


def test_no_xml_returns_explanation_only(agent):
    assert agent.extract_diagram_content("Sorry.") == {"xml": "", "explanation": "Sorry."}


# ---------- sessions ----------
def test_session_reuses_agent(agent):
    StubAgent.queue = [reply(GOOD), reply(GOOD)]
    ctx = SimpleNamespace(session_id="s1")
    agent.invoke({"prompt": "a"}, ctx)
    agent.invoke({"prompt": "b"}, ctx)
    assert len(StubAgent.instances) == 1 and StubAgent.instances[0].calls == ["a", "b"]


def test_no_session_no_cache(agent):
    StubAgent.queue = [reply(GOOD), reply(GOOD)]
    agent.invoke({"prompt": "a"})
    agent.invoke({"prompt": "b"})
    assert len(StubAgent.instances) == 2 and agent._agents == {}


def test_worse_repair_is_discarded(agent):
    broken = GOOD.replace('<mxCell id="1" parent="0"/>', '<mxCell id="1" parent="0">')
    StubAgent.queue = [reply(UNKNOWN), reply(broken)]
    out = agent.invoke({"prompt": "x"})
    assert out["xml"] == UNKNOWN and "worse" in out["validation"]["note"]
