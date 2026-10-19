Create a diagram about the CSP Architect Agent (Amazon Bedrock AgentCore Runtime + Strands Agents + Claude on Bedrock).



## Architecture Overview
The CSP Architect Agent is a serverless, session-based agent hosted on **Amazon Bedrock AgentCore Runtime**. A developer runs `invoke.py` on a laptop, authenticated through IAM Identity Center SSO. The script sends a JSON request (`prompt`, `mode`, `language`, `enable_search`, `skills`) to the runtime. Inside a per-session microVM, a Strands Agent calls Claude on Amazon Bedrock to generate draw.io XML plus an explanation. A post-processing pipeline then validates and repairs the XML before the response returns to the client.

AgentCore Runtime has no icon in the allowed icon list, so it is drawn as a labelled container. The same applies to the internal Python components and the external Tavily API, which are rounded rectangles. Flow runs left to right (client → runtime → Bedrock / Tavily). The response returns along the bottom edge.

## Component Details
- **Client (invoke.py)**: Uses boto3 `InvokeAgentRuntime` with SigV4 credentials from an IAM Identity Center SSO role. It writes `.drawio` and `.md` files, and `to_html.py` bundles them into a tabbed HTML viewer.
- **AgentCore Runtime `csp_architect_agent`**: PUBLIC network mode, HTTP protocol, Python 3.12 direct code deploy, version 4, idle timeout 15 min, max lifetime 8 h, one microVM per session.
- **Amazon S3 `bedrock-agentcore-codebuild-sources`**: Holds the `deployment.zip` code artifact.
- **IAM role `AmazonBedrockAgentCoreSDKRuntime`**: Execution role used by the runtime. The **AgentCore workload identity** is shown inside the runtime.
- **CloudWatch Logs / observability**: Receives runtime logs and traces.
- **agent.py entrypoint**: A `BedrockAgentCoreApp` entrypoint that parses the payload.
- **Prompt selector**: Chooses one of 5 system prompts (aws, azure, gcp, software-architecture, business-process).
- **Strands Agent**: Cached per `session_id` so follow-up requests can refine earlier diagrams.
- **Skills mode (optional)**: The Strands AgentSkills plugin and a `read_skill_file` tool load the aws/azure/gcp-architecture-diagram and strict-networking-review skills.
- **tavilySearch tool (optional)**: Makes outbound HTTPS calls to the Tavily Search API, only if `TAVILY_API_KEY` is set.
- **Amazon Bedrock (Claude Sonnet 5.5)**: Invoked through a `us.` cross-region inference profile with `max_tokens` 64000 and a 900 s read timeout.
- **Post-processing pipeline**:
  - XML extractor (`mxfile` / `mxGraphModel`).
  - `drawio_validator`, which checks:
    - XML well-formedness.
    - Unique ids.
    - Edge source/target references.
    - Icon names against the `drawio_icons.json` registry (1,050 AWS4 / 297 GCP2 / 704 Azure2 icons).
  - Auto-fix of known-wrong icon names.
  - Repair loop with up to `MAX_REPAIR_TURNS=1` extra model turn.
  - `max_tokens` guard that rolls history back.

## Data Flow
1. `invoke.py` signs and sends the request to AgentCore Runtime with SigV4 (SSO role credentials).
2. The entrypoint parses the payload.
3. The prompt selector picks the system prompt for the `mode`, and the Strands Agent for the `session_id` is fetched or created.
4. The Strands Agent calls Claude on Bedrock.
5. If `skills` is on, it loads skills through the AgentSkills plugin.
6. If `enable_search` is on, it calls `tavilySearch`.
7. `tavilySearch` makes an outbound HTTPS call to Tavily (only if `TAVILY_API_KEY` is set).
8. The raw model output goes to the XML extractor.
9. The validator checks the extracted XML.
10. Auto-fix corrects known-wrong icon names.
11. If problems remain, the repair loop runs one extra model turn (11b).
12. The `max_tokens` guard rolls history back on truncation.
13. The response `{xml, explanation, mode, validation, usage}` returns to the client.
14. `invoke.py` writes `.drawio` and `.md` files, and `to_html.py` builds the HTML viewer.

## Benefits
- Serverless, per-session isolation with no servers to manage.
- Session memory enables iterative diagram refinement.
- Validation, auto-fix, and a bounded repair loop reduce broken or blank draw.io icons.
- Five modes and optional skills and web search make one agent reusable across clouds and diagram types.

## Security Considerations
- Client calls are SigV4-signed with short-lived SSO credentials, not static keys.
- The runtime uses a dedicated execution role and an AgentCore workload identity.
- The runtime is in PUBLIC network mode, so access control relies on IAM authorization of `InvokeAgentRuntime`.
- The Tavily key is supplied by environment variable. Consider storing it in AWS Secrets Manager.
- Search is opt-in, so outbound traffic is limited to Bedrock and, optionally, Tavily.

## Cost Optimization
- AgentCore bills for active runtime use, and the 15 min idle timeout limits idle microVMs.
- The repair loop is capped at one extra model turn, which bounds token spend.
- Skills and search are optional, which avoids extra tokens and external calls.
- The `usage` field in the response helps track token consumption.

## Best Practices
- Validate model output against an icon registry before delivering it.
- Keep a bounded repair loop and a `max_tokens` guard that rolls history back.
- Use SSO-based credentials for local invocation.
- Use CloudWatch observability to trace session behaviour and model latency.
- Pin the code artifact version (currently version 4) for reproducible deployments.

## References
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
- https://strandsagents.com/
- https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles.html
- https://docs.aws.amazon.com/singlesignon/latest/userguide/