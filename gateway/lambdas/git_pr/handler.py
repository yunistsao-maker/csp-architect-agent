"""Git pull-request tool for the portal agents (Lambda target on the tools gateway, attached to the VPC).

Tool `open_pull_request` {agent, job_id, title, body, files: [{path, content}]} -> {url, number, branch, repo}.

- Reaches the internal Git server (GitLab or GitHub Enterprise Server) privately: Lambda ENIs in the agent VPC ->
  Transit Gateway -> Direct Connect -> on-prem. The token comes from Secrets Manager through its VPC endpoint.
- Who is asking comes from headers the gateway propagates: x-user-alias (set by the ingress interceptor from the
  JWT, then by agent code) and x-agent-kind (set by agent code). Every agent kind has a PROFILE: which repository,
  base branch, path prefix and file types it may write. Agents never choose the repository.
- Branch agent/<kind>/<alias>/<job>; the PR records who asked; merging stays with humans and the repository's CI.
- Idempotent: publishing the same job again returns the open PR for its branch.

Environment:
  GIT_PROVIDER=gitlab|github   GIT_API_URL=https://gitlab.corp.example/api/v4 | https://ghe.corp.example/api/v3
  GIT_TOKEN_SECRET_ARN=...     GIT_CA_BUNDLE=internal-ca.pem (optional, packaged with the function)
  GIT_PROFILES={"terraform": {"repo": "platform/infra-live", "base": "main", "path_prefix": "terraform/",
                "suffixes": [".tf", ".md", ".tfvars.example"], "labels": ["agent", "terraform"]}, "coe": {...}}
"""

import json
import os
import posixpath
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

ALIAS_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 60, 200_000, 2_000_000
_token_cache: dict[str, str] = {}


class Rejected(ValueError):
    pass


def _log(**fields):
    print(json.dumps({"component": "git-pr-tool", **fields}, ensure_ascii=False))


