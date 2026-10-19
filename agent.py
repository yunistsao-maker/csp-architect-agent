"""Diagram Generator agent from aws-samples/bedrock-engineer, ported to Strands + Amazon Bedrock AgentCore Runtime.

Payload:
    {
      "prompt": "Serverless web app with API Gateway, Lambda and DynamoDB",   # required
      "mode": "aws" | "azure" | "gcp" | "software-architecture" | "business-process",  # default "aws"
      "language": "English",                                                 # optional, default: match the user
      "enable_search": false,                                                # optional, needs TAVILY_API_KEY
      "skills": false                       # optional (aws/azure/gcp): load Agent Skills from skills/ instead of the
    }                                       # per-mode prompt; default from USE_SKILLS

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
from strands import Agent, AgentSkills, tool
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
# Default for the "skills" payload flag: load Agent Skills on demand instead of the per-mode system prompt.
USE_SKILLS = os.environ.get("USE_SKILLS", "false").lower() in ("1", "true", "yes")
MAX_SKILL_FILE_CHARS = 120_000

PROMPT_DIR = Path(__file__).parent / "prompts"
MODES = {
    "aws": "aws.md",
    "azure": "azure.md",
    "gcp": "gcp.md",
    "software-architecture": "software_architecture.md",
    "business-process": "business_process.md",
}
SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_PROMPT = "skills_runtime.md"
# Skills mode is available for the CSP modes; the other modes always use their prompt.
SKILL_FOR_MODE = {
    "aws": "aws-architecture-diagram",
    "azure": "azure-architecture-diagram",
    "gcp": "gcp-architecture-diagram",
}
LANG_LINE = "Respond in the following languages included in the user request."
SEARCH_LINE = (
    "* If the user's request requires specific information, use the tavilySearch tool "
    "to gather up-to-date information before creating the diagram."
)

app = BedrockAgentCoreApp()

# One agent per (session, mode, ...) so follow-up messages can refine the previous diagram,
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


@tool
def read_skill_file(skill_name: str, path: str) -> str:
    """Read one reference file of a skill, e.g. skill_name="aws-architecture-diagram", path="references/xml-rules.md".

    Args:
        skill_name: Name of an activated skill.
        path: File path relative to the skill directory, as listed when the skill was activated.
    """
    root = SKILLS_DIR.resolve()
    base = (root / skill_name).resolve()
    if base.parent != root or not base.is_dir():
        return f"Unknown skill '{skill_name}'. Available: {', '.join(sorted(d.name for d in root.iterdir() if d.is_dir()))}"
    target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        return f"File '{path}' not found in skill '{skill_name}'."
    if target.suffix == ".drawio":
        return "Example .drawio files are not available here; follow the skill's reference files instead."
    text = target.read_text(encoding="utf-8")
    if len(text) > MAX_SKILL_FILE_CHARS:
        text = text[:MAX_SKILL_FILE_CHARS] + "\n\n[truncated]"
    return text


def build_system_prompt(mode: str, language: str | None, enable_search: bool, use_skills: bool = False) -> str:
    if use_skills:
        prompt = (PROMPT_DIR / SKILLS_PROMPT).read_text(encoding="utf-8")
        prompt += f"\n\nTarget cloud for this conversation: {mode}. Activate the {SKILL_FOR_MODE[mode]} skill first.\n"
    else:
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


def get_agent(session_id: str | None, mode: str, language: str | None, enable_search: bool,
              use_skills: bool = False) -> Agent:
    key = (session_id, mode, language, enable_search, use_skills)
    if session_id and key in _agents:
        return _agents[key]
    tools = [tavilySearch] if enable_search else []
    plugins = []
    if use_skills:
        tools.append(read_skill_file)
        plugins.append(AgentSkills(skills=str(SKILLS_DIR)))
    agent = Agent(
        # Large diagrams can stall the stream past Strands' 120s default read timeout.
        model=BedrockModel(
            model_id=MODEL_ID,
            region_name=REGION,
            max_tokens=MAX_TOKENS,
            boto_client_config=Config(read_timeout=900, retries={"max_attempts": 3, "mode": "adaptive"}),
        ),
        system_prompt=build_system_prompt(mode, language, enable_search, use_skills),
        tools=tools,
        plugins=plugins,
        callback_handler=None,
    )
    if session_id:
        _agents[key] = agent
    return agent


class OutputTruncated(Exception):
    """The model hit max_tokens before finishing the diagram."""


def new_usage() -> dict:
    return {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "tool_calls": 0,
            "skills_activated": [], "files_read": []}


def _token_totals(agent: Agent) -> tuple[int, int]:
    usage = getattr(getattr(agent, "event_loop_metrics", None), "accumulated_usage", None) or {}
    return usage.get("inputTokens", 0), usage.get("outputTokens", 0)


def run_turn(agent: Agent, prompt: str, usage: dict | None = None) -> str:
    """One user turn. On max_tokens, roll the conversation back so a reused session stays consistent.

    `usage` (optional) accumulates tokens, model calls and tool calls for this request.
    """
    checkpoint = len(agent.messages)
    tokens_before = _token_totals(agent)
    try:
        result = agent(prompt)
    except MaxTokensReachedException as e:
        del agent.messages[checkpoint:]
        raise OutputTruncated(str(e)) from e
    if usage is not None:
        tokens_after = _token_totals(agent)
        usage["input_tokens"] += tokens_after[0] - tokens_before[0]
        usage["output_tokens"] += tokens_after[1] - tokens_before[1]
        for msg in agent.messages[checkpoint:]:
            if msg.get("role") == "assistant":
                usage["model_calls"] += 1
            for block in msg.get("content", []):
                call = block.get("toolUse") if isinstance(block, dict) else None
                if not call:
                    continue
                usage["tool_calls"] += 1
                args = call.get("input") or {}
                if call.get("name") == "skills" and args.get("skill_name"):
                    usage["skills_activated"].append(args["skill_name"])
                elif call.get("name") == "read_skill_file":
                    usage["files_read"].append(f"{args.get('skill_name')}/{args.get('path')}")
    return "".join(c.get("text", "") for c in result.message.get("content", []))


def _problems(report: dict) -> int:
    # A document that does not parse is worse than any number of fixable issues.
    if not report["pages"]:
        return 10**6
    return len(report["errors"]) + sum(e["count"] for e in report["icon_errors"])


def generate_validated(agent: Agent, prompt: str, usage: dict | None = None) -> dict:
    """Generate a diagram, auto-fix icon names, and spend up to MAX_REPAIR_TURNS turns repairing the rest."""
    usage = usage if usage is not None else new_usage()
    out = extract_diagram_content(run_turn(agent, prompt, usage))
    if not out["xml"]:  # e.g. the model asked a clarifying question instead of drawing
        return {**out, "validation": None, "usage": usage}
    xml, fixed = auto_fix_icons(out["xml"])
    report = validate(xml)
    turns, note = 0, None
    while not report["ok"] and turns < MAX_REPAIR_TURNS:
        turns += 1
        try:
            retry = extract_diagram_content(run_turn(agent, repair_prompt(report), usage))
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
    return {"xml": xml, "explanation": out["explanation"], "validation": validation, "usage": usage}


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    prompt = payload.get("prompt")
    if not prompt:
        return {"error": "Missing 'prompt' in payload"}
    mode = payload.get("mode", "aws")
    if mode not in MODES:
        return {"error": f"Invalid mode '{mode}'. Use one of: {', '.join(MODES)}"}
    enable_search = bool(payload.get("enable_search")) and bool(TAVILY_API_KEY)

    use_skills = bool(payload.get("skills", USE_SKILLS)) and mode in SKILL_FOR_MODE

    session_id = getattr(context, "session_id", None) if context else None
    agent = get_agent(session_id, mode, payload.get("language"), enable_search, use_skills)
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
    return {**result, "mode": mode, "skills": use_skills}


if __name__ == "__main__":
    app.run()
