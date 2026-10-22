"""Diagram Generator agent from aws-samples/bedrock-engineer, ported to Strands + Amazon Bedrock AgentCore Runtime.

Payload:
    {
      "prompt": "Serverless web app with API Gateway, Lambda and DynamoDB",   # required
      "mode": "aws" | "azure" | "gcp" | "software-architecture" | "business-process",  # default "aws"
      "language": "English",                                                 # optional, default: match the user
      "enable_search": false,              # optional: web search via the tools gateway (TOOLS_GATEWAY_URL) or TAVILY_API_KEY
      "skills": false,                      # optional (aws/azure/gcp): load Agent Skills from skills/ instead of the
                                            # per-mode prompt; default from USE_SKILLS
      "async": false                        # optional: submit a job and return {"job_id"} at once; default ASYNC_DEFAULT
    }
    Poll an async job with {"action": "status", "job_id": "..."}: status RUNNING | SUCCEEDED | FAILED | LOST, and a
    finished job carries the same fields as a synchronous response. Only the caller alias that submitted a job can
    read it.

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
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.config import Config
from strands import Agent, AgentSkills, tool
from strands.models import BedrockModel
from strands.types.exceptions import MaxTokensReachedException

from drawio_validator import auto_fix_icons, repair_prompt, validate
from jobs import JOB_ID_RE, Heartbeat, MemoryJobStore, S3JobStore, new_job_id, new_record, now, public_view
from tools_gateway import ToolsGatewayClient, ToolsGatewayError, tool_json, tool_text

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5-5")
REGION = os.environ.get("AWS_REGION", "us-east-1")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
# Gateway mode (see gateway/README.md). The ingress gateway's interceptor puts the caller's alias in this header;
# the runtime must allowlist it (requestHeaderConfiguration).
USER_ALIAS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias"
REQUIRE_CALLER_ALIAS = os.environ.get("REQUIRE_CALLER_ALIAS", "false").lower() in ("1", "true", "yes")
# Tools gateway (MCP, AWS_IAM inbound): web search connector + post-processing hook Lambda.
TOOLS_GATEWAY_URL = os.environ.get("TOOLS_GATEWAY_URL")
WEB_SEARCH_TARGET = os.environ.get("WEB_SEARCH_TARGET", "web-search-tool")
HOOK_TARGET = os.environ.get("HOOK_TARGET", "postprocess-hook")
HOOK_TOOL = os.environ.get("HOOK_TOOL", "process_architecture")
HOOK_REQUIRED = os.environ.get("HOOK_REQUIRED", "false").lower() in ("1", "true", "yes")
# Async jobs: AgentCore Gateway ends a call after 15 minutes, so in gateway mode a call submits a job and the
# client polls {"action": "status", "job_id": ...}. Job state is in s3://JOB_BUCKET/jobs/ (in memory if unset).
ASYNC_DEFAULT = os.environ.get("ASYNC_DEFAULT", "false").lower() in ("1", "true", "yes")
JOB_BUCKET = os.environ.get("JOB_BUCKET")
JOB_HEARTBEAT_SECONDS = float(os.environ.get("JOB_HEARTBEAT_SECONDS", "60"))
JOB_STALE_SECONDS = float(os.environ.get("JOB_STALE_SECONDS", "300"))
JOB_POLL_HINT_SECONDS = int(os.environ.get("JOB_POLL_HINT_SECONDS", "15"))
# Adaptive thinking counts toward max_tokens: at 64k, mid-size diagrams (e.g. a private LLM platform) ran out before the
# XML was complete. 128k is the Sonnet 5.5 output maximum (BedrockModel streams, so no HTTP timeout).
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "128000"))
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


def make_tools_client(alias: str | None) -> ToolsGatewayClient:
    """One client per request so the caller's alias header is never shared between users."""
    return ToolsGatewayClient(TOOLS_GATEWAY_URL, REGION, alias)


@tool(context=True)
def web_search(query: str, tool_context) -> str:
    """Search the web for up-to-date information (cloud service features, limits, regional availability).

    Args:
        query: The search query.
    """
    alias = tool_context.agent.state.get("caller_alias")
    client = make_tools_client(alias)
    try:
        spec = client.find_tool(WEB_SEARCH_TARGET)
        props = list((spec.get("inputSchema") or {}).get("properties", {}))
        arg = "query" if "query" in props or not props else props[0]
        return tool_text(client.call_tool(spec["name"], {arg: query}))[:20000]
    except (ToolsGatewayError, OSError) as e:
        return f"Web search failed: {e}"


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
    elif TOOLS_GATEWAY_URL:
        prompt = prompt.replace("use the tavilySearch tool", "use the web_search tool")
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
    tools = [web_search if TOOLS_GATEWAY_URL else tavilySearch] if enable_search else []
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


def caller_alias(context) -> str | None:
    headers = getattr(context, "request_headers", None) or {}
    for k, v in headers.items():
        if k.lower() == USER_ALIAS_HEADER.lower():
            return v
    return None


