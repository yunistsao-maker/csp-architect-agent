"""Fill mode for the COE / AICOE agents: answer an organisation's form (questionnaire or checklist) for a project.

The form comes either in the payload ("form", e.g. a portal's built-in form or the answers so far) or as an attached
file the agent reads and turns into items first. The agent answers from the project files the portal sends
("context"), the attachments and the user's messages, gives every answer a confidence, and asks for what it cannot
support. Follow-up turns send the current form back and change only the items the user's message affects.

Payload (on top of the portal actions in portal/runtime.py):
    {"mode": "fill", "prompt": "...", "form"?: {"name", "title", "items": [...]}, "context"?: "PROJECT FILES ...",
     "attachments"?: [upload keys: the form file, screenshots], "language"?: "zh-TW" | "en" | "...", "session_id"?}

Item fields (all optional except id and q):
    form:    id, section, q, type (yn | single | multi | text), options, other, guide (fill-in instructions),
             choices (allowed yn answers: Y* / Y / N / NA), expect (compliant yn answer), veto, reject (option indexes
             that trigger a veto), high_risk, detail (label of the reason / evidence field)
    answers: answer (yn), selected (single: index, multi: list of indexes), other_text, text (text items),
             note (the reason / evidence), confidence (0-100), basis (source), exception (exception note)

Result: {"form": {..., "items": [form + answer fields]}, "summary", "ask": [questions], "updated": [ids],
         "checks", "files": {docs/<domain>/forms/<date>-<name>.md: answers table}}
The agent's reply carries only ids and answer fields when the form was sent; the form fields come from the payload.
"""

import json
import re
from datetime import datetime, timezone

from strands import Agent

from portal.model import MAX_REPAIR_TURNS, OutputTruncated, make_model, new_usage, run_turn
from portal.runtime import RunContext, error

TYPES = ("yn", "single", "multi", "text")
YN = ("Y*", "Y", "N", "NA")
FORM_FIELDS = ("section", "q", "type", "options", "other", "guide", "choices", "expect", "veto", "reject", "high_risk",
               "detail")
ANSWER_FIELDS = ("answer", "selected", "other_text", "text", "note", "confidence", "basis", "exception")
FILL_RE = re.compile(r"<fill>\s*(.*?)\s*</fill>", re.S)
LANGUAGES = {"zh-tw": "Traditional Chinese", "zh-hant": "Traditional Chinese", "zh": "Traditional Chinese",
             "zh-cn": "Simplified Chinese", "en": "English", "ja": "Japanese"}
MAX_ITEMS = 300
MAX_FORM_CHARS = 400_000
MAX_CONTEXT_CHARS = 200_000


class FormError(ValueError):
    pass


def language_name(language: str | None, default: str) -> str:
    if not language:
        return default
    return LANGUAGES.get(str(language).strip().lower(), str(language)[:40])


# ---------------------------------------------------------------------------------------------- form shape
def _str(v, limit: int) -> str:
    return str(v)[:limit] if v is not None else ""


