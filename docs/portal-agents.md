# Portal agents: COE documents, AICOE documents, Terraform

The on-prem portal reaches three more agents next to the CSP architect agent. All four are behind the same private
ingress (PrivateLink → AgentCore Gateway, JWT from the corporate IdP, IAM inside); see
[gateway/README.md](../gateway/README.md) for that path.

| Agent | Target | What it does | Output |
|---|---|---|---|
| CSP architect | `/csp-architect` | prompt → draw.io architecture (existing) | `.drawio` + explanation |
| **COE documents** | `/coe-doc` | **prepare** a Cloud CoE document from the template, **review** one against the CoE checklist, or **fill** one of the CoE's forms for a project | `docs/coe/…md` / review report / filled form |
| **AICOE documents** | `/aicoe-doc` | same for the AI CoE (AI use-case assessment, model change, AI questionnaires) | `docs/aicoe/…md` / review report / filled form |
| **Terraform** | `/terraform` | requirements, an uploaded diagram, or a CSP architect `job_id` → a Terraform module that passes the static checks | `terraform/<name>/…` |

![Network and security path](architecture/portal-agents-network.png)
![Agents and data flow](architecture/portal-agents-flow.png)

The editable source is [architecture/portal-agents-architecture.drawio](architecture/portal-agents-architecture.drawio)
(two pages), regenerated with `scripts/draw_portal_architecture.py`.

## Design decisions
- **One AgentCore Runtime per agent.** Each has its own IAM role, deploy cycle and scaling. Only the Terraform
  runtime can reach the infrastructure repository profile, and a bug in one agent can't take the others down. All
  share `portal/runtime.py`.
- **Templates are data.** `templates/coe|aicoe/templates.yaml` (doc types, required headings, guidance) and
  `checklist.yaml` (review items with severity and doc types). The same files build the prompt *and* validate the
  output: a draft missing a required heading, or a review that leaves a checklist item without a status, gets a
  repair turn and is not publishable until it passes. Replace the shipped defaults with your CoE's own templates.
- **Unknowns are never invented.** Drafts mark missing facts as `【待確認】` and list them in `open_questions`. The
  portal can show these and send the answers as the next turn in the same session.
- **Forms are filled, not judged.** `mode: "fill"` ([form_fill.py](../form_fill.py)) answers a questionnaire or
  self-check list (Y* / Y / N / NA, single / multi choice, free text) from the project material the portal sends,
  with a confidence (0-100), a basis and a reason for every answer, and a list of questions for what it can't
  support. It never picks the compliant answer without evidence; veto (否決題) and high-risk items stay empty with a
  question instead. The form comes in the payload (`form`: the portal's own form, or the answers so far) or as an
  attached file the agent turns into items. The form and the project material go in the system prompt, so a
  long session keeps only the conversation; follow-up turns return only the items they change.
- **Terraform is checked here, proven in CI.** `terraform_checks.py` parses every file (python-hcl2) and enforces
  the baseline in `templates/terraform/standards.yaml`: pinned versions, S3 backend with locking, required
  default tags, no credentials (in any file), no 0.0.0.0/0 ingress (inline, `dynamic`, `aws_security_group_rule`), no
  IGW / NAT / public IPs (also through module inputs), encryption, IMDSv2, KMS rotation.
  - **Code that would run in CI is refused, whatever the input says:** model output is untrusted because uploads
    and web results can carry prompt injection. This covers `data "external"`, `data "http"`, provisioners,
    `null_resource` / `terraform_data`, the external / null / http providers, and modules or providers from
    sources outside the allowlists (TF015–TF017, not waivable).
  - **A justified exception is waived for one resource** with a visible comment,
    `# tfcheck:allow TF005 aws_security_group.alb <reason>`. `terraform validate / plan` and human review
  happen in the repository's CI on the merge request. The agent has no cloud credentials for the target accounts.
- **Results become merge requests, not commits.** `{"action": "publish", "job_id"}` calls the `git-pr` tool (a
  Lambda target on the tools gateway, VPC-attached, reaching GitLab / GitHub Enterprise over TGW → DX):
  - branch `agent/<kind>/<alias>/<job>`;
  - a `Requested-by` trailer;
  - idempotent (one job, one MR);
  - only when the checks passed.
  Each agent kind has a profile in `gateway/agents.yaml` (repository, base branch, path prefix, file types), so an
  agent can't choose where it writes.
- **Tool ACL.** The tools-gateway interceptor allows each agent kind only its own targets (`TOOL_ACL`, built from
  `agents.yaml`): CSP architect gets web search + hook, COE / AICOE get git-pr, Terraform gets web search + git-pr.
  The kind travels in `x-agent-kind`, set by agent code, never by the model.
  - **Known limitation:** the tools gateway authenticates the runtime role, not the kind. A compromised runtime
    (not a prompt) could claim another kind. For IAM-level separation, give each agent its own tools gateway; the
    trade-off is more gateways to operate.
  - The git-pr Lambda can only be invoked by the tools gateway (resource policy `SourceArn`), since it trusts the
    propagated headers.

## Sessions in DynamoDB
A gateway call can land on any runtime microVM, so a multi-turn conversation (refining a draft, answering open
questions) is stored by `portal/sessions.py`, a Strands `SessionRepository` on one DynamoDB table:

| Item | Key |
|---|---|
| session | `PK = <alias>#<session_id>`, `SK = SESSION` |
| agent state | `SK = AGENT#<kind>` |
| messages | `SK = MSG#<kind>#<00000042>` |
| running-job lock | `SK = LOCK` (lease renewed by the job heartbeat) |

- **Scoped by alias:** the partition key starts with the caller's alias from the JWT, so the same `session_id` from
  another user is a different, empty conversation.
- **Expiry:** `expires_at` is the table's TTL attribute, set once when the session is created (`SESSION_TTL_DAYS`,
  default 90), so the whole conversation expires together. Expired items are ignored on read, even before DynamoDB
  deletes them.
