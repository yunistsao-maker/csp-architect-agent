"""Every icon name a prompt tells the model to use must exist in draw.io, and every example must validate."""
import re
from pathlib import Path

import pytest

import agent
from drawio_validator import _icons, validate

PROMPTS = Path(agent.__file__).parent / "prompts"


def section(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", text)
    assert m, f"<{tag}> section missing"
    return m.group(1)


def listed_names(block: str) -> list[str]:
    names = []
    for line in block.splitlines():
        if line.startswith("- ") and ":" in line:
            body = re.sub(r"\([^)]*\)", "", line.split(":", 1)[1])
            names += [n.strip() for n in body.split(",") if n.strip()]
    return names


def test_aws_icons_exist():
    text = (PROMPTS / "aws.md").read_text()
    block = section(text, "aws_icons")
    names = listed_names(block) + re.findall(r"\b(group_[a-z0-9_]+)", block)
    names += re.findall(r"shape=mxgraph\.aws4\.([a-z0-9_]+)", block)
    bad = [n for n in names if n not in _icons()["aws4"] and f"mxgraph.aws4.{n}" not in _icons()["shapes"]]
    assert not bad, f"aws.md lists icons draw.io does not have: {bad}"
    assert len(names) > 60


def test_azure_icons_exist():
    names = listed_names(section((PROMPTS / "azure.md").read_text(), "azure_icons"))
    bad = [n for n in names if n not in _icons()["azure2"]]
    assert not bad, f"azure.md lists icons draw.io does not have: {bad}"
    assert len(names) > 40


def test_gcp_icons_exist():
    names = listed_names(section((PROMPTS / "gcp.md").read_text(), "gcp_icons"))
    bad = [n for n in names if n not in _icons()["gcp2"]]
    assert not bad, f"gcp.md lists icons draw.io does not have: {bad}"
    assert len(names) > 40


@pytest.mark.parametrize("mode", list(agent.MODES))
def test_example_xml_in_prompt_is_valid(mode):
    text = (PROMPTS / agent.MODES[mode]).read_text()
    for xml in re.findall(r"<mxfile\b[^>]*>\s*<diagram[\s\S]*?</mxfile>", text):
        report = validate(xml)
        assert report["ok"], report


@pytest.mark.parametrize("mode", ["aws", "azure", "gcp"])
def test_csp_prompts_carry_conventions(mode):
    text = (PROMPTS / agent.MODES[mode]).read_text()
    assert "<diagram_conventions>" in text and "orthogonalEdgeStyle" in text
    assert "## Security Considerations" in text and "## Data Flow" in text
