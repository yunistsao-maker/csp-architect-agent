"""COE / AICOE document agents: prepare a document from the organisation's template, or review one against its checklist.

Templates and checklists live in templates/<domain>/templates.yaml and checklist.yaml. The same files drive the
prompt and the validation, so a generated document must contain every required heading, and a review must give
every applicable checklist item a status. Up to MAX_REPAIR_TURNS extra turns fix what validation finds.

Payload (on top of the portal actions in portal/runtime.py):
    {"prompt": "...", "mode": "prepare" | "review", "doc_type": "solution-design", "title"?: "...",
     "language"?: "Traditional Chinese", "document"?: "<markdown to review>", "attachments"?: [...],
     "session_id"?: "..."}    # same session_id = keep refining the same draft over several turns
"""

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from strands import Agent
from strands.models import BedrockModel

from portal.model import MAX_REPAIR_TURNS, OutputTruncated, make_model, new_usage, run_turn
from portal.runtime import PortalAgentApp, RunContext, error

TEMPLATES = Path(__file__).resolve().parent / "templates"
MODES = ("prepare", "review")
VERDICTS = ("approve", "approve_with_conditions", "revise")
STATUSES = ("pass", "partial", "fail", "n/a")
SEVERITIES = ("high", "medium", "low")
DOC_RE = re.compile(r"<document>\s*(.*?)\s*</document>", re.S)
QUESTIONS_RE = re.compile(r"<open_questions>\s*(.*?)\s*</open_questions>", re.S)
REVIEW_RE = re.compile(r"<review>\s*(.*?)\s*</review>", re.S)


# ---------------------------------------------------------------------------------------------- templates
@lru_cache(maxsize=None)
def load_domain(domain: str) -> dict:
    base = TEMPLATES / domain
    cfg = yaml.safe_load((base / "templates.yaml").read_text(encoding="utf-8"))
    checklist = yaml.safe_load((base / "checklist.yaml").read_text(encoding="utf-8"))["items"]
    ids = [i["id"] for i in checklist]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{domain}: duplicate checklist ids")
    for dt, t in cfg["doc_types"].items():
        headings = [s["heading"] for s in t["sections"]]
        if len(headings) != len(set(headings)):
            raise ValueError(f"{domain}/{dt}: duplicate headings")
    for item in checklist:
        unknown = set(item["applies_to"]) - set(cfg["doc_types"]) - {"all"}
        if unknown:
            raise ValueError(f"{domain} checklist {item['id']}: unknown doc types {sorted(unknown)}")
    cfg["checklist"] = checklist
    return cfg


def checklist_for(cfg: dict, doc_type: str) -> list[dict]:
    return [i for i in cfg["checklist"] if "all" in i["applies_to"] or doc_type in i["applies_to"]]


# ---------------------------------------------------------------------------------------------- prompts
def _sections_text(t: dict) -> str:
    return "\n".join(f"## {s['heading']}{'' if s['required'] else '  (optional)'}\n   {s['guidance']}"
                     for s in t["sections"])


def _checklist_text(items: list[dict]) -> str:
    return "\n".join(f"- {i['id']} [{i['area']}, default severity {i['severity']}]: {i['check']}" for i in items)


def system_prompt(cfg: dict, doc_type: str, mode: str, language: str | None) -> str:
    t = cfg["doc_types"][doc_type]
    lang = language or cfg.get("default_language", "the user's language")
    refs = "".join(f"\n- {r}" for r in cfg.get("references", []))
    common = (f"You work for the {cfg['name']} of an enterprise. Document type: {t['title']}.\n"
              f"Purpose: {t['purpose'].strip()}\nWrite in {lang}; keep the template headings exactly as given.\n"
              + (f"Reference frameworks:{refs}\n" if refs else ""))
    if mode == "prepare":
        return common + f"""
TASK: prepare (or revise) this document from the user's input and any attached files (diagrams, screenshots,
existing documents). In a continuing conversation, apply the user's feedback to your previous draft and output the
complete revised document again.

TEMPLATE (use these headings verbatim, as level-2 Markdown headings, in this order):
{_sections_text(t)}

RULES
- Never invent facts (names, numbers, dates, approvals, CIDRs, costs). Where information is missing, write
  【待確認】 with what is needed, and list it under the open questions section and in <open_questions>.
- An optional section may say "不適用 (N/A)" with a one-line reason. A required section may not be omitted.
- Before finishing, check your draft against the reviewers' checklist below and address what applies:
{_checklist_text(checklist_for(cfg, doc_type))}

OUTPUT exactly:
<document>
# <document title>
## <first heading>
...
</document>
<open_questions>["question 1", "question 2"]</open_questions>
Then at most three sentences summarising what you changed or still need."""
    return common + f"""
TASK: review the document the user provides (inline in <document_to_review> or as an attached file) for the
{cfg['name']}. Judge only what the document says; quote or cite the section as evidence.

CHECKLIST (give EVERY item a status: pass | partial | fail | n/a, with evidence):
{_checklist_text(checklist_for(cfg, doc_type))}

Also check that the document follows the template headings:
{_sections_text(t)}

VERDICT: approve (no high or medium findings), approve_with_conditions (no high findings), or revise.
Findings: one per problem, severity high | medium | low, referencing a checklist id (or "TEMPLATE" / "OTHER"),
the section, the issue, and a concrete recommendation.

OUTPUT exactly one JSON object inside <review></review>, written in {lang}:
<review>{{"verdict": "...", "summary": "...",
 "checklist": [{{"id": "GOV-01", "status": "pass", "evidence": "..."}}],
 "findings": [{{"id": "F1", "checklist_id": "NET-01", "severity": "high", "section": "6. ...",
               "issue": "...", "recommendation": "..."}}]}}</review>"""


