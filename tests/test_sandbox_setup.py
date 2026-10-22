"""Sandbox setup: Cognito test users (yhtsao + fake users) and the github.com git-pr mode. moto, no AWS."""
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


deploy = load("gateway_deploy", "gateway/deploy.py")
pd = deploy.pd
client = load("gateway_client_sandbox", "gateway/client.py")


@pytest.fixture()
def cognito(aws):
    import boto3
    return boto3.client("cognito-idp", region_name="us-east-1")


def test_fake_users_get_passwords_in_a_private_file_and_can_log_in(cognito, tmp_path, capsys):
    users_file, state = tmp_path / ".test-users.json", {"region": "us-east-1"}
    users = ["yhtsao", *deploy.fake_user_names(2)]
    assert users == ["yhtsao", "fake-user-01", "fake-user-02"]
    saved = deploy.deploy_cognito(cognito, state, users, users_file=users_file)
    assert set(saved) == set(users) and stat.S_IMODE(users_file.stat().st_mode) == 0o600
    assert not any(pw in capsys.readouterr().out for pw in saved.values())  # never printed
    assert state["test_user"] == "yhtsao" and state["test_users"] == users
    listed = {u["Username"] for u in cognito.list_users(UserPoolId=state["user_pool_id"])["Users"]}
    assert listed == set(users)
    for u in users:
        tok = client.login(state, u, saved[u])
        assert client.claims(tok)["username"] == u  # the alias the interceptor will use

    # re-run: same passwords; adding a user only creates that one; --reset-passwords rotates everyone
    again = deploy.deploy_cognito(cognito, state, users + ["fake-user-03"], users_file=users_file)
    assert {u: again[u] for u in users} == saved and "fake-user-03" in again
    rotated = deploy.deploy_cognito(cognito, state, users, reset=True, users_file=users_file)
    assert all(rotated[u] != saved[u] for u in users)


def test_client_reads_passwords_from_the_users_file(tmp_path, monkeypatch):
    f = tmp_path / ".test-users.json"
    f.write_text(json.dumps({"yhtsao": "pw-a", "fake-user-01": "pw-b"}))
    monkeypatch.setattr(client, "USERS_FILE", f)
    monkeypatch.setenv("CSP_TEST_PASSWORD", "from-env")
    assert client.test_password("yhtsao") == "from-env"
    assert client.test_password("fake-user-01", primary=False) == "pw-b"
    monkeypatch.delenv("CSP_TEST_PASSWORD")
    assert client.test_password("yhtsao") == "pw-a"


def test_sandbox_and_enterprise_configs():
    sandbox = pd.load_config()
    assert sandbox["git"]["network"] == "public" and sandbox["git"]["api_url"] == "https://api.github.com"
    repos = {p["repo"] for p in sandbox["git"]["profiles"].values()}
    prefixes = [p["path_prefix"] for p in sandbox["git"]["profiles"].values()]
    assert repos == {"yunistsao-maker/portal-agents-sandbox"} and len(set(prefixes)) == 3  # one repo, a folder each
    enterprise = pd.load_config(ROOT / "gateway/agents.enterprise.yaml")
    assert enterprise["git"]["network"] == "vpc" and enterprise["git"]["provider"] == "gitlab"


def test_bad_network_value(tmp_path):
    import yaml
    cfg = yaml.safe_load(pd.CONFIG_FILE.read_text())
    cfg["git"]["network"] = "internet"
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="git.network"):
        pd.load_config(p)


class Rec:
    """Records calls to any method; returns canned answers."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __getattr__(self, name):
        def fn(*a, **kw):
            self.calls.append((name, kw))
            return self.answers.get(name, {})
        return fn


def test_public_git_lambda_is_not_in_the_vpc():
    lam = Rec({"get_waiter": Rec()})
    lam.get_waiter = lambda name: Rec()
    ec2 = Rec()
    sm = Rec({"describe_secret": {"ARN": "arn:secret:github"}})
    roles = []
    state = {}
    pd.deploy_git_lambda(Rec(), lam, ec2, sm, state, pd.load_config(), "111", [], None,
                         ensure_role=lambda iam, name, trust, policy, managed: roles.append(managed) or "arn:role",
                         ensure_lambda=lambda *a, **kw: "arn:lambda:git-pr", zip_files=lambda files: b"zip")
    assert ec2.calls == []  # no security group, no VPC lookups
    vpc = next(kw for name, kw in lam.calls if name == "update_function_configuration")["VpcConfig"]
    assert vpc == {"SubnetIds": [], "SecurityGroupIds": []}
    assert roles == [["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]]
    assert state["git_network"] == "public" and state["git_pr_arn"] == "arn:lambda:git-pr"
    env = pd.git_lambda_env(pd.load_config(), "arn:secret:github")
    assert env["GIT_PROVIDER"] == "github" and json.loads(env["GIT_PROFILES"])["coe"]["path_prefix"] == "docs/coe/"
