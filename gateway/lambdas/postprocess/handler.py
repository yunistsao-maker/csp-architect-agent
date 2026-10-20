"""Post-processing hook behind the tools gateway (Lambda target, tool `process_architecture`).

The agent calls this tool from code right after it generates a diagram (it is not an LLM tool choice).
The gateway invokes the function with its IAM role and passes the allowlisted `x-user-alias` header in
`context.client_context.custom["bedrockAgentCorePropagatedHeaders"]`.

What it does:
1. Validates the draw.io XML and auto-fixes known-wrong icon names (same drawio_validator as the agent).
2. Stores the final .drawio and explanation in S3 (ARTIFACT_BUCKET, optional), keyed by alias and date.
3. Writes one structured log line with the caller alias for auditing.
4. Returns the final XML, the validation report and the S3 location.
"""

import json
import os
import time
import uuid

from drawio_validator import auto_fix_icons, validate

ALIAS_HEADER = "x-user-alias"


def caller_alias(context) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    headers = custom.get("bedrockAgentCorePropagatedHeaders") or {}
    if isinstance(headers, str):  # tolerate a JSON-encoded map
        try:
            headers = json.loads(headers)
        except json.JSONDecodeError:
            headers = {}
    for k, v in headers.items():
        if k.lower() == ALIAS_HEADER:
            return v
    return "unknown"


def _store(bucket: str, alias: str, mode: str, xml: str, explanation: str) -> str:
    import boto3  # imported lazily so unit tests need no AWS

    key = f"diagrams/{alias}/{time.strftime('%Y/%m/%d')}/{mode}-{uuid.uuid4().hex[:8]}"
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=f"{key}.drawio", Body=xml.encode(), ContentType="application/xml",
                  Metadata={"user-alias": alias, "mode": mode})
    s3.put_object(Bucket=bucket, Key=f"{key}.md", Body=(explanation or "").encode(), ContentType="text/markdown",
                  Metadata={"user-alias": alias, "mode": mode})
    return f"s3://{bucket}/{key}.drawio"


def lambda_handler(event, context):
    alias = caller_alias(context)
    xml = event.get("xml") or ""
    mode = event.get("mode") or "unknown"
    fixed_xml, fixes = auto_fix_icons(xml) if xml else ("", [])
    report = validate(fixed_xml) if fixed_xml else {"ok": False, "errors": ["empty diagram"], "icon_errors": [],
                                                       "pages": 0, "vertices": 0, "edges": 0}
    location = None
    bucket = os.environ.get("ARTIFACT_BUCKET")
    if bucket and fixed_xml:
        location = _store(bucket, alias, mode, fixed_xml, event.get("explanation") or "")
    print(json.dumps({"component": "postprocess-hook", "alias": alias, "mode": mode,
                      "session_id": event.get("session_id"), "valid": report["ok"],
                      "pages": report["pages"], "vertices": report["vertices"], "edges": report["edges"],
                      "icons_fixed": sum(f["count"] for f in fixes), "xml_chars": len(fixed_xml),
                      "artifact": location}))
    return {"alias": alias, "xml": fixed_xml, "validation": {**report, "auto_fixed": fixes}, "artifact": location}
