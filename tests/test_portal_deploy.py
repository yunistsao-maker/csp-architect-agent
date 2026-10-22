"""gateway/portal_deploy.py builders: config validation, tool ACL, IAM / bucket policies, lifecycle, runtime env."""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portal_deploy", ROOT / "gateway/portal_deploy.py")
pd = importlib.util.module_from_spec(spec)
sys.modules["portal_deploy"] = pd
spec.loader.exec_module(pd)

STATE = {"tools_gateway_url": "https://tools.gw/mcp", "tools_gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:111:gateway/t",
         "artifact_bucket": "art", "session_table": "csp-arch-portal-sessions", "uploads_bucket": "up"}


def test_shipped_config_is_valid():
    cfg = pd.load_config()
    assert set(pd.portal_agents(cfg)) == {"coe", "aicoe", "terraform"}
    acl = pd.tool_acl(cfg)
    assert acl["_default"] == ["postprocess-hook", "web-search-tool"] and "git-pr" in acl["coe"]
    assert "web-search-tool" not in acl["coe"]


def test_config_errors(tmp_path):
    cfg = yaml.safe_load(pd.CONFIG_FILE.read_text())
    cfg["agents"]["aicoe"]["target"] = "coe-doc"
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="duplicate targets"):
        pd.load_config(p)
    cfg = yaml.safe_load(pd.CONFIG_FILE.read_text())
    del cfg["git"]["profiles"]["coe"]
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="no git profile"):
        pd.load_config(p)


def test_runtime_policy_is_scoped():
    doc = pd.runtime_policy(STATE, "111", "us-east-1")
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    assert by_sid["Sessions"]["Resource"] == "arn:aws:dynamodb:us-east-1:111:table/csp-arch-portal-sessions"
    assert "dynamodb:Scan" not in by_sid["Sessions"]["Action"] and "dynamodb:DeleteTable" not in by_sid["Sessions"]["Action"]
    assert by_sid["UploadsAndSessionOffload"]["Resource"] == ["arn:aws:s3:::up/uploads/*", "arn:aws:s3:::up/sessions/*"]
    assert by_sid["Jobs"]["Resource"] == "arn:aws:s3:::art/jobs/*"
    assert all("*" != s["Resource"] for s in doc["Statement"])


def test_uploads_bucket_policy_and_lifecycle():
    pol = pd.uploads_bucket_policy("up", "vpce-123")
    pin = next(s for s in pol["Statement"] if s["Sid"] == "UploadsOnlyViaEndpoint")
    assert pin["Effect"] == "Deny" and pin["Condition"] == {"StringNotEquals": {"aws:SourceVpce": "vpce-123"}}
    assert [s["Sid"] for s in pd.uploads_bucket_policy("up", None)["Statement"]] == ["TLSOnly"]
    rules = {r["ID"]: r for r in pd.uploads_lifecycle(pd.load_config())["Rules"]}
    assert rules["expire-uploads"]["Expiration"]["Days"] == 90 and rules["expire-uploads"]["Filter"]["Prefix"] == "uploads/"
    assert rules["expire-session-offload"]["Expiration"]["Days"] == 90


def test_runtime_env_and_resource_policy():
    env = pd.runtime_env(STATE, "terraform", pd.load_config())
    assert env["AGENT_KIND"] == "terraform" and env["SESSION_TTL_DAYS"] == "90" and env["GIT_TARGET"] == "git-pr"
    assert env["REQUIRE_CALLER_ALIAS"] == env["ASYNC_DEFAULT"] == "true"
    rp = pd.runtime_resource_policy("arn:rt", ["arn:role/ingress"])
    deny = rp["Statement"][1]
    assert deny["Effect"] == "Deny" and deny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] == ["arn:role/ingress"]


def test_git_lambda_env():
    env = pd.git_lambda_env(pd.load_config(), "arn:secret")
    assert env["GIT_PROVIDER"] == "gitlab" and "infra-live" in env["GIT_PROFILES"] and env["GIT_TOKEN_SECRET_ARN"] == "arn:secret"
