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
    env = pd.git_lambda_env(pd.load_config(ROOT / "gateway/agents.enterprise.yaml"), "arn:secret")
    assert env["GIT_PROVIDER"] == "gitlab" and "infra-live" in env["GIT_PROFILES"] and env["GIT_TOKEN_SECRET_ARN"] == "arn:secret"


class FakeEC2:
    def __init__(self, existing):
        self.endpoints, self.created = list(existing), []

    def describe_route_tables(self, Filters):
        return {"RouteTables": [{"RouteTableId": "rtb-1"}]}

    def describe_vpc_endpoints(self, Filters):
        return {"VpcEndpoints": self.endpoints}

    def create_vpc_endpoint(self, **kw):
        eid = f"vpce-{len(self.endpoints)}"
        self.endpoints.append({"VpcEndpointId": eid, "ServiceName": kw["ServiceName"], "VpcEndpointType": kw["VpcEndpointType"],
                               "State": "available"})
        self.created.append(kw)
        return {"VpcEndpoint": {"VpcEndpointId": eid}}


def test_runtime_endpoints_reuse_shared_ones_and_record_only_ours(monkeypatch):
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    shared = [{"VpcEndpointId": "vpce-shared", "ServiceName": "com.amazonaws.us-east-1.s3", "VpcEndpointType": "Gateway",
               "State": "available"},
              {"VpcEndpointId": "vpce-dead", "ServiceName": "com.amazonaws.us-east-1.logs", "VpcEndpointType": "Interface",
               "State": "deleted"}]
    ec2, state = FakeEC2(shared), {}
    pd.deploy_runtime_endpoints(ec2, state, "us-east-1", "vpc-1", ["subnet-a"], "sg-1")
    made = {(c["ServiceName"].rsplit(".", 1)[-1], c["VpcEndpointType"]) for c in ec2.created}
    assert made == {("bedrock-runtime", "Interface"), ("logs", "Interface"), ("secretsmanager", "Interface"),
                    ("dynamodb", "Gateway"), ("s3", "Interface")}
    assert "vpce-shared" not in state["created_endpoints"].values() and len(state["created_endpoints"]) == 5
    s3if = next(c for c in ec2.created if c["ServiceName"].endswith(".s3"))
    assert s3if["DnsOptions"] == {"PrivateDnsOnlyForInboundResolverEndpoint": True}
    pd.deploy_runtime_endpoints(ec2, state, "us-east-1", "vpc-1", ["subnet-a"], "sg-1")  # re-run: nothing new
    assert len(ec2.created) == 5
