"""Terraform agent: static checks, waivers, diagram summary, generation + repair, CSP architect job input."""
import glob
from pathlib import Path

import pytest

import jobs
import terraform_agent
from portal.runtime import RunContext
from terraform_checks import check
from test_skills import ScriptedModel

VERSIONS = '''terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Owner = var.owner, CostCenter = var.cost_center, Environment = var.environment, DataClassification = var.data_classification }
  }
}
'''
VARIABLES = "\n".join(f'variable "{v}" {{\n  type        = string\n  description = "{v}"\n}}\n'
                      for v in ("region", "owner", "cost_center", "environment", "data_classification", "vpc_cidr"))
MAIN = '''resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr
}

resource "aws_security_group" "app" {
  vpc_id = aws_vpc.this.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }
}

resource "aws_kms_key" "data" {
  enable_key_rotation = true
}
'''
GOOD = {"versions.tf": VERSIONS, "variables.tf": VARIABLES, "main.tf": MAIN,
        "outputs.tf": 'output "vpc_id" {\n  value = aws_vpc.this.id\n}\n', "README.md": "# network\n"}


def rules(report, severity="error"):
    return sorted({f["rule"] for f in report["findings"] if f["severity"] == severity})


def test_good_module_passes():
    r = check(GOOD)
    assert r["ok"] and r["errors"] == 0 and r["warnings"] == 0, r["findings"]


@pytest.mark.parametrize("change,rule", [
    (lambda f: f.update({"main.tf": MAIN.replace('"10.0.0.0/8"', '"0.0.0.0/0"')}), "TF005"),
    (lambda f: f.update({"versions.tf": VERSIONS.replace('version = "~> 6.0" ', "")}), "TF002"),
    (lambda f: f.update({"versions.tf": VERSIONS.replace('required_version = ">= 1.10"', "")}), "TF002"),
    (lambda f: f.update({"versions.tf": VERSIONS.replace("CostCenter = var.cost_center, ", "")}), "TF007"),
    (lambda f: f.update({"main.tf": MAIN + 'resource "aws_internet_gateway" "igw" {\n  vpc_id = aws_vpc.this.id\n}\n'}), "TF008"),
    (lambda f: f.update({"main.tf": MAIN.replace("enable_key_rotation = true", "enable_key_rotation = false")}), "TF011"),
    (lambda f: f.update({"main.tf": MAIN + 'resource "aws_instance" "vm" {\n  ami = "ami-1"\n}\n'}), "TF012"),
    (lambda f: f.update({"main.tf": MAIN + 'resource "aws_db_instance" "db" {\n  publicly_accessible = true\n}\n'}), "TF010"),
    (lambda f: f.update({"main.tf": MAIN + 'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n}\n'}), "TF006"),
    (lambda f: f.update({"main.tf": MAIN + 'locals {\n  k = "AKIAABCDEFGHIJKLMNOP"\n}\n'}), "TF003"),
    (lambda f: f.update({"terraform.tfvars": 'region = "us-east-1"\n'}), "TF003"),
    (lambda f: f.update({"main.tf": "resource {{{ broken"}), "TF001"),
    (lambda f: f.update({"variables.tf": VARIABLES + 'variable "db_password" {\n  type = string\n  description = "x"\n}\n'}), "TF004"),
])
def test_rule_violations(change, rule):
    files = dict(GOOD)
    change(files)
    r = check(files)
    assert not r["ok"] and rule in rules(r), r["findings"]


def test_waivers_need_a_reason_and_cannot_hide_secrets():
    files = dict(GOOD, **{"main.tf": MAIN.replace('"10.0.0.0/8"', '"0.0.0.0/0"').replace(
        "resource \"aws_security_group\"", "# tfcheck:allow TF005 public ALB approved in EXC-2026-014\nresource \"aws_security_group\"")})
    r = check(files)
    assert r["ok"] and r["waived"] == 1 and r["findings"][0]["waived"].startswith("public ALB")
    files["main.tf"] += '# tfcheck:allow TF003 testing only\nlocals {\n  k = "AKIAABCDEFGHIJKLMNOP"\n}\n'
    assert "TF003" in rules(check(files))
    short = dict(GOOD, **{"main.tf": "# tfcheck:allow TF005 ok\n" + MAIN.replace('"10.0.0.0/8"', '"0.0.0.0/0"')})
    assert not check(short)["ok"]  # a waiver needs a real reason (5+ characters)


