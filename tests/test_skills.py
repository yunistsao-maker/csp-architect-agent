"""Skills mode, end to end through the real Strands Agent + AgentSkills plugin, with a scripted model (no Bedrock)."""
import importlib
import json
import re
from pathlib import Path

import pytest
from strands.models import Model
from strands.vended_plugins.skills import Skill

from drawio_validator import _icons

SKILLS = Path(__file__).resolve().parents[1] / "skills"
GOOD = ('<mxfile><diagram id="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" value="AKS" style="image;html=1;image=img/lib/azure2/containers/Kubernetes_Services.svg;" '
        'vertex="1" parent="1"><mxGeometry width="50" height="50" as="geometry"/></mxCell></root></mxGraphModel>'
        '</diagram></mxfile>')


class ScriptedModel(Model):
    """Plays back a fixed list of assistant turns as Converse stream events and records what it was sent."""

    def __init__(self, turns):
        self.turns, self.system_prompts, self.tool_specs, self.seen = list(turns), [], [], []

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *a, **kw):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.system_prompts.append(system_prompt or "".join(
            b.get("text", "") for b in (kwargs.get("system_prompt_content") or [])))
        self.tool_specs.append([t["name"] for t in tool_specs or []])
        self.seen.append(messages[-1])
        turn = self.turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        if "tool" in turn:
            name, args = turn["tool"]
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{len(self.turns)}", "name": name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": turn["text"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100},
                            "metrics": {"latencyMs": 1}}}


@pytest.fixture()
def agent_mod(monkeypatch):
    mod = importlib.reload(importlib.import_module("agent"))
    mod._agents.clear()
    return mod


def run(agent_mod, monkeypatch, turns, payload):
    model = ScriptedModel(turns)
    monkeypatch.setattr(agent_mod, "BedrockModel", lambda **kw: model)
    return agent_mod.invoke(payload), model


def tool_results(model):
    out = []
    for msg in model.seen:
        for block in msg.get("content", []):
            if "toolResult" in block:
                out.append("".join(c.get("text", "") for c in block["toolResult"]["content"]))
    return out


def test_skills_mode_end_to_end(agent_mod, monkeypatch):
    turns = [
        {"tool": ("skills", {"skill_name": "azure-architecture-diagram"})},
        {"tool": ("read_skill_file", {"skill_name": "azure-architecture-diagram", "path": "references/icons/containers.md"})},
        {"text": f"Create a diagram about X.\n```xml\n{GOOD}\n```\n## Architecture Overview\nok"},
    ]
    out, model = run(agent_mod, monkeypatch, turns, {"prompt": "AKS", "mode": "azure", "skills": True})

    first_prompt = model.system_prompts[0]
    assert "<available_skills>" in first_prompt and "azure-architecture-diagram" in first_prompt
    assert "strict-networking-review" in first_prompt and "<runtime_overrides>" in first_prompt
    assert {"skills", "read_skill_file"} <= set(model.tool_specs[0])
    skill_body, catalog = tool_results(model)
    assert "Self-check before returning" in skill_body and "references/icons/containers.md" in skill_body
    assert "containers/Kubernetes_Services.svg" in catalog

    assert out["skills"] is True and out["validation"]["ok"]
    u = out["usage"]
    assert u["model_calls"] == 3 and u["tool_calls"] == 2
    assert u["skills_activated"] == ["azure-architecture-diagram"]
    assert u["files_read"] == ["azure-architecture-diagram/references/icons/containers.md"]
    assert u["input_tokens"] == 3000 and u["output_tokens"] == 300


def test_prompt_mode_has_no_skills(agent_mod, monkeypatch):
    out, model = run(agent_mod, monkeypatch, [{"text": f"```xml\n{GOOD}\n```"}], {"prompt": "AKS", "mode": "azure"})
    assert out["skills"] is False and "<available_skills>" not in model.system_prompts[0]
    assert model.tool_specs[0] == [] and out["usage"]["model_calls"] == 1