def caller(context) -> tuple[str | None, str | None]:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    headers = custom.get("bedrockAgentCorePropagatedHeaders") or {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except json.JSONDecodeError:
            headers = {}
    low = {str(k).lower(): v for k, v in headers.items()}
    return low.get("x-user-alias"), low.get("x-agent-kind")


def check_files(files, profile: dict) -> list[dict]:
    if not isinstance(files, list) or not files:
        raise Rejected("files must be a non-empty list")
    if len(files) > MAX_FILES:
        raise Rejected(f"at most {MAX_FILES} files")
    prefix, suffixes = profile.get("path_prefix", ""), tuple(profile.get("suffixes") or ())
    out, total = [], 0
    for f in files:
        path, content = (f or {}).get("path"), (f or {}).get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise Rejected("every file needs a string path and content")
        norm = posixpath.normpath(path)
        if norm != path or path.startswith("/") or ".." in path.split("/") or not re.fullmatch(r"[A-Za-z0-9._/\-]+", path):
            raise Rejected(f"invalid path {path!r}")
        if prefix and not path.startswith(prefix):
            raise Rejected(f"{path!r} is outside {prefix!r}")
        if suffixes and not path.endswith(suffixes):
            raise Rejected(f"{path!r}: file type not allowed")
        size = len(content.encode())
        if size > MAX_FILE_BYTES:
            raise Rejected(f"{path!r} is too large")
        total += size
        out.append({"path": path, "content": content})
    if total > MAX_TOTAL_BYTES:
        raise Rejected("files are too large in total")
    return out


class Http:
    def __init__(self, base: str, token: str, provider: str, cafile: str | None = None, opener=None):
        self.base, self.token, self.provider = base.rstrip("/"), token, provider
        self.ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        self.opener = opener

    def __call__(self, method: str, path: str, body: dict | None = None, ok=(200, 201)) -> tuple[int, dict | list]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.provider == "gitlab":
            headers["PRIVATE-TOKEN"] = self.token
        else:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["Accept"] = "application/vnd.github+json"
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            if self.opener:
                status, raw = self.opener(req)
            else:
                with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
                    status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        data = json.loads(raw) if raw else {}
        if status not in ok:
            raise RuntimeError(f"{method} {path} -> HTTP {status}: {str(data)[:300]}")
        return status, data


def gitlab_pr(http: Http, repo: str, base: str, branch: str, files: list[dict], title: str, body: str,
              message: str, labels: list[str]) -> dict:
    pid = urllib.parse.quote(repo, safe="")
    _, existing = http("GET", f"/projects/{pid}/merge_requests?state=opened&source_branch={urllib.parse.quote(branch)}")
    if existing:
        return {"url": existing[0]["web_url"], "number": existing[0]["iid"], "branch": branch, "existing": True}
    actions = []
    for f in files:
        fp = urllib.parse.quote(f["path"], safe="")
        status, _ = http("GET", f"/projects/{pid}/repository/files/{fp}?ref={urllib.parse.quote(base)}", ok=(200, 404))
        actions.append({"action": "update" if status == 200 else "create", "file_path": f["path"],
                        "content": f["content"]})
    http("POST", f"/projects/{pid}/repository/commits",
         {"branch": branch, "start_branch": base, "commit_message": message, "actions": actions})
    _, mr = http("POST", f"/projects/{pid}/merge_requests",
                 {"source_branch": branch, "target_branch": base, "title": title, "description": body,
                  "labels": ",".join(labels), "remove_source_branch": True})
    return {"url": mr["web_url"], "number": mr["iid"], "branch": branch}


def github_pr(http: Http, repo: str, base: str, branch: str, files: list[dict], title: str, body: str,
              message: str, labels: list[str]) -> dict:
    owner = repo.split("/")[0]
    _, existing = http("GET", f"/repos/{repo}/pulls?state=open&head={urllib.parse.quote(f'{owner}:{branch}')}")
    if existing:
        return {"url": existing[0]["html_url"], "number": existing[0]["number"], "branch": branch, "existing": True}
    _, ref = http("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(base)}")
    base_sha = ref["object"]["sha"]
    _, tree = http("POST", f"/repos/{repo}/git/trees", {"base_tree": base_sha, "tree": [
        {"path": f["path"], "mode": "100644", "type": "blob", "content": f["content"]} for f in files]})
    _, commit = http("POST", f"/repos/{repo}/git/commits",
                     {"message": message, "tree": tree["sha"], "parents": [base_sha]})
    http("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    _, pr = http("POST", f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body})
    if labels:
        http("POST", f"/repos/{repo}/issues/{pr['number']}/labels", {"labels": labels})
    return {"url": pr["html_url"], "number": pr["number"], "branch": branch}


def token() -> str:
    arn = os.environ["GIT_TOKEN_SECRET_ARN"]
    if arn not in _token_cache:
        import boto3
        _token_cache[arn] = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"].strip()
    return _token_cache[arn]


def lambda_handler(event, context, http: Http | None = None):
    alias, kind = caller(context)
    try:
        if not (alias and ALIAS_RE.match(alias)):
            raise Rejected("missing caller alias")
        if not (kind and KIND_RE.match(kind)):
            raise Rejected("missing agent kind")
        profiles = json.loads(os.environ.get("GIT_PROFILES", "{}"))
        profile = profiles.get(kind)
        if not profile:
            raise Rejected(f"agent '{kind}' may not open pull requests")
        job_id = str(event.get("job_id") or "")
        if not JOB_RE.match(job_id):
            raise Rejected("job_id is required")
        files = check_files(event.get("files"), profile)
        title = re.sub(r"\s+", " ", str(event.get("title") or f"[{kind}] {job_id[:8]}"))[:200]
        branch = f"agent/{kind}/{re.sub(r'[^A-Za-z0-9._-]', '-', alias)}/{job_id[:12]}"
        trailer = f"Requested-by: {alias}\nAgent: {kind}\nJob: {job_id}"
        body = str(event.get("body") or "")[:20000] + "\n\n---\n" + trailer.replace("\n", "  \n")
        provider = os.environ.get("GIT_PROVIDER", "gitlab")
        http = http or Http(os.environ["GIT_API_URL"], token(), provider, os.environ.get("GIT_CA_BUNDLE"))
        fn = gitlab_pr if provider == "gitlab" else github_pr
        pr = fn(http, profile["repo"], profile.get("base", "main"), branch, files, title, body,
                f"{title}\n\n{trailer}", list(profile.get("labels") or []))
        result = {**pr, "repo": profile["repo"], "provider": provider}
        _log(decision="opened", alias=alias, agent=kind, job_id=job_id, repo=profile["repo"], files=len(files), **pr)
        return result
    except Rejected as e:
        _log(decision="rejected", alias=alias, agent=kind, reason=str(e))
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - report to the agent, keep details in the log
        _log(decision="failed", alias=alias, agent=kind, reason=f"{type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}

