"""Call the deployed Diagram Generator agent and save the result as a .drawio file.

Usage: python invoke.py "<description>" [aws|azure|gcp|software-architecture|business-process] [session-id] [-o NAME] [--skills]
Reuse the same session-id to refine the previous diagram in a follow-up request.
Output: NAME.drawio + NAME.md (default NAME: diagram-<mode>-<timestamp>, so runs never overwrite each other).
"""

import argparse
import json
import sys
import uuid
from datetime import datetime

import boto3
import yaml
from botocore.config import Config

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("prompt")
ap.add_argument("mode", nargs="?", default="aws")
ap.add_argument("session_id", nargs="?", default=None)
ap.add_argument("-o", "--out", help="output name without extension")
ap.add_argument("--language", help='e.g. "Traditional Chinese"')
ap.add_argument("--skills", action="store_true", help="skills mode (aws/azure/gcp): load Agent Skills on demand")
args = ap.parse_args()

cfg = yaml.safe_load(open(".bedrock_agentcore.yaml"))
agent_cfg = cfg["agents"][cfg["default_agent"]]
arn = agent_cfg["bedrock_agentcore"]["agent_arn"]
region = agent_cfg["aws"]["region"]
session_id = args.session_id or f"diagram-{uuid.uuid4()}"

payload = {"prompt": args.prompt, "mode": args.mode}
if args.language:
    payload["language"] = args.language
if args.skills:
    payload["skills"] = True

client = boto3.client("bedrock-agentcore", region_name=region, config=Config(read_timeout=900))
resp = client.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session_id, payload=json.dumps(payload))
data = json.loads(resp["response"].read())
if "error" in data:
    sys.exit(f"[{data.get('error_code', 'error')}] {data['error']}")

if not data.get("xml"):
    print(data.get("explanation", ""))
    sys.exit(f"\nNo diagram was returned (session: {session_id}). Reply in the same session to continue.")

out = args.out or f"diagram-{args.mode}-{datetime.now():%Y%m%d-%H%M%S}"
with open(f"{out}.drawio", "w", encoding="utf-8") as f:
    f.write(data["xml"])
with open(f"{out}.md", "w", encoding="utf-8") as f:
    f.write(data["explanation"])
print(data["explanation"])

v = data.get("validation") or {}
status = "valid" if v.get("ok") else f"{len(v.get('errors', []))} errors, {len(v.get('icon_errors', []))} unknown icons"
fixed = sum(x["count"] for x in v.get("auto_fixed", []))
print(f"\nSaved {out}.drawio + {out}.md  (session: {session_id})")
print(f"Validation: {status} · {v.get('pages', '?')} page(s), {v.get('vertices', '?')} shapes, {v.get('edges', '?')} edges"
      f" · {fixed} icon name(s) auto-fixed · {v.get('repair_turns', 0)} repair turn(s)")
for e in v.get("errors", []) + [f"unknown icon {i['name']}" for i in v.get("icon_errors", [])]:
    print(f"  ! {e}")
u = data.get("usage") or {}
if u:
    print(f"Usage: {'skills' if data.get('skills') else 'prompt'} mode · {u.get('input_tokens')} in / {u.get('output_tokens')} out tokens"
          f" · {u.get('model_calls')} model call(s), {u.get('tool_calls')} tool call(s)"
          + (f" · skills: {', '.join(u['skills_activated'])}" if u.get("skills_activated") else ""))