# ---------------------------------------------------------------------------------------------- validation
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def check_document(cfg: dict, doc_type: str, document: str) -> dict:
    headings = {_norm(m) for m in re.findall(r"^#{2,3}\s+(.+?)\s*#*\s*$", document or "", re.M)}
    missing, optional_missing = [], []
    for s in cfg["doc_types"][doc_type]["sections"]:
        if _norm(s["heading"]) not in headings:
            (missing if s["required"] else optional_missing).append(s["heading"])
    errors = [] if document else ["No <document> block in the reply."]
    return {"ok": not errors and not missing, "errors": errors, "missing_sections": missing,
            "optional_missing": optional_missing}


def parse_review(text: str) -> tuple[dict | None, list[str]]:
    m = REVIEW_RE.search(text or "")
    if not m:
        return None, ["No <review> block in the reply."]
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", m.group(1).strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, [f"<review> is not valid JSON: {e}"]
    return (data, []) if isinstance(data, dict) else (None, ["<review> must be a JSON object."])


def check_review(cfg: dict, doc_type: str, review: dict | None, parse_errors: list[str]) -> dict:
    if review is None:
        return {"ok": False, "errors": parse_errors}
    errors = []
    items = {i["id"]: i for i in checklist_for(cfg, doc_type)}
    if review.get("verdict") not in VERDICTS:
        errors.append(f"verdict must be one of {', '.join(VERDICTS)}")
    statuses = {}
    for row in review.get("checklist") or []:
        cid = row.get("id")
        if cid not in items:
            errors.append(f"checklist id {cid!r} is not in the checklist for {doc_type}")
        elif row.get("status") not in STATUSES:
            errors.append(f"{cid}: status must be one of {', '.join(STATUSES)}")
        else:
            statuses[cid] = row["status"]
    missing = [cid for cid in items if cid not in statuses]
    if missing:
        errors.append(f"checklist items without a status: {', '.join(missing)}")
    sev = []
    for f in review.get("findings") or []:
        if f.get("severity") not in SEVERITIES:
            errors.append(f"finding {f.get('id')}: severity must be one of {', '.join(SEVERITIES)}")
        if f.get("checklist_id") not in items and f.get("checklist_id") not in ("TEMPLATE", "OTHER"):
            errors.append(f"finding {f.get('id')}: unknown checklist_id {f.get('checklist_id')!r}")
        if not f.get("recommendation"):
            errors.append(f"finding {f.get('id')}: missing recommendation")
        sev.append(f.get("severity"))
    for cid, st in statuses.items():
        if st in ("fail", "partial") and not any(f.get("checklist_id") == cid for f in review.get("findings") or []):
            errors.append(f"{cid} is {st} but has no finding")
    verdict = review.get("verdict")
    if verdict == "approve" and ({"high", "medium"} & set(sev)):
        errors.append("verdict 'approve' is not allowed with high or medium findings")
    if verdict == "approve_with_conditions" and "high" in sev:
        errors.append("verdict 'approve_with_conditions' is not allowed with high findings")
    return {"ok": not errors, "errors": errors,
            "counts": {s: sev.count(s) for s in SEVERITIES}, "verdict": verdict}