- **Large items:** items over ~300 KB (long documents, image bytes in the history) are offloaded to
  `s3://<uploads bucket>/sessions/…`. That prefix has the same 90-day lifecycle rule.
- **One job at a time:** only one job runs per session. A second request gets `session_busy`.
  - The lease is renewed while the job runs (async heartbeat, or a renewer thread for `"async": false`).
  - If a microVM dies, the lease frees the session within 5 minutes.
  - A turn is never overwritten: message writes are conditional, so a conflicting writer fails with
    `session_conflict` instead of interleaving.
- **Attachments are not re-sent:** they are persisted as a short note, not their bytes. Later turns stay small and
  within Bedrock's per-request document limit. The files stay in S3 for 90 days and can be attached again.
- **Output limit:** a `max_tokens` turn is rolled back in DynamoDB as well as in memory.

## Uploads (images, documents) in S3 for 90 days
1. `{"action": "upload_url", "filename", "content_type", "size"}` returns a presigned PUT on the regional host
   (`<bucket>.s3.<region>.amazonaws.com`, or `UPLOAD_ENDPOINT_URL`) for
   `uploads/<alias>/<yyyy>/<mm>/<dd>/<id>/<name>`. It is valid for 5 minutes and bound to that content type and exact size.
   - Images: png, jpeg, gif, webp (≤ 3.75 MB).
   - Documents: pdf, docx, doc, xlsx, xls, csv, md, txt, html (≤ 4.5 MB). These are the Bedrock Converse limits.
2. The portal PUTs the file over DX to the **S3 interface endpoint**:
   - its private DNS answers only queries from the inbound Resolver, so on-prem resolves the bucket host to the
     endpoint, while workloads inside the VPC keep using the S3 gateway endpoint;
   - the bucket policy denies `PutObject` on `uploads/*` that doesn't come through that endpoint.
3. `{"prompt", "attachments": [key, …]}`: the runtime checks every key is under the caller's own prefix, checks
   the file signature, and passes images / documents to Claude as Converse content blocks.
4. Lifecycle rule: `uploads/` and `sessions/` expire after 90 days (`retention` in `gateway/agents.yaml`).

## API (all agents)
```jsonc
{"prompt": "...", "mode": "prepare|review", "doc_type": "solution-design", "title": "payments-gateway",
 "attachments": ["uploads/yhtsao/..."], "session_id": "pay-2026-001"}     // -> {"job_id", "status": "RUNNING"}
{"action": "status", "job_id": "..."}        // -> RUNNING | SUCCEEDED (document / review / files + checks) | FAILED | LOST
{"action": "upload_url", "filename": "board.jpg", "content_type": "image/jpeg", "size": 183422}
{"action": "publish", "job_id": "...", "title"?: "...", "description"?: "..."}   // -> {"pull_request": {"url", ...}}
```
Fill mode (COE / AICOE, no `doc_type`):
```jsonc
{"prompt": "開始填寫", "mode": "fill", "form"?: {"name", "title", "items": [{"id", "section", "q", "type", "options",
  "other", "guide", "choices", "expect", "veto", "reject", "high_risk", "detail", ...answers}]},
 "context"?: "PROJECT FILES ...", "attachments"?: ["uploads/..."], "language"?: "zh-TW", "session_id"?: "..."}
// SUCCEEDED -> "form" (items with answer / selected / other_text / text, note, confidence, basis, exception),
//              "updated" (ids changed this turn), "ask", "counts" (answered, veto, exception, schedule), "files"
```
`form` or an attached form file is required. With an unanswered `form`, every item gets a row; with an answered
one, only the items the message affects change. The published file is the answers table: no fill-in instructions,
no confidence.

Terraform also takes `"architecture_job_id"`: the CSP architect job whose diagram (summarised to a component /
connection list) should be implemented. Only the same alias's successful jobs can be used.

## Sandbox: before any corporate service is connected
`gateway/agents.yaml` is the **sandbox** configuration, while `gateway/agents.enterprise.yaml` keeps the corporate
IdP + internal Git setup:
- **Users:** the Cognito test pool. `deploy.py` creates `yhtsao` plus fake users `fake-user-01`, `fake-user-02`
  (`--fake-users N`, extra `--test-user NAME`). Their access tokens carry `username`, which becomes the alias.
  Generated passwords go to `gateway/.test-users.json` (mode 600, git-ignored), not the console. Clients read them
  from there.
