"""git-pr Lambda (GitLab + GitHub Enterprise flows against a fake API) and the tools-gateway tool ACL."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


git = load("git_pr_handler", "gateway/lambdas/git_pr/handler.py")
icpt = load("alias_interceptor_acl", "gateway/lambdas/alias_interceptor/handler.py")
JOB = "ab" * 16
PROFILES = {"terraform": {"repo": "platform/infra-live", "base": "main", "path_prefix": "terraform/",
                          "suffixes": [".tf", ".md"], "labels": ["agent", "terraform"]},
            "coe": {"repo": "ccoe/design-reviews", "base": "main", "path_prefix": "docs/coe/", "suffixes": [".md"]}}


def ctx(alias="yhtsao", kind="terraform"):
    headers = {k: v for k, v in {"X-User-Alias": alias, "x-agent-kind": kind}.items() if v}
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCorePropagatedHeaders": headers}))


class FakeApi:
    """Records calls; answers like GitLab / GitHub for the PR flow."""

    def __init__(self, provider, existing=None, files_on_base=()):
        self.provider, self.calls, self.existing, self.on_base = provider, [], existing or [], set(files_on_base)

    def __call__(self, req):
        method, url = req.get_method(), req.full_url
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, url.split("/api", 1)[-1], body, dict(req.header_items())))
        if method == "GET" and ("merge_requests?" in url or "/pulls?" in url):
            return 200, json.dumps(self.existing).encode()
        if "/repository/files/" in url:
            path = url.split("/repository/files/")[1].split("?")[0].replace("%2F", "/")
            return (200, b"{}") if path in self.on_base else (404, b'{"message":"404 File Not Found"}')
        if url.endswith("/repository/commits"):
            return 201, b'{"id":"c1"}'
        if url.endswith("/merge_requests"):
            return 201, json.dumps({"web_url": "https://gitlab.corp/platform/infra-live/-/merge_requests/9", "iid": 9}).encode()
        if "/git/ref/heads/" in url:
            return 200, b'{"object":{"sha":"base1"}}'
        if url.endswith("/git/trees"):
            return 201, b'{"sha":"tree1"}'
        if url.endswith("/git/commits"):
            return 201, b'{"sha":"commit1"}'
        if url.endswith("/git/refs"):
            return 201, b"{}"
        if url.endswith("/pulls"):
            return 201, json.dumps({"html_url": "https://ghe.corp/platform/infra-live/pull/12", "number": 12}).encode()
        if url.endswith("/labels"):
            return 200, b"[]"
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GIT_PROFILES", json.dumps(PROFILES))


def event(files=None, **kw):
    return {"agent": "terraform", "job_id": JOB, "title": "[terraform] payments-network", "body": "summary",
            "files": files or [{"path": "terraform/payments-network/main.tf", "content": "x"},
                               {"path": "terraform/payments-network/README.md", "content": "# r"}], **kw}


def test_gitlab_merge_request(monkeypatch, capsys):
    monkeypatch.setenv("GIT_PROVIDER", "gitlab")
    api = FakeApi("gitlab", files_on_base={"terraform/payments-network/README.md"})
    out = git.lambda_handler(event(), ctx(), http=git.Http("https://gitlab.corp/api/v4", "tok", "gitlab", opener=api))
    assert out["url"].endswith("/merge_requests/9") and out["branch"] == f"agent/terraform/yhtsao/{JOB[:12]}"
    commit = next(b for m, u, b, h in api.calls if u.endswith("/repository/commits"))
    assert [a["action"] for a in commit["actions"]] == ["create", "update"] and commit["start_branch"] == "main"
    assert "Requested-by: yhtsao" in commit["commit_message"]
    mr = next(b for m, u, b, h in api.calls if u.endswith("/merge_requests") and m == "POST")
    assert mr["target_branch"] == "main" and mr["labels"] == "agent,terraform" and "Requested-by: yhtsao" in mr["description"]
    assert api.calls[0][3]["Private-token"] == "tok"
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["decision"] == "opened" and log["alias"] == "yhtsao" and log["repo"] == "platform/infra-live"


def test_github_pull_request(monkeypatch):
    monkeypatch.setenv("GIT_PROVIDER", "github")
    api = FakeApi("github")
    out = git.lambda_handler(event(), ctx(), http=git.Http("https://ghe.corp/api/v3", "tok", "github", opener=api))
    assert out["number"] == 12 and out["repo"] == "platform/infra-live"
    paths = [u for _, u, _, _ in api.calls]
    assert any(p.endswith("/git/trees") for p in paths) and paths[-1].endswith("/issues/12/labels")
    ref = next(b for m, u, b, h in api.calls if u.endswith("/git/refs"))
    assert ref == {"ref": f"refs/heads/agent/terraform/yhtsao/{JOB[:12]}", "sha": "commit1"}


def test_republishing_returns_the_open_pr(monkeypatch):
    monkeypatch.setenv("GIT_PROVIDER", "gitlab")
    api = FakeApi("gitlab", existing=[{"web_url": "https://gitlab.corp/x/-/merge_requests/3", "iid": 3}])
    out = git.lambda_handler(event(), ctx(), http=git.Http("https://g/api/v4", "t", "gitlab", opener=api))
    assert out["number"] == 3 and out["existing"] and len(api.calls) == 1


@pytest.mark.parametrize("files,c,msg", [
    ([{"path": "docs/coe/x.md", "content": "x"}], ctx(), "outside"),                       # another agent's area
    ([{"path": "terraform/a/run.sh", "content": "x"}], ctx(), "not allowed"),                # file type
    ([{"path": "terraform/../.github/workflows/x.md", "content": "x"}], ctx(), "invalid path"),
    (None, ctx(alias=None), "alias"),
    (None, ctx(kind="csp"), "may not open"),                                                  # no profile
    (None, ctx(kind=None), "agent kind"),
    ([{"path": "docs/coe/a.md", "content": "x" * 300_000}], ctx(kind="coe"), "too large"),
])
def test_rejections(files, c, msg):
    out = git.lambda_handler(event(files), c, http=lambda *a, **k: pytest.fail("no API call expected"))
    assert msg in out["error"]


# ---------------------------------------------------------------------------------------------- tool ACL
def mcp_event(kind, tool):
    headers = {"x-user-alias": "yhtsao", **({"x-agent-kind": kind} if kind else {})}
    return {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {"path": "/mcp", "httpMethod": "POST",
            "headers": headers, "body": {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                         "params": {"name": tool, "arguments": {"files": [1, 2]}}}}}}


ACL = {"_default": ["web-search-tool", "postprocess-hook"], "coe": ["git-pr"], "terraform": ["web-search-tool", "git-pr"]}


@pytest.mark.parametrize("kind,tool,allowed", [
    ("terraform", "git-pr___open_pull_request", True), ("coe", "git-pr___open_pull_request", True),
    ("coe", "web-search-tool___WebSearch", False), (None, "git-pr___open_pull_request", False),
    (None, "postprocess-hook___process_architecture", True), ("Bad Kind!", "git-pr___open_pull_request", False)])
def test_tool_acl(monkeypatch, kind, tool, allowed):
    monkeypatch.setenv("TOOL_ACL", json.dumps(ACL))
    out = icpt.lambda_handler(mcp_event(kind, tool), SimpleNamespace(client_context=None))["mcp"]
    assert ("transformedGatewayRequest" in out) == allowed
    if not allowed:
        assert out["transformedGatewayResponse"]["body"]["error"]["code"] == -32003


def test_no_acl_allows_everything(monkeypatch):
    monkeypatch.delenv("TOOL_ACL", raising=False)
    out = icpt.lambda_handler(mcp_event("coe", "web-search-tool___WebSearch"), SimpleNamespace(client_context=None))
    assert "transformedGatewayRequest" in out["mcp"]
