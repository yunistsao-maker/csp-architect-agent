"""Diagram Generator agent from aws-samples/bedrock-engineer, ported to Strands + Amazon Bedrock AgentCore Runtime.

Payload:
    {
      "prompt": "Serverless web app with API Gateway, Lambda and DynamoDB",   # required
      "mode": "aws" | "software-architecture" | "business-process",         # default "aws"
      "language": "English",                                                 # optional, default: match the user
      "enable_search": false                                                 # optional, needs TAVILY_API_KEY
    }

Response:
    {"xml": "<mxfile>...</mxfile>", "explanation": "...markdown...", "mode": "aws"}
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5-5")
REGION = os.environ.get("AWS_REGION", "us-east-1")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

PROMPT_DIR = Path(__file__).parent / "prompts"
MODES = {
    "aws": "aws.md",
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


def extract_drawio_xml(content: str) -> str:
    """Port of xmlParser.extractDrawioXml from bedrock-engineer."""
    m = re.search(r"<mxfile[^>]*>[\s\S]*</mxfile>", content, re.I)
    if m:
        return m.group(0)
    m = re.search(r"<mxGraphModel[\s\S]*?</mxGraphModel>", content, re.I)
    if m:
        ts = datetime.now(timezone.utc).isoformat()
        return f'<mxfile host="Electron" modified="{ts}" type="device">\n  <diagram>\n    {m.group(0)}\n  </diagram>\n</mxfile>'
    return ""


def extract_diagram_content(content: str) -> dict:
    """Port of xmlParser.extractDiagramContent: split XML from the explanation text."""
    xml = extract_drawio_xml(content)
    if not xml:
        return {"xml": "", "explanation": content.strip()}
    explanation = re.sub(r"```(?:xml)?\s*<mxfile[\s\S]*?</mxfile>\s*```", "", content, count=1, flags=re.I)
    explanation = explanation.replace(xml, "")
    return {"xml": xml, "explanation": explanation.strip()}


def get_agent(session_id: str | None, mode: str, language: str | None, enable_search: bool) -> Agent:
    key = (session_id, mode, language, enable_search)
    if session_id and key in _agents:
        return _agents[key]
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION, max_tokens=32000),
        system_prompt=build_system_prompt(mode, language, enable_search),
        tools=[tavilySearch] if enable_search else [],
        callback_handler=None,
    )
    if session_id:
        _agents[key] = agent
    return agent


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
    result = agent(prompt)
    text = "".join(c.get("text", "") for c in result.message.get("content", []))
    return {**extract_diagram_content(text), "mode": mode}


if __name__ == "__main__":
    app.run()