- **Git:** merge requests go to one private github.com repo, `yunistsao-maker/portal-agents-sandbox`, with a folder
  per agent (`terraform/`, `docs/coe/`, `docs/aicoe/`). The git-pr Lambda runs outside the VPC
  (`git.network: public`), because github.com is on the internet.
- **Network:** there is no DX yet, so the quickest test is `--skip-vpc`. The gateway is then reachable from your
  laptop, but still only with a valid Cognito JWT; runtimes stay in public network mode. Add the VPC / PrivateLink
  pieces when the on-prem path exists.

```bash
# once: the sandbox repo (with a README, so main exists) and a fine-grained PAT for it only
#   (Contents, Pull requests, Issues: read/write)
gh repo create yunistsao-maker/portal-agents-sandbox --private --add-readme
aws secretsmanager create-secret --name csp-arch/github-token --secret-string '<fine-grained PAT>'

python gateway/deploy.py --skip-vpc                     # Cognito pool + yhtsao + fake-user-01/02, gateways, agents
python gateway/portal_client.py coe --doc-type solution-design --session demo-0001 "支付閘道上雲的方案設計"
python gateway/portal_client.py coe --user fake-user-01 --doc-type exception-request "例外申請：暫時開放 NAT"
python gateway/portal_client.py publish coe --job <job_id>          # -> PR in portal-agents-sandbox
python gateway/portal_client.py checks coe              # negative checks + yhtsao vs fake-user-01 isolation
```

## Deploy
```bash
# 1. one runtime per agent (VPC network mode is applied by deploy.py --runtime-vpc)
agentcore configure -e coe_agent.py       -n coe_doc_agent   -r us-east-1 -rf requirements.txt -dt direct_code_deploy -rt PYTHON_3_12 -dm -ni
agentcore configure -e aicoe_agent.py     -n aicoe_doc_agent -r us-east-1 -rf requirements.txt -dt direct_code_deploy -rt PYTHON_3_12 -dm -ni
agentcore configure -e terraform_agent.py -n terraform_agent -r us-east-1 -rf requirements.txt -dt direct_code_deploy -rt PYTHON_3_12 -dm -ni
agentcore deploy -a coe_doc_agent && agentcore deploy -a aicoe_doc_agent && agentcore deploy -a terraform_agent

# 2. Git bot token (project / repo scoped) for the git-pr Lambda; edit gateway/agents.yaml (git server, repos, CIDRs)
aws secretsmanager create-secret --name csp-arch/git-token --secret-string '<token>'

# 3. gateway, targets, table, buckets, Lambdas, endpoints, policies
python gateway/deploy.py --vpc-id vpc-0abc --subnet-ids subnet-aaa,subnet-bbb --client-cidr 10.10.0.0/16 \
  --runtime-vpc --discovery-url https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration \
  --allowed-audience api://portal-agents
python gateway/deploy.py --update-runtime-only --runtime-vpc    # after every `agentcore deploy`
```
On-prem DNS needs conditional forwarders to the Route 53 inbound endpoint for
`gateway.bedrock-agentcore.<region>.amazonaws.com` and `s3.<region>.amazonaws.com`.

## Try it
```bash
export CSP_TOKEN=<access token from your SSO>          # or use the Cognito test user from deploy.py
python gateway/portal_client.py coe --doc-type solution-design --attach whiteboard.jpg --session pay-2026-001 \
    "新的支付閘道上雲，需要 DX 備援與 PrivateLink，RTO 1 小時"
python gateway/portal_client.py coe --doc-type solution-design --session pay-2026-001 "補上成本估算，月預算 8000 USD"
python gateway/portal_client.py aicoe --mode review --doc-type use-case-assessment --attach proposal.docx "請依檢核表審查"
python gateway/portal_client.py terraform --architecture-job <csp job_id> --title payments-network "network layer" --publish
python gateway/portal_client.py checks coe            # foreign attachment / busy session / foreign job / bad upload type
```

## Verify on the first deploy
Not run against AWS yet (offline tests only). Check these:
1. **`networkConfiguration` VPC mode via `update_agent_runtime`.** Confirm the runtime ENIs appear in the
   subnets, and that Bedrock / S3 / DynamoDB / tools-gateway calls succeed with no NAT.
2. **S3 interface endpoint with `PrivateDnsOnlyForInboundResolverEndpoint`.** The S3 gateway endpoint must
   exist first. Test a presigned PUT from on-prem, and confirm that one from the internet is denied.
3. **`metadataConfiguration.allowedRequestHeaders` on the git-pr Lambda target.** Both `x-user-alias` and
   `x-agent-kind` must reach the Lambda, or it rejects the call.
4. **JWT claims from your IdP.** `ALIAS_CLAIMS` on the interceptor (default
   `alias,username,cognito:username,preferred_username,upn`), and `allowedAudience` vs `allowedClients` for your
   token type.
5. **Gateway response size.** A finished job returns the full document or module in the status response (6 MB
   gateway limit). Very large Terraform outputs should be split.
