"""Diagram Generator agent from aws-samples/bedrock-engineer, ported to Strands + Amazon Bedrock AgentCore Runtime.

Payload:
    {
      "prompt": "Serverless web app with API Gateway, Lambda and DynamoDB",   # required
      "mode": "aws" | "azure" | "gcp" | "software-architecture" | "business-process",  # default "aws"
      "language": "English",                                                 # optional, default: match the user
      "enable_search": false                                                 # optional, needs TAVILY_API_KEY
    }

Response:
    {"xml": "<mxfile>...</mxfile>", "explanation": "...markdown...", "mode": "aws",
     "validation": {"ok": true, "errors": [], "icon_errors": [], "auto_fixed": [], "repair_turns": 0, ...}}
    or, when the model runs out of output tokens:
    {"error": "...", "error_code": "max_tokens", "mode": "aws"}

Every diagram is validated (well-formed XML, cell references, icon names that really exist in draw.io).
Known-wrong icon names are auto-corrected; anything else triggers up to MAX_REPAIR_TURNS repair turns.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.config import Config
from strands import Agent, tool
from strands.models import BedrockModel
from strands.types.exceptions import MaxTokensReachedException

from drawio_validator import auto_fix_icons, repair_prompt, validate

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5-5")
REGION = os.environ.get("AWS_REGION", "us-east-1")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
# Large diagrams (e.g. full MLOps platforms) plus adaptive thinking exceed 32k output tokens.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "64000"))
# Extra model turns allowed to repair a diagram that fails validation after auto-fix (0 disables).
MAX_REPAIR_TURNS = int(os.environ.get("MAX_REPAIR_TURNS", "1"))

PROMPT_DIR = Path(__file__).parent / "prompts"
MODES = {
    "aws": "aws.md",
    "azure": "azure.md",
    "gcp": "gcp.md",
    "software-architecture": "software_architecture.md",
    "business-process": "business_process.md",
}
LANG_LINE = "Respond in the following languages included in the user request."
SEARCH_LINE = (
    "* If the user's request requires specific information, use the tavilySearch tool "
    "to gather up-to-date information before creating the diagram."
)

app = BedrockAgentCoreApp()

# One agent per (session, mode) so follow-up messages can refine the previous diagram,
# matching the original app's enableHistory behaviour.
_agents: dict[tuple, Agent] = {}


@tool
def tavilySearch(query: str) -> str:
    """Search the web for up-to-date information (AWS service features, limits, best practices).

    Args:
        query: The search query.
    """
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({"query": query, "max_results": 5, "include_answer": True}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TAVILY_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    results = [{"title": r.get("title"), "url": r.get("url"), "content": r.get("content")} for r in data.get("results", [])]
    return json.dumps({"answer": data.get("answer"), "results": results}, ensure_ascii=False)


def build_system_prompt(mode: str, language: str | None, enable_search: bool) -> str:
    prompt = (PROMPT_DIR / MODES[mode]).read_text(encoding="utf-8")
    if language:
        prompt = prompt.replace(LANG_LINE, f"Respond in the following languages: {language}.")
    if not enable_search:
        prompt = prompt.replace(SEARCH_LINE, "")
    return prompt


# Require <diagram> right after the start tag so a prose mention of "<mxfile>" is never taken as the start.
MXFILE_RE = re.compile(r"<mxfile\b[^>]*>\s*<diagram\b[\s\S]*?</mxfile>", re.I)
GRAPHMODEL_RE = re.compile(r"<mxGraphModel\b[\s\S]*?</mxGraphModel>", re.I)
# Any code fence (```xml, ```drawio, ```html, bare ```) that wraps a diagram.
FENCED_DIAGRAM_RE = re.compile(r"```[\w-]*\s*(?:<mxfile\b[^>]*>\s*<diagram\b[\s\S]*?</mxfile>|<mxGraphModel\b[\s\S]*?</mxGraphModel>)\s*```", re.I)


def extract_drawio_xml(content: str) -> str:
    """Port of xmlParser.extractDrawioXml from bedrock-engineer.

    Matches non-greedily and keeps the LAST complete <mxfile>, so a reply that shows a "before" and an
    "after" diagram yields the final one instead of one invalid document spanning both.
    """
    files = MXFILE_RE.findall(content)
    if files:
        return files[-1]
    models = GRAPHMODEL_RE.findall(content)
    if models:
        ts = datetime.now(timezone.utc).isoformat()
        return f'<mxfile host="Electron" modified="{ts}" type="device">\n  <diagram>\n    {models[-1]}\n  </diagram>\n</mxfile>'
    return ""


def extract_diagram_content(content: str) -> dict:
    """Port of xmlParser.extractDiagramContent: split XML from the explanation text."""
    xml = extract_drawio_xml(content)
    if not xml:
        return {"xml": "", "explanation": content.strip()}
    explanation = FENCED_DIAGRAM_RE.sub("", content)
    explanation = MXFILE_RE.sub("", explanation)
    explanation = GRAPHMODEL_RE.sub("", explanation)
    return {"xml": xml, "explanation": explanation.strip()}


def get_agent(session_id: str | None, mode: str, language: str | None, enable_search: bool) -> Agent:
    key = (session_id, mode, language, enable_search)
    if session_id and key in _agents:
        return _agents[key]
    agent = Agent(
        # Large diagrams can stall the stream past Strands' 120s default read timeout.
        model=BedrockModel(
            model_id=MODEL_ID,
            region_name=REGION,
            max_tokens=MAX_TOKENS,
            boto_client_config=Config(read_timeout=900, retries={"max_attempts": 3, "mode": "adaptive"}),
        ),
        system_prompt=build_system_prompt(mode, language, enable_search),
        tools=[tavilySearch] if enable_search else [],
        callback_handler=None,
    )
    if session_id:
        _agents[key] = agent
    return agent


class OutputTruncated(Exception):
    """The model hit max_tokens before finishing the diagram."""


def run_turn(agent: Agent, prompt: str) -> str:
    """One model turn. On max_tokens, roll the conversation back so a reused session stays consistent."""
    checkpoint = len(agent.messages)
    try:
        result = agent(prompt)
    except MaxTokensReachedException as e:
        del agent.messages[checkpoint:]
        raise OutputTruncated(str(e)) from e
    return "".join(c.get("text", "") for c in result.message.get("content", []))


def _problems(report: dict) -> int:
    # A document that does not parse is worse than any number of fixable issues.
    if not report["pages"]:
        return 10**6
    return len(report["errors"]) + sum(e["count"] for e in report["icon_errors"])


def generate_validated(agent: Agent, prompt: str) -> dict:
    """Generate a diagram, auto-fix icon names, and spend up to MAX_REPAIR_TURNS turns repairing the rest."""
    out = extract_diagram_content(run_turn(agent, prompt))
    if not out["xml"]:  # e.g. the model asked a clarifying question instead of drawing
        return {**out, "validation": None}
    xml, fixed = auto_fix_icons(out["xml"])
    report = validate(xml)
    turns, note = 0, None
    while not report["ok"] and turns < MAX_REPAIR_TURNS:
        turns += 1
        try:
            retry = extract_diagram_content(run_turn(agent, repair_prompt(report)))
        except OutputTruncated:
            note = "repair turn hit max_tokens; returning the unrepaired diagram"
            break
        if not retry["xml"]:
            note = "repair turn returned no diagram; returning the unrepaired diagram"
            break
        new_xml, more = auto_fix_icons(retry["xml"])
        new_report = validate(new_xml)
        if _problems(new_report) > _problems(report):
            note = "repair turn made the diagram worse; returning the previous version"
            continue
        xml, report = new_xml, new_report
        fixed += more
        if retry["explanation"] and len(retry["explanation"]) > 200:
            out["explanation"] = retry["explanation"]
    validation = {**report, "auto_fixed": fixed, "repair_turns": turns}
    if note:
        validation["note"] = note
    return {"xml": xml, "explanation": out["explanation"], "validation": validation}


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    prompt = payload.get("prompt")
    if not prompt:
        return {"error": "Missing 'prompt' in payload"}
    mode = payload.get("mode", "aws")
    if mode not in MODES:
        return {"error": f"Invalid mode '{mode}'. Use one of: {', '.join(MODES)}"}
    enable_search = bool(payload.get("enable_search")) and bool(TAVILY_API_KEY)

    session_id = getattr(context, "session_id", None) if context else None
    agent = get_agent(session_id, mode, payload.get("language"), enable_search)
    try:
        result = generate_validated(agent, prompt)
    except OutputTruncated:
        return {
            "error": (
                f"The diagram exceeded the model's output limit (MAX_TOKENS={MAX_TOKENS}) and was not returned. "
                "Ask for fewer components, or for one page per concern (e.g. network / workloads / MLOps), "
                "or raise MAX_TOKENS if the model allows it."
            ),
            "error_code": "max_tokens",
            "mode": mode,
        }
    return {**result, "mode": mode}


if __name__ == "__main__":
    app.run()