def _index(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def clean_item(raw: dict, *, with_answers: bool = True) -> dict:
    """One item from untrusted input (payload or model), reduced to the known fields with the right types."""
    it = {"id": _str(raw.get("id"), 40).strip(), "q": _str(raw.get("q"), 2000).strip()}
    t = raw.get("type")
    it["type"] = t if t in TYPES else ("single" if raw.get("options") else "text")
    for k in ("section", "guide", "detail"):
        if raw.get(k):
            it[k] = _str(raw[k], 4000)
    if isinstance(raw.get("options"), list):
        it["options"] = [_str(o, 300) for o in raw["options"]][:50]
    if it["type"] in ("single", "multi") and not it.get("options"):
        it["type"] = "text"
    if it["type"] == "yn":
        choices = list(dict.fromkeys(c for c in raw.get("choices") or [] if c in YN))
        it["choices"] = choices or ["Y", "N", "NA"]
    for k in ("other", "veto", "high_risk"):
        if raw.get(k):
            it[k] = True
    if raw.get("expect") in ("Y", "N"):
        it["expect"] = raw["expect"]
    if isinstance(raw.get("reject"), list):
        it["reject"] = sorted({i for i in raw["reject"] if _index(i)})
    if with_answers:
        it.update(clean_answer(it, raw))
    return it


def clean_answer(item: dict, raw: dict) -> dict:
    """The answer fields of raw that are valid for item. Missing keys stay missing (= unchanged on merge)."""
    out = {}
    if "answer" in raw:
        out["answer"] = raw["answer"] if item["type"] == "yn" and raw["answer"] in item.get("choices", YN) else None
    if "selected" in raw:
        n = len(item.get("options") or [])
        sel = raw["selected"]
        if item["type"] == "single":
            out["selected"] = sel if _index(sel) and sel < n else None
        elif item["type"] == "multi":
            sel = sel if isinstance(sel, list) else [sel]
            out["selected"] = sorted({i for i in sel if _index(i) and i < n})
    for k, limit in (("other_text", 2000), ("text", 8000), ("note", 4000), ("basis", 500), ("exception", 4000)):
        if k in raw:
            out[k] = _str(raw[k], limit) if raw[k] is not None else ""
    if "confidence" in raw and not isinstance(raw["confidence"], bool):
        try:
            out["confidence"] = max(0, min(100, int(raw["confidence"])))
        except (TypeError, ValueError):
            pass
    return out


def normalize_form(form) -> dict:
    """The payload's form, validated: unique ids, at most MAX_ITEMS items. Raises FormError."""
    if not isinstance(form, dict) or not isinstance(form.get("items"), list):
        raise FormError("'form' must be an object with an 'items' list.")
    if len(json.dumps(form, ensure_ascii=False)) > MAX_FORM_CHARS:
        raise FormError(f"'form' is larger than {MAX_FORM_CHARS} characters.")
    items = [clean_item(i) for i in form["items"] if isinstance(i, dict)]
    items = [i for i in items if i["id"] and i["q"]]
    if not items:
        raise FormError("'form' has no items with an id and a question.")
    if len(items) > MAX_ITEMS:
        raise FormError(f"'form' has more than {MAX_ITEMS} items.")
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        raise FormError("'form' item ids must be unique.")
    out = {k: _str(form[k], 300) for k in ("name", "title") if form.get(k)}
    if isinstance(form.get("answers"), dict):
        out["answers"] = {_str(k, 10): _str(v, 200) for k, v in list(form["answers"].items())[:10]}
    if form.get("source"):
        out["source"] = _str(form["source"], 1000)
    return {**out, "items": items}


def answered(item: dict) -> bool:
    return any(item.get(k) not in (None, "", []) for k in ANSWER_FIELDS)


def answer_text(it: dict) -> str:
    if it["type"] == "yn":
        return it.get("answer") or ""
    if it["type"] == "text":
        return it.get("text") or ""
    sel = it.get("selected")
    sel = sel if isinstance(sel, list) else [] if sel is None else [sel]
    picked = [it["options"][i] for i in sel if 0 <= i < len(it.get("options") or [])]
    if it.get("other_text"):
        picked.append(f"Other: {it['other_text']}")
    return ", ".join(picked)


def flag(it: dict) -> str | None:
    """veto (rejects the request), exception (not the compliant answer), schedule (Y*) or None."""
    a = it.get("answer")
    against = it["type"] == "yn" and it.get("expect") and a and a not in ("NA", "Y*") and a != it["expect"]
    sel = it.get("selected")
    sel = set(sel if isinstance(sel, list) else [] if sel is None else [sel])
    if it.get("veto") and (against or sel & set(it.get("reject") or [])):
        return "veto"
    if against:
        return "exception"
    return "schedule" if a == "Y*" else None


# ---------------------------------------------------------------------------------------------- prompt
def _form_json(form: dict) -> str:
    keep = ("id",) + FORM_FIELDS + ANSWER_FIELDS
    items = [{k: v for k, v in i.items() if k in keep and v not in (None, "", [], False)} for i in form["items"]]
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def system_prompt(cfg: dict, language: str, form: dict | None, context: str) -> str:
    rules = f"""You fill in forms for the {cfg['name']} of an enterprise: questionnaires and self-check lists that a project
team submits before go-live. You answer for the user's project. Write every free-text field in {language}.

SOURCES: the PROJECT FILES below (architecture diagrams, explanations, requirement documents, other agents' output),
the attached files and images, and what the user tells you in this conversation. Nothing else.

RULES
- Never invent facts (names, numbers, dates, approvals, vendors, regions, controls). If the sources do not support an
  answer, leave it empty (answer / selected null, text ""), set confidence 40 or lower, and add a question to "ask".
- Follow each item's guide: it defines what each answer means (e.g. Y* = capable but not done yet, give the schedule;
  when NA applies) and what the reason / evidence must contain.
- yn: "answer" is one of the item's choices (default Y, N, NA). single: "selected" is one option index (0-based).
  multi: "selected" is a list of indexes. Use "other_text" only when the item allows other and no option fits.
  text: "text".
- "note": the reason / evidence the form asks for (the item's detail field), concise, citing the source.
- "confidence": 0-100, how likely the answer is right given the sources. 80+ only when a source states it directly;
  50-79 when it follows from the sources; below 50 when you are guessing.
- "basis": the source you used (a file name, "user", or an attachment name); "" when none.
- expect / veto / high_risk: answer what is true. Never choose the compliant answer without evidence; an unsupported
  veto or high-risk item stays empty with a question. When the answer is not the expected one, or is Y*, draft
  "exception" (the exception note: why, compensating controls, schedule) only from facts in the sources; otherwise
  leave it "" and ask.
- "ask": at most 8 short questions for the user about missing or low-confidence items, each starting with the item id.
"""
    if form is None:
        task = """TASK: read the form in the attached file(s) and fill it in. First list EVERY item of the form, in order:
- id: the form's own numbering; if it has none, S01, S02, ...
- section: the heading the item sits under; q: the question as written (keep both languages if the form has two)
- type, options, other: from the form's answer fields; choices: the yn answers the form allows (e.g. Y*, Y, N, NA)
- guide: the form's fill-in instructions for the item, verbatim; detail: the label of its reason / evidence field
- expect: the compliant yn answer when the form makes it clear; veto / high_risk: true when the form marks them
  (e.g. 否決題 / 高風險); reject: option indexes that reject the request
then add the answer fields to each item.

OUTPUT exactly:
<fill>{"title": "<form title>", "summary": "<two sentences>", "ask": ["..."],
 "items": [{"id": "1", "section": "...", "q": "...", "type": "yn", "choices": ["Y", "N", "NA"], "guide": "...",
            "answer": "Y", "note": "...", "confidence": 85, "basis": "...", "exception": ""}]}</fill>"""
    elif not any(answered(i) for i in form["items"]):
        task = f"""TASK: fill in this form. Give EVERY item a row, also when you must leave the answer empty.

FORM ({form.get('title') or form.get('name') or 'form'}; JSON items with their instructions):
{_form_json(form)}

OUTPUT exactly (only ids and answer fields; do not repeat the questions or instructions):
<fill>{{"summary": "<two sentences>", "ask": ["..."],
 "items": [{{"id": "...", "answer": "Y", "note": "...", "confidence": 85, "basis": "...", "exception": ""}}]}}</fill>"""
    else:
        task = f"""TASK: the user is reviewing the form below, which is already filled in (the current answers are in the
items). Apply the user's latest message: answer their questions about items, and change only the items the message
affects (and items that depend on them). Keep everything else as it is.

FORM ({form.get('title') or form.get('name') or 'form'}; JSON items with their current answers):
{_form_json(form)}

OUTPUT exactly (only the changed items, with all their answer fields; "items": [] when nothing changes):
<fill>{{"summary": "<what you changed or answered>", "ask": ["..."],
 "items": [{{"id": "...", "answer": "N", "note": "...", "confidence": 95, "basis": "user", "exception": "..."}}]}}</fill>"""
    ctx = f"\n\n{context[:MAX_CONTEXT_CHARS]}" if context else "\n\nPROJECT FILES: none were sent."
    return rules + "\n" + task + ctx


# ---------------------------------------------------------------------------------------------- validation
def parse_fill(text: str) -> tuple[dict | None, list[str]]:
    m = FILL_RE.search(text or "")
    if not m:
        return None, ["No <fill> block in the reply."]
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", m.group(1).strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, [f"<fill> is not valid JSON: {e}"]
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None, ["<fill> must be a JSON object with an 'items' list."]
    return data, []


def check_fill(data: dict | None, parse_errors: list[str], form: dict | None) -> dict:
    if data is None:
        return {"ok": False, "errors": parse_errors}
    errors = []
    rows = data["items"]
    if not all(isinstance(r, dict) and r.get("id") not in (None, "") for r in rows):
        errors.append("every item must be an object with an 'id'")
    rows = [r for r in rows if isinstance(r, dict) and r.get("id") not in (None, "")]
    ids = [str(r["id"]) for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"duplicate item ids: {', '.join(dupes[:10])}")
    if form is None:
        if not rows:
            errors.append("no items: list every item of the attached form")
        if len(rows) > MAX_ITEMS:
            errors.append(f"at most {MAX_ITEMS} items per form; split the form")
        for r in rows:
            if not str(r.get("q") or "").strip():
                errors.append(f"{r['id']}: missing 'q' (the question)")
            if r.get("type") not in TYPES:
                errors.append(f"{r['id']}: type must be one of {', '.join(TYPES)}")
            elif r["type"] in ("single", "multi") and not r.get("options"):
                errors.append(f"{r['id']}: a {r['type']} item needs 'options'")
        items = {str(r["id"]): clean_item(r, with_answers=False) for r in rows if r.get("type") in TYPES}
    else:
        items = {i["id"]: i for i in form["items"]}
        unknown = [i for i in ids if i not in items]
        if unknown:
            errors.append(f"ids not in the form: {', '.join(unknown[:10])}")
        if not any(answered(i) for i in form["items"]):
            missing = [i for i in items if i not in ids]
            if missing:
                errors.append(f"items without a row (give every item a row, empty answer allowed): {', '.join(missing[:30])}"
                              + (" ..." if len(missing) > 30 else ""))
    for r in rows:
        it = items.get(str(r["id"]))
        if not it:
            continue
        if r.get("answer") not in (None, "") and (it["type"] != "yn" or r["answer"] not in it.get("choices", YN)):
            errors.append(f"{r['id']}: answer {r['answer']!r} is not one of {', '.join(it.get('choices') or ['-'])}")
        sel = r.get("selected")
        if sel is not None and it["type"] in ("single", "multi"):
            n = len(it.get("options") or [])
            vals = sel if isinstance(sel, list) else [sel]
            if (it["type"] == "single" and isinstance(sel, list)) or not all(isinstance(v, int) and 0 <= v < n for v in vals):
                errors.append(f"{r['id']}: selected must be {'an index' if it['type'] == 'single' else 'a list of indexes'} in 0..{n - 1}")
        c = r.get("confidence")
        if c is not None and not (isinstance(c, (int, float)) and 0 <= c <= 100):
            errors.append(f"{r['id']}: confidence must be a number 0-100")
    if not isinstance(data.get("ask", []), list):
        errors.append("'ask' must be a list of strings")
    return {"ok": not errors, "errors": errors[:40], "items": len(rows)}


def merge(form: dict | None, data: dict) -> tuple[dict, list[str]]:
    """The full filled form, and the ids whose answers changed."""
    rows = [r for r in data["items"] if isinstance(r, dict) and r.get("id") not in (None, "")]
    if form is None:
        items = [clean_item({**r, "id": str(r["id"])}) for r in rows]
        items = [i for i in items if i["id"] and i["q"]][:MAX_ITEMS]
        return {"title": _str(data.get("title"), 300) or "form", "items": items}, [i["id"] for i in items]
    by_id = {str(r["id"]): r for r in rows}
    items, updated = [], []
    for it in form["items"]:
        r = by_id.get(it["id"])
        if r is None:
            items.append(it)
            continue
        new = {**it, **clean_answer(it, r)}
        if any(new.get(k) != it.get(k) for k in ("answer", "selected", "other_text", "text", "exception")):
            updated.append(it["id"])
        items.append(new)
    return {**{k: v for k, v in form.items() if k != "items"}, "items": items}, updated


def render(cfg: dict, form: dict) -> str:
    """The answers as Markdown (no fill-in instructions, no confidence), for the pull request."""
    esc = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = [f"# {cfg['name']}: {form.get('title') or form.get('name') or 'form'}", ""]
    if form.get("source"):
        lines += [f"> {esc(form['source'])}", ""]
    lines += ["| Section | ID | Question | Answer | Reason / Evidence | Exception note |", "|---|---|---|---|---|---|"]
    for it in form["items"]:
        lines.append(f"| {esc(it.get('section'))} | {esc(it['id'])} | {esc(it['q'])} | {esc(answer_text(it)) or 'TO CONFIRM'} | "
                     f"{esc(it.get('note'))} | {esc(it.get('exception'))} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- agent
def validate(payload: dict) -> dict | None:
    if payload.get("form") is not None:
        try:
            normalize_form(payload["form"])
        except FormError as e:
            return error(str(e), "bad_request")
    elif not payload.get("attachments"):
        return error("Fill needs the form: attach the form file in 'attachments' or send it as 'form'.", "bad_request")
    if payload.get("context") is not None and not isinstance(payload["context"], str):
        return error("'context' must be a string.", "bad_request")
    return None


def run(domain: str, cfg: dict, payload: dict, ctx: RunContext, model_cls, slug) -> dict:
    form = normalize_form(payload["form"]) if payload.get("form") is not None else None
    language = language_name(payload.get("language"), cfg.get("default_language", "the user's language"))
    agent = Agent(model=make_model(model_cls), agent_id=f"{domain}-fill",
                  system_prompt=system_prompt(cfg, language, form, payload.get("context") or ""),
                  session_manager=ctx.session_manager, callback_handler=None)
    usage = new_usage()
    try:
        reply = run_turn(agent, ctx.user_message(payload["prompt"]), usage, ctx.session_manager)
        for turn in range(MAX_REPAIR_TURNS + 1):
            data, parse_errors = parse_fill(reply)
            checks = check_fill(data, parse_errors, form)
            if checks["ok"] or turn == MAX_REPAIR_TURNS:
                break
            reply = run_turn(agent, "Your reply failed validation. Fix these problems and output the complete <fill> "
                             "block again:\n- " + "\n- ".join(checks["errors"]), usage, ctx.session_manager)
        checks["repair_turns"] = turn
    except OutputTruncated:
        return error("The filled form exceeded the model's output limit; split the form or fill it in parts.",
                     "max_tokens", mode="fill")
    base = {"mode": "fill", "checks": checks, "usage": usage, "publishable": checks["ok"]}
    if data is None:
        return {**base, "form": form, "summary": "; ".join(checks["errors"]), "ask": [], "updated": [], "files": {}}
    filled, updated = merge(form, data)
    if not filled["items"]:  # an attached file the model could not turn into items
        checks = {**checks, "ok": False, "errors": checks.get("errors", []) + ["no form items found in the attachments"]}
        return {**base, "checks": checks, "publishable": False, "form": None, "summary": "No form items found in the attachments.",
                "ask": [], "updated": [], "files": {}}
    if form is None:
        filled["name"] = slug(filled["title"], (ctx.job_id or "form")[:8])
    flags = [flag(i) for i in filled["items"]]
    counts = {"items": len(filled["items"]), "answered": sum(1 for i in filled["items"] if answer_text(i)),
              "veto": flags.count("veto"), "exception": flags.count("exception"), "schedule": flags.count("schedule")}
    title = filled.get("title") or filled.get("name") or "form"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"{cfg['output_dir']}/forms/{date}-{slug(filled.get('name') or title, 'form')}.md"
    return {**base, "form": filled, "counts": counts, "updated": updated,
            "ask": [_str(a, 500) for a in data.get("ask") or [] if isinstance(a, str)][:8],
            "summary": _str(data.get("summary"), 2000) or f"{counts['answered']}/{counts['items']} items answered.",
            "title": f"[{cfg['name']} form] {title}", "files": {path: render(cfg, filled)} if checks["ok"] else {}}
