"""Terraform agent (AgentCore Runtime entrypoint): requirements, an uploaded diagram or a CSP architect job -> a
Terraform module that passes terraform_checks.py, published as a pull request to the infrastructure repository.

Payload (on top of the portal actions in portal/runtime.py):
    {"prompt": "...", "title"?: "payments-network", "architecture_job_id"?: "<job id from the CSP architect agent>",
     "attachments"?: [...], "session_id"?: "..."}

The PR is the gate: terraform fmt / validate / plan and human review run in the repository's CI, never here.
The agent has no cloud credentials beyond Bedrock, its session table, uploads and the tools gateway.

    agentcore configure -e terraform_agent.py -n terraform_agent -r us-east-1 -rf requirements.txt \\
      -dt direct_code_deploy -rt PYTHON_3_12 -dm -ni
"""

import html
import os
import re
import xml.etree.ElementTree as ET

from strands import Agent
from strands.models import BedrockModel

from drawio_validator import _pages
from jobs import JOB_ID_RE
from portal.files import extract_files, strip_files
from portal.model import OutputTruncated, make_model, new_usage, run_turn
from portal.runtime import PortalAgentApp, RunContext, error
from terraform_checks import check, problems_for_repair, standards

KIND = "terraform"
OUTPUT_ROOT = os.environ.get("TF_OUTPUT_ROOT", "terraform")
MAX_REPAIR_TURNS = int(os.environ.get("TF_MAX_REPAIR_TURNS", "2"))
ALLOWED_SUFFIXES = (".tf", ".md", ".tfvars.example", ".tftest.hcl", ".json", ".tpl", ".yaml", ".yml")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def system_prompt() -> str:
    std = standards()
    return f"""You are a senior platform engineer writing production Terraform for an enterprise with strictly private
networks (on-prem connected over Direct Connect + Transit Gateway; no internet paths in workload VPCs).

STANDARDS (mandatory; automated checks reject violations):
{std['conventions']}
Waivers: if the user explicitly requires something the standards forbid, keep it, and put
`# tfcheck:allow <RULE> <reason>` on the line above, and explain it in README.md.

INPUT may include requirements, an architecture summary from the CSP architect agent (<architecture>), and attached
diagrams or documents. Unknown values (CIDRs, account IDs, names) become variables with clear descriptions;
never invent real IDs. In a continuing conversation, apply the feedback and output the complete module again.

OUTPUT: every file as
<file path="relative/path.tf">
...file content...
</file>
Paths are relative to the module root (e.g. versions.tf, modules/network/main.tf, envs/prod/main.tf).
Allowed file types: {', '.join(ALLOWED_SUFFIXES)}. After the files, write a short summary (what is created,
assumptions, what to review)."""


def summarize_drawio(xml: str, limit: int = 400) -> str:
    """Compact component / connection list of a draw.io diagram (far fewer tokens than the XML)."""
    lines = []
    for page, gm in _pages(ET.fromstring(xml)):
        labels, rows, edges = {}, [], []
        root = gm.find("root")
        for el in (root if root is not None else []):
            cell = el if el.tag == "mxCell" else el.find("mxCell")
            if cell is None:
                continue
            cid = el.get("id")
            label = el.get("label") if el.tag != "mxCell" else cell.get("value")
            label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(label or ""))).strip()
            labels[cid] = label
            style = cell.get("style") or ""
            icon = re.search(r"(?:resIcon|grIcon|prIcon)=([\w.]+)|shape=mxgraph\.aws4\.(\w+)|azure2/([^;\"]+)", style)
            if cell.get("edge") == "1":
                edges.append((cell.get("source"), cell.get("target"), label))
            elif cell.get("vertex") == "1" and (label or icon):
                svc = next((g for g in icon.groups() if g), "") if icon else ""
                svc = svc.rsplit("/", 1)[-1][:-4] if svc.endswith(".svg") else svc.split(".")[-1]
                rows.append((cid, label, svc, cell.get("parent")))
        lines.append(f"## Page: {page}")
        for cid, label, svc, parent in rows:
            where = f" (in: {labels.get(parent)})" if labels.get(parent) else ""
            lines.append(f"- {label or '(no label)'}{f' [{svc}]' if svc else ''}{where}")
        for s, t, label in edges:
            lines.append(f"- {labels.get(s) or s} -> {labels.get(t) or t}{f': {label}' if label else ''}")
    return "\n".join(lines[:limit])