def render_review(cfg: dict, doc_type: str, review: dict, title: str) -> str:
    items = {i["id"]: i for i in checklist_for(cfg, doc_type)}
    lines = [f"# {cfg['name']} review: {title}", "",
             f"- Document type: {cfg['doc_types'][doc_type]['title']}",
             f"- Verdict: **{review.get('verdict')}**", "", review.get("summary", ""), "",
             "## Findings", "", "| ID | Severity | Checklist | Section | Issue | Recommendation |", "|---|---|---|---|---|---|"]
    esc = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
    for f in sorted(review.get("findings") or [], key=lambda f: SEVERITIES.index(f.get("severity", "low"))):
        lines.append(f"| {esc(f.get('id'))} | {esc(f.get('severity'))} | {esc(f.get('checklist_id'))} | "
                     f"{esc(f.get('section'))} | {esc(f.get('issue'))} | {esc(f.get('recommendation'))} |")
    lines += ["", "## Checklist", "", "| ID | Area | Check | Status | Evidence |", "|---|---|---|---|---|"]
    for row in review.get("checklist") or []:
        item = items.get(row.get("id"), {})
        lines.append(f"| {esc(row.get('id'))} | {esc(item.get('area'))} | {esc(item.get('check'))} | "
                     f"{esc(row.get('status'))} | {esc(row.get('evidence'))} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- agent
def slug(text: str | None, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]
    return s or fallback


def validate_payload(domain: str, payload: dict) -> dict | None:
    cfg = load_domain(domain)
    mode = payload.get("mode", "prepare")
    if mode not in MODES:
        return error(f"Invalid mode '{mode}'. Use one of: {', '.join(MODES)}", "bad_request")
    doc_type = payload.get("doc_type")
    if doc_type not in cfg["doc_types"]:
        return error(f"Invalid doc_type '{doc_type}'. Use one of: {', '.join(cfg['doc_types'])}", "bad_request")
    if mode == "review" and not (payload.get("document") or payload.get("attachments") or payload.get("session_id")):
        return error("Review needs the document: 'document' (text) or an uploaded file in 'attachments'.",
                     "bad_request")
    return None


def run(domain: str, payload: dict, ctx: RunContext) -> dict:
    cfg = load_domain(domain)
    mode, doc_type = payload.get("mode", "prepare"), payload["doc_type"]
    agent = Agent(model=make_model(BedrockModel), agent_id=domain,
                  system_prompt=system_prompt(cfg, doc_type, mode, payload.get("language")),
                  session_manager=ctx.session_manager, callback_handler=None)
    text = payload["prompt"]
    if payload.get("document"):
        text += f"\n\n<document_to_review>\n{payload['document']}\n</document_to_review>"
    usage = new_usage()
    try:
        reply = run_turn(agent, ctx.user_message(text), usage, ctx.session_manager)
        for turn in range(MAX_REPAIR_TURNS + 1):
            if mode == "prepare":
                m = DOC_RE.search(reply)
                document = m.group(1).strip() if m else ""
                checks = check_document(cfg, doc_type, document)
                problems = checks["errors"] + [f"missing required section heading: ## {h}" for h in checks["missing_sections"]]
            else:
                review, parse_errors = parse_review(reply)
                checks = check_review(cfg, doc_type, review, parse_errors)
                problems = checks["errors"]
            if not problems or turn == MAX_REPAIR_TURNS:
                break
            reply = run_turn(agent, "Your reply failed validation. Fix these problems and output the complete "
                             "result again in the same format:\n- " + "\n- ".join(problems), usage, ctx.session_manager)
        checks["repair_turns"] = turn
    except OutputTruncated:
        return error("The document exceeded the model's output limit; ask for fewer sections per turn.", "max_tokens",
                     mode=mode, doc_type=doc_type)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = slug(payload.get("title"), (ctx.job_id or "draft")[:8])
    base = {"mode": mode, "doc_type": doc_type, "checks": checks, "usage": usage, "publishable": checks["ok"]}
    if mode == "prepare":
        m = QUESTIONS_RE.search(reply)
        try:
            questions = json.loads(m.group(1)) if m else []
        except json.JSONDecodeError:
            questions = [q.strip("-• ").strip() for q in m.group(1).splitlines() if q.strip()]
        title = payload.get("title") or (re.search(r"^#\s+(.+)$", document, re.M) or [None, doc_type])[1]
        path = f"{cfg['output_dir']}/{doc_type}/{date}-{name}.md"
        return {**base, "title": f"[{cfg['name']}] {title}", "document": document, "open_questions": questions,
                "files": {path: document + "\n"} if document else {},
                "summary": f"{cfg['doc_types'][doc_type]['title']} draft; {len(questions)} open question(s)."}
    title = payload.get("title") or doc_type
    report = render_review(cfg, doc_type, review, title) if review else ""
    path = f"{cfg['output_dir']}/reviews/{doc_type}/{date}-{name}-review.md"
    return {**base, "title": f"[{cfg['name']} review] {title}", "verdict": (review or {}).get("verdict"),
            "review": review, "report": report, "files": {path: report} if report else {},
            "summary": f"Verdict: {(review or {}).get('verdict')}; findings {checks.get('counts')}."}


def build_portal_app(domain: str, app=None) -> PortalAgentApp:
    load_domain(domain)  # fail fast on a broken template
    return PortalAgentApp(domain, handler=lambda p, ctx: run(domain, p, ctx),
                          validate=lambda p: validate_payload(domain, p), app=app)
