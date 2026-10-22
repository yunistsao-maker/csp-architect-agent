"""Model + turn helpers shared by the portal agents."""

import os

from botocore.config import Config
from strands.models import BedrockModel
from strands.types.exceptions import MaxTokensReachedException

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5-5")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "64000"))
MAX_REPAIR_TURNS = int(os.environ.get("MAX_REPAIR_TURNS", "1"))


class OutputTruncated(Exception):
    pass


def make_model(model_cls=None):
    """Bedrock through the bedrock-runtime VPC endpoint (private DNS); long read timeout for large outputs."""
    cls = model_cls or BedrockModel
    return cls(model_id=MODEL_ID, region_name=os.environ.get("AWS_REGION", "us-east-1"), max_tokens=MAX_TOKENS,
               boto_client_config=Config(read_timeout=900, retries={"max_attempts": 3, "mode": "adaptive"}))


def new_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "model_calls": 0, "tool_calls": 0}


def _tokens(agent) -> tuple[int, int]:
    u = agent.event_loop_metrics.accumulated_usage
    return u.get("inputTokens", 0), u.get("outputTokens", 0)


def run_turn(agent, content, usage: dict | None = None, session_manager=None) -> str:
    """One user turn. On max_tokens, roll back the in-memory and the persisted conversation, then raise
    OutputTruncated, so the session stays usable for the next (smaller) request."""
    checkpoint = len(agent.messages)
    latest = getattr(session_manager, "_latest_agent_message", {}).get(agent.agent_id) if session_manager else None
    before = _tokens(agent)
    try:
        result = agent(content)
    except MaxTokensReachedException as e:
        del agent.messages[checkpoint:]
        if session_manager is not None:
            from portal.sessions import delete_messages_after
            delete_messages_after(session_manager.session_repository, session_manager.session_id, agent.agent_id,
                                  latest.message_id if latest else None)
            session_manager._latest_agent_message[agent.agent_id] = latest
        raise OutputTruncated(str(e)) from e
    if usage is not None:
        after = _tokens(agent)
        usage["input_tokens"] += after[0] - before[0]
        usage["output_tokens"] += after[1] - before[1]
        for msg in agent.messages[checkpoint:]:
            usage["model_calls"] += msg.get("role") == "assistant"
            usage["tool_calls"] += sum(1 for b in msg.get("content", []) if isinstance(b, dict) and "toolUse" in b)
    return "".join(c.get("text", "") for c in result.message.get("content", []))
