# CSP Architect Agent (Diagram Generator on AgentCore)

The **Diagram Generator** agent from [aws-samples/bedrock-engineer](https://github.com/aws-samples/bedrock-engineer)
(`src/renderer/src/pages/ChatPage/constants/DEFAULT_AGENTS.ts` + `DiagramGeneratorPage/utils/xmlParser.ts`),
ported to a Strands agent running on Amazon Bedrock AgentCore Runtime.

- `agent.py` – AgentCore entrypoint; returns `{"xml", "explanation", "mode"}`
- `prompts/` – the original system prompts for the 3 modes: `aws`, `software-architecture`, `business-process`
- `invoke.py` – client that calls the deployed runtime and writes a `.drawio` file

## Payload
```json
{"prompt": "...", "mode": "aws", "language": "English", "enable_search": false}
```
`enable_search` requires a `TAVILY_API_KEY` env var on the runtime. The model defaults to
`us.anthropic.claude-sonnet-5-5` (override with `MODEL_ID`). If you reuse the same runtime session ID,
the agent keeps the conversation history, so a follow-up message can refine the previous diagram.

## Commands
```bash
uv venv -p 3.12 && source .venv/bin/activate
uv pip install bedrock-agentcore strands-agents bedrock-agentcore-starter-toolkit pyyaml boto3
agentcore configure -e agent.py -n csp_architect_agent -r us-east-1 -rf requirements.txt \
  -dt direct_code_deploy -rt PYTHON_3_12 -dm -ni
agentcore deploy                      # deploy / redeploy after changes
python invoke.py "Serverless app with API GW, Lambda, DynamoDB" aws
agentcore invoke '{"prompt": "3-tier web app on EC2 with RDS"}'
agentcore destroy                     # remove all AWS resources
```