def run_postprocess_hook(result: dict, mode: str, alias: str | None, session_id: str | None) -> dict:
    """Call the post-processing Lambda behind the tools gateway (IAM) and merge its output.

    This is a deterministic hook after generation, not a tool the model chooses. The alias travels in the
    x-user-alias header; the Lambda reads it from the gateway's propagated headers for logging.
    """
    v = result.get("validation") or {}
    try:
        client = make_tools_client(alias)
        name = client.find_tool(HOOK_TARGET, HOOK_TOOL)["name"]
        out = tool_json(client.call_tool(name, {
            "xml": result["xml"], "explanation": result.get("explanation", ""), "mode": mode,
            "session_id": session_id or "", "agent_validation_ok": bool(v.get("ok"))}))
    except (ToolsGatewayError, OSError, ValueError, KeyError) as e:
        if HOOK_REQUIRED:
            raise
        return {**result, "postprocess": {"ok": False, "error": f"{type(e).__name__}: {e}"}}
    merged = {**result, "postprocess": {"ok": True, "artifact": out.get("artifact"), "alias": out.get("alias"),
                                        "validation_ok": (out.get("validation") or {}).get("ok")}}
    if out.get("xml"):
        merged["xml"] = out["xml"]
    return merged


def _check_request(payload: dict) -> dict | None:
    if not payload.get("prompt"):
        return {"error": "Missing 'prompt' in payload"}
    mode = payload.get("mode", "aws")
    if mode not in MODES:
        return {"error": f"Invalid mode '{mode}'. Use one of: {', '.join(MODES)}"}
    return None


def generate(payload: dict, alias: str | None, session_id: str | None) -> dict:
    """One diagram, end to end: agent turns, validation/repair, then the post-processing hook (gateway mode)."""
    mode = payload.get("mode", "aws")
    enable_search = bool(payload.get("enable_search")) and bool(TOOLS_GATEWAY_URL or TAVILY_API_KEY)
    use_skills = bool(payload.get("skills", USE_SKILLS)) and mode in SKILL_FOR_MODE
    agent = get_agent(session_id, mode, payload.get("language"), enable_search, use_skills)
    agent.state.set("caller_alias", alias)  # always overwrite: a cached session agent must not keep an old alias
    try:
        result = generate_validated(agent, payload["prompt"])
    except OutputTruncated:
        return {
            "error": (
                f"The diagram exceeded the model's output limit (MAX_TOKENS={MAX_TOKENS}) and was not returned. "
                "Ask for fewer components, or for one page per concern (e.g. network / workloads / MLOps), "
                "or raise MAX_TOKENS if the model allows it."
            ),
            "error_code": "max_tokens",
            "mode": mode,
            "caller": {"alias": alias},
        }
    if TOOLS_GATEWAY_URL and result.get("xml"):
        result = run_postprocess_hook(result, mode, alias, session_id)
    return {**result, "mode": mode, "skills": use_skills, "caller": {"alias": alias}}


# ------------------------------------------------------------------------------------------- async jobs
_job_store = None


def job_store():
    global _job_store
    if _job_store is None:
        _job_store = S3JobStore(JOB_BUCKET) if JOB_BUCKET else MemoryJobStore()
    return _job_store


def spawn(fn, *args) -> None:
    """Run fn in a background thread (tests replace this to run inline)."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def run_job(record: dict, payload: dict, alias: str | None) -> None:
    store = job_store()
    task_id = app.add_async_task("diagram_job", {"job_id": record["job_id"]})  # ping -> HealthyBusy
    try:
        with Heartbeat(store, record, JOB_HEARTBEAT_SECONDS):
            try:
                result = generate(payload, alias, None)  # fresh agent: no history shared between jobs
            except Exception as e:  # noqa: BLE001 - the job record must always reach a terminal state
                result = {"error": f"{type(e).__name__}: {e}", "error_code": "internal_error",
                          "caller": {"alias": alias}}
        # Write the final state only after the heartbeat thread has stopped, so a late beat cannot overwrite it.
        store.put({**record, "status": "FAILED" if result.get("error") else "SUCCEEDED", "updated_at": now(),
                   "heartbeat_at": now(), "result": result})
    finally:
        app.complete_async_task(task_id)


def submit_job(payload: dict, alias: str | None) -> dict:
    mode = payload.get("mode", "aws")
    request = {"mode": mode, "skills": bool(payload.get("skills", USE_SKILLS)) and mode in SKILL_FOR_MODE,
               "enable_search": bool(payload.get("enable_search")), "language": payload.get("language"),
               "prompt_chars": len(payload["prompt"])}
    record = new_record(new_job_id(), alias, request)
    job_store().put(record)
    spawn(run_job, record, payload, alias)
    return {"job_id": record["job_id"], "status": "RUNNING", "poll_after_s": JOB_POLL_HINT_SECONDS,
            "mode": mode, "caller": {"alias": alias}}


def job_status(job_id: str | None, alias: str | None) -> dict:
    not_found = {"error": f"Job '{job_id}' not found.", "error_code": "not_found", "job_id": job_id}
    if not job_id or not JOB_ID_RE.match(job_id):
        return not_found
    record = job_store().get(job_id)
    # Only the caller who submitted the job can read it; anyone else gets the same answer as a missing job.
    if record is None or record.get("alias") != alias:
        return not_found
    return {**public_view(record, JOB_STALE_SECONDS), "caller": {"alias": alias}}


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    alias = caller_alias(context)
    if REQUIRE_CALLER_ALIAS and not alias:
        return {"error": "Caller alias header missing; invoke this agent through the ingress gateway.",
                "error_code": "forbidden"}
    action = payload.get("action", "generate")
    if action == "status":
        return job_status(payload.get("job_id"), alias)
    if action != "generate":
        return {"error": f"Invalid action '{action}'. Use 'generate' or 'status'.", "error_code": "bad_request"}
    problem = _check_request(payload)
    if problem:
        return problem
    if bool(payload.get("async", ASYNC_DEFAULT)):
        return submit_job(payload, alias)
    session_id = getattr(context, "session_id", None) if context else None
    return generate(payload, alias, session_id)


if __name__ == "__main__":
    app.run()
