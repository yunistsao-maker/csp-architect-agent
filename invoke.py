"""Call the deployed Diagram Generator agent and save the result as a .drawio file.

Usage: python invoke.py "<description>" [aws|software-architecture|business-process] [session-id]
Reuse the same session-id to refine the previous diagram in a follow-up request.
"""

import json
import sys
import uuid

import boto3
import yaml
from botocore.config import Config

cfg = yaml.safe_load(open(".bedrock_agentcore.yaml"))
agent_cfg = cfg["agents"][cfg["default_agent"]]
arn = agent_cfg["bedrock_agentcore"]["agent_arn"]
region = agent_cfg["aws"]["region"]

prompt = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "aws"
session_id = sys.argv[3] if len(sys.argv) > 3 else f"diagram-{uuid.uuid4()}"

client = boto3.client("bedrock-agentcore", region_name=region, config=Config(read_timeout=600))
resp = client.invoke_agent_runtime(
    agentRuntimeArn=arn,
    runtimeSessionId=session_id,
    payload=json.dumps({"prompt": prompt, "mode": mode}),
)
data = json.loads(resp["response"].read())
if "error" in data:
    sys.exit(data["error"])

out = f"diagram-{mode}.drawio"
with open(out, "w", encoding="utf-8") as f:
    f.write(data["xml"])
with open(f"diagram-{mode}.md", "w", encoding="utf-8") as f:
    f.write(data["explanation"])
print(data["explanation"])
print(f"\nSaved {out} + diagram-{mode}.md  (session: {session_id})")