def validate_payload(payload: dict) -> dict | None:
    title = payload.get("title")
    if title is not None and not SLUG_RE.match(str(title)):
        return error("title must be a slug: lowercase letters, digits and '-' (2-63 characters).", "bad_request")
    job = payload.get("architecture_job_id")
    if job is not None and not JOB_ID_RE.match(str(job)):
        return error("architecture_job_id is not a job id.", "bad_request")
    return None


def architecture_context(portal: PortalAgentApp, job_id: str, alias: str | None) -> str:
    """The CSP architect job's diagram, if it belongs to the same caller and succeeded."""
    record = portal.job_store.get(job_id)
    result = (record or {}).get("result") or {}
    if not record or record.get("alias") != alias or record.get("status") != "SUCCEEDED" or not result.get("xml"):
        raise LookupError(f"Architecture job '{job_id}' was not found (or has not succeeded).")
    return (f"<architecture source=\"csp-architect job {job_id}\">\n{summarize_drawio(result['xml'])}\n\n"
            f"Design notes:\n{(result.get('explanation') or '')[:12000]}\n</architecture>")


def run(portal: PortalAgentApp, payload: dict, ctx: RunContext) -> dict:
    text = payload["prompt"]
    if payload.get("architecture_job_id"):
        try:
            text += "\n\n" + architecture_context(portal, payload["architecture_job_id"], ctx.alias)
        except LookupError as e:
            return error(str(e), "not_found")
    agent = Agent(model=make_model(BedrockModel), agent_id=KIND, system_prompt=system_prompt(),
                  session_manager=ctx.session_manager, callback_handler=None)
    usage = new_usage()
    try:
        reply = run_turn(agent, ctx.user_message(text), usage, ctx.session_manager)
        for turn in range(MAX_REPAIR_TURNS + 1):
            files, path_errors = extract_files(reply, ALLOWED_SUFFIXES)
            report = check(files)
            problems = path_errors + problems_for_repair(report)
            if not problems or turn == MAX_REPAIR_TURNS:
                break
            reply = run_turn(agent, "The generated files failed the automated checks. Fix every problem and output "
                             "ALL files again (unchanged ones too):\n- " + "\n- ".join(problems), usage, ctx.session_manager)
    except OutputTruncated:
        return error("The module exceeded the model's output limit; split it (e.g. network first, then compute).",
                     "max_tokens")
    report["repair_turns"], report["path_errors"] = turn, path_errors
    name = payload.get("title") or f"module-{(ctx.job_id or 'draft')[:8]}"
    prefixed = {f"{OUTPUT_ROOT}/{name}/{p}": c for p, c in files.items()}
    summary = strip_files(reply)
    waived = [f for f in report["findings"] if f["severity"] == "waived"]
    if waived:
        summary += "\n\nWaived checks:\n" + "\n".join(f"- {f['rule']} {f['file']}: {f['waived']}" for f in waived)
    return {"title": f"[terraform] {name}", "files": prefixed, "checks": report, "summary": summary,
            "publishable": report["ok"] and not path_errors and bool(files), "usage": usage,
            "standards": {"required_tags": standards().get("required_tags")}}


portal = PortalAgentApp(KIND, handler=lambda p, ctx: run(portal, p, ctx), validate=validate_payload)
app = portal.app


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    return portal.invoke(payload, context)


if __name__ == "__main__":
    app.run()

