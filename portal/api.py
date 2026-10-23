"""API contract helpers shared by every runtime (contract/v1/api.schema.json, contract/README.md).

- api_version: a caller may say which major version it speaks ("1"); a runtime that doesn't serve it answers
  error_code unsupported_api_version instead of guessing. Every response carries the version it implements.
- trace_id: the caller's correlation id, echoed in results and written to the logs, so one request can be followed
  from the portal through the gateway into the runtime.
"""

import json
import logging
import re
import sys

API_VERSION = "1.0.0"  # = contract/VERSION; tests keep them equal
MAJOR = API_VERSION.split(".")[0]
TRACE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
log = logging.getLogger("csp.api")
if not log.handlers:  # one JSON object per line on stdout -> CloudWatch Logs
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def check_version(payload: dict) -> dict | None:
    asked = payload.get("api_version")
    if asked is None:
        return None
    if not isinstance(asked, str) or asked.split(".")[0] != MAJOR:
        return {"error": f"This runtime serves API version {API_VERSION}; the request asked for {asked!r}.",
                "error_code": "unsupported_api_version", "api_version": API_VERSION}
    return None


def trace_id(payload: dict) -> str | None:
    t = payload.get("trace_id")
    return t if isinstance(t, str) and TRACE_RE.match(t) else None


def stamp(out: dict, payload: dict | None = None, trace: str | None = None) -> dict:
    """Add api_version (and the caller's trace_id) to a response."""
    t = trace or (trace_id(payload) if payload else None)
    return {**out, "api_version": API_VERSION, **({"trace_id": t} if t else {})}


def event(name: str, **fields) -> None:
    """One structured log line (CloudWatch Logs Insights can filter on trace_id / job_id)."""
    log.info(json.dumps({"event": name, **{k: v for k, v in fields.items() if v is not None}}, ensure_ascii=False, default=str))