def test_diagram_summary_from_examples():
    for f in glob.glob(str(Path(__file__).resolve().parents[1] / "examples" / "*.drawio")):
        s = terraform_agent.summarize_drawio(Path(f).read_text())
        assert s.startswith("## Page:") and "->" in s and len(s) < 60_000


def blocks(files):
    return "\n".join(f'<file path="{p}">\n{c}</file>' for p, c in files.items())


def ctx(**kw):
    return RunContext(kind="terraform", alias="yhtsao", job_id="cd" * 16, session_id=None, **kw)


def test_generation_with_repair_turn_and_prefixing(monkeypatch):
    bad = dict(GOOD, **{"main.tf": MAIN.replace('"10.0.0.0/8"', '"0.0.0.0/0"')})
    model = ScriptedModel([{"text": blocks(bad) + "\nCreated a VPC."}, {"text": blocks(GOOD) + "\nFixed the SG."}])
    monkeypatch.setattr(terraform_agent, "BedrockModel", lambda **kw: model)
    out = terraform_agent.run(terraform_agent.portal, {"prompt": "VPC for payments", "title": "payments-network"}, ctx())
    assert out["publishable"] and out["checks"]["repair_turns"] == 1
    assert "[TF005] main.tf" in model.seen[1]["content"][0]["text"]
    assert sorted(out["files"]) == sorted(f"terraform/payments-network/{p}" for p in GOOD)
    assert out["summary"] == "Fixed the SG." and "use_lockfile" in model.system_prompts[0]


def test_unfixable_output_is_not_publishable(monkeypatch):
    bad = {"main.tf": 'resource "aws_security_group" "x" {\n  ingress {\n    cidr_blocks = ["0.0.0.0/0"]\n  }\n}\n',
           "../../etc/x.tf": "x"}
    model = ScriptedModel([{"text": blocks(bad)}] * 3)
    monkeypatch.setattr(terraform_agent, "BedrockModel", lambda **kw: model)
    out = terraform_agent.run(terraform_agent.portal, {"prompt": "x"}, ctx())
    assert not out["publishable"] and out["checks"]["repair_turns"] == 2 and out["checks"]["path_errors"]


def test_architecture_job_input(monkeypatch):
    store = jobs.MemoryJobStore()
    monkeypatch.setattr(terraform_agent.portal, "_job_store", store)
    xml = Path(glob.glob(str(Path(__file__).resolve().parents[1] / "examples" / "mlops-aws.drawio"))[0]).read_text()
    mine = jobs.new_record("ab" * 16, "yhtsao", {})
    store.put({**mine, "status": "SUCCEEDED", "result": {"xml": xml, "explanation": "MLOps on AWS"}})
    store.put({**jobs.new_record("ef" * 16, "eve", {}), "status": "SUCCEEDED", "result": {"xml": xml}})
    model = ScriptedModel([{"text": blocks(GOOD)}])
    monkeypatch.setattr(terraform_agent, "BedrockModel", lambda **kw: model)
    out = terraform_agent.run(terraform_agent.portal, {"prompt": "network only", "architecture_job_id": "ab" * 16}, ctx())
    sent = model.seen[0]["content"][0]["text"]
    assert out["publishable"] and '<architecture source="csp-architect job' in sent and "MLOps on AWS" in sent
    assert "<mxfile" not in sent  # summarised, not raw XML
    other = terraform_agent.run(terraform_agent.portal, {"prompt": "x", "architecture_job_id": "ef" * 16}, ctx())
    assert other["error_code"] == "not_found"


@pytest.mark.parametrize("payload", [{"title": "Bad Title"}, {"architecture_job_id": "x"}])
def test_payload_validation(payload):
    assert terraform_agent.validate_payload(payload)["error_code"] == "bad_request"
