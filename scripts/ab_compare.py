"""A/B test: per-mode system prompt ("prompt") vs on-demand Agent Skills ("skills") on evals/usecases.json.

Runs every use case in both variants against a local server (`python agent.py`) or the deployed AgentCore runtime,
then scores each diagram: validation, requirement coverage (evals/rubric.py), tokens, model/tool calls, latency.

    python scripts/ab_compare.py                         # local server on http://localhost:8080
    python scripts/ab_compare.py --runtime               # deployed runtime from .bedrock_agentcore.yaml
    python scripts/ab_compare.py --only uc2-azure-manufacturing --repeat 3

Writes evals/results/<timestamp>/ (diagrams, raw responses, results.json, summary.md).
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
from rubric import score  # noqa: E402

VARIANTS = {"prompt": False, "skills": True}


def call_local(url: str, payload: dict, session: str) -> dict:
    req = urllib.request.Request(f"{url}/invocations", data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session})
    with urllib.request.urlopen(req, timeout=1200) as resp:
        return json.load(resp)


def runtime_caller():
    import boto3
    import yaml
    from botocore.config import Config

    cfg = yaml.safe_load(open(ROOT / ".bedrock_agentcore.yaml"))
    agent_cfg = cfg["agents"][cfg["default_agent"]]
    client = boto3.client("bedrock-agentcore", region_name=agent_cfg["aws"]["region"],
                          config=Config(read_timeout=1200, retries={"max_attempts": 1}))
    arn = agent_cfg["bedrock_agentcore"]["agent_arn"]

    def call(payload: dict, session: str) -> dict:
        resp = client.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session, payload=json.dumps(payload))
        return json.loads(resp["response"].read())
    return call


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--runtime", action="store_true", help="call the deployed AgentCore runtime instead of --url")
    ap.add_argument("--only", help="run a single use case id")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--variants", default="prompt,skills")
    args = ap.parse_args()

    call = runtime_caller() if args.runtime else (lambda p, s: call_local(args.url, p, s))
    out_dir = ROOT / "evals/results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    usecases = [u for u in json.loads((ROOT / "evals/usecases.json").read_text()) if not args.only or u["id"] == args.only]
    rows = []
    for uc in usecases:
        for variant in args.variants.split(","):
            for i in range(args.repeat):
                session = f"ab-{uc['id']}-{variant}-{uuid.uuid4()}"
                payload = {"prompt": uc["prompt"], "mode": uc["mode"], "skills": VARIANTS[variant]}
                print(f"▶ {uc['id']} [{variant}] #{i + 1} …", flush=True)
                t0 = time.time()
                try:
                    data = call(payload, session)
                except Exception as e:  # noqa: BLE001 - record and continue
                    data = {"error": f"{type(e).__name__}: {e}"}
                secs = round(time.time() - t0, 1)
                stem = out_dir / f"{uc['id']}.{variant}.{i + 1}"
                stem.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
                stem.with_suffix(".drawio").write_text(data.get("xml") or "")
                v, u = data.get("validation") or {}, data.get("usage") or {}
                cov = score(data.get("xml") or "", data.get("explanation") or "", uc["id"]) if data.get("xml") else None
                row = {"usecase": uc["id"], "variant": variant, "run": i + 1, "seconds": secs,
                       "error": data.get("error_code") or data.get("error"),
                       "valid": v.get("ok"), "icon_errors": len(v.get("icon_errors", [])),
                       "auto_fixed": sum(x["count"] for x in v.get("auto_fixed", [])),
                       "repair_turns": v.get("repair_turns"), "pages": v.get("pages"),
                       "vertices": v.get("vertices"), "edges": v.get("edges"),
                       "coverage_pct": cov and cov["coverage_pct"], "missing": cov and cov["missing"],
                       "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                       "model_calls": u.get("model_calls"), "tool_calls": u.get("tool_calls"),
                       "skills_activated": u.get("skills_activated"), "files_read": u.get("files_read")}
                rows.append(row)
                print(f"  {secs}s valid={row['valid']} coverage={row['coverage_pct']}% "
                      f"tokens={row['input_tokens']}/{row['output_tokens']} calls={row['model_calls']} "
                      f"{('ERROR ' + str(row['error'])) if row['error'] else ''}", flush=True)

    (out_dir / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    lines = ["| Use case | Variant | Runs | Valid | Coverage % | Auto-fixed icons | Repair turns | Input tok | Output tok | Model calls | Seconds |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]

    def mean(xs):
        xs = [x for x in xs if isinstance(x, (int, float))]
        return round(statistics.mean(xs), 1) if xs else "–"
    for uc in usecases:
        for variant in args.variants.split(","):
            rs = [r for r in rows if r["usecase"] == uc["id"] and r["variant"] == variant]
            lines.append(f"| {uc['id']} | {variant} | {len(rs)} | {sum(bool(r['valid']) for r in rs)}/{len(rs)} | "
                         f"{mean(r['coverage_pct'] for r in rs)} | {mean(r['auto_fixed'] for r in rs)} | "
                         f"{mean(r['repair_turns'] for r in rs)} | {mean(r['input_tokens'] for r in rs)} | "
                         f"{mean(r['output_tokens'] for r in rs)} | {mean(r['model_calls'] for r in rs)} | "
                         f"{mean(r['seconds'] for r in rs)} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines) + f"\n\nSaved {out_dir}")


if __name__ == "__main__":
    main()