def test_skills_flag_ignored_for_non_csp_modes(agent_mod, monkeypatch):
    out, model = run(agent_mod, monkeypatch, [{"text": f"```xml\n{GOOD}\n```"}],
                     {"prompt": "x", "mode": "business-process", "skills": True})
    assert out["skills"] is False and "<available_skills>" not in model.system_prompts[0]


def test_use_skills_env_default(agent_mod, monkeypatch):
    monkeypatch.setattr(agent_mod, "USE_SKILLS", True)
    out, _ = run(agent_mod, monkeypatch, [{"text": f"```xml\n{GOOD}\n```"}], {"prompt": "x", "mode": "gcp"})
    assert out["skills"] is True


# ---------- read_skill_file ----------
@pytest.mark.parametrize("skill,path,expect", [
    ("aws-architecture-diagram", "references/xml-rules.md", "mxgraph.aws4"),
    ("aws-architecture-diagram", "../../agent.py", "not found"),
    ("../prompts", "aws.md", "Unknown skill"),
    ("aws-architecture-diagram", "/etc/passwd", "not found"),
    ("aws-architecture-diagram", "references/example-agentcore.drawio", "not found"),
    ("nope", "SKILL.md", "Unknown skill"),
])
def test_read_skill_file_is_confined(agent_mod, skill, path, expect):
    assert expect in agent_mod.read_skill_file._tool_func(skill_name=skill, path=path)


# ---------- skill content ----------
@pytest.mark.parametrize("skill_dir", sorted(p for p in SKILLS.iterdir() if p.is_dir()), ids=lambda p: p.name)
def test_skill_frontmatter_is_valid(skill_dir):
    skill = Skill.from_file(skill_dir / "SKILL.md", strict=True)
    assert skill.name == skill_dir.name and len(skill.description) > 50


def test_every_icon_named_in_skills_exists():
    icons = _icons()
    bad = []
    for f in SKILLS.rglob("*.md"):
        if f.name == "VENDORED.md":  # documents the names that were fixed
            continue
        text = f.read_text()
        for n in re.findall(r"mxgraph\.aws4\.([A-Za-z0-9_]+)", text):
            if n != "SHAPE_NAME" and n not in icons["aws4"] and f"mxgraph.aws4.{n}" not in icons["shapes"]:
                bad.append((f.name, n))
        for n in re.findall(r"img/lib/azure2/([A-Za-z0-9_]+/[A-Za-z0-9_\-.()]+\.svg)", text):
            if n not in icons["azure2"]:
                bad.append((f.name, n))
        for n in re.findall(r"prIcon=([a-z0-9_]+)", text):
            if n not in icons["gcp2"]:
                bad.append((f.name, n))
    assert not bad, bad


def test_gcp_skill_common_names_exist():
    text = (SKILLS / "gcp-architecture-diagram/SKILL.md").read_text()
    block = text.split("Common GCP2 names:")[1].split("Names like")[0]
    names = re.findall(r"`([a-z0-9_]+)`", block)
    assert names and all(n in _icons()["gcp2"] for n in names)


def test_skill_template_examples_validate():
    from drawio_validator import validate
    for f in SKILLS.rglob("xml-template.md"):
        for xml in re.findall(r"<mxfile\b[^>]*>\s*<diagram[\s\S]*?</mxfile>", f.read_text()):
            assert validate(xml)["ok"], f


def test_icon_catalogs_are_up_to_date(tmp_path):
    import subprocess
    import sys
    before = {p: p.read_text() for p in SKILLS.rglob("icons/*.md")}
    before[SKILLS / "gcp-architecture-diagram/references/gcp2-icons.md"] = \
        (SKILLS / "gcp-architecture-diagram/references/gcp2-icons.md").read_text()
    subprocess.run([sys.executable, str(SKILLS.parent / "scripts/build_icon_catalogs.py")], check=True, capture_output=True)
    for p, text in before.items():
        assert p.read_text() == text, f"{p} is stale; run scripts/build_icon_catalogs.py"
