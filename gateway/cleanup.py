"""Remove what gateway/deploy.py created (tolerates already-deleted resources).

    python gateway/cleanup.py            # keeps the runtime itself (remove it with `agentcore destroy`)
    python gateway/cleanup.py --keep-cognito --keep-bucket
"""

import argparse
import json
import time
from pathlib import Path

import boto3

STATE_FILE = Path(__file__).resolve().parent / ".deploy-state.json"
PREFIX = "csp-arch"


def safe(what, fn, *a, **kw):
    try:
        fn(*a, **kw)
        print(f"  removed {what}")
    except Exception as e:  # noqa: BLE001
        print(f"  skip {what}: {type(e).__name__}: {str(e)[:120]}")


def delete_gateway(ctl, gateway_id):
    for t in ctl.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=50).get("items", []):
        safe(f"target {t['name']}", ctl.delete_gateway_target, gatewayIdentifier=gateway_id, targetId=t["targetId"])
    for _ in range(30):
        if not ctl.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=50).get("items"):
            break
        time.sleep(5)
    safe(f"gateway {gateway_id}", ctl.delete_gateway, gatewayIdentifier=gateway_id)


def delete_role(iam, name):
    try:
        for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
    except Exception:  # noqa: BLE001
        pass
    safe(f"role {name}", iam.delete_role, RoleName=name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-cognito", action="store_true")
    ap.add_argument("--keep-bucket", action="store_true")
    args = ap.parse_args()
    state = json.loads(STATE_FILE.read_text())
    s = boto3.Session(region_name=state["region"])
    ctl, iam = s.client("bedrock-agentcore-control"), s.client("iam")

    if state.get("vpce_id"):
        safe(f"VPC endpoint {state['vpce_id']}", s.client("ec2").delete_vpc_endpoints, VpcEndpointIds=[state["vpce_id"]])
    rt = state.get("runtime_arn")
    if rt:
        for resource in (rt, f"{rt}/runtime-endpoint/DEFAULT"):
            safe(f"resource policy on {resource}", ctl.delete_resource_policy, resourceArn=resource)
    for key in ("ingress_gateway_id", "tools_gateway_id"):
        if state.get(key):
            delete_gateway(ctl, state[key])
    if state.get("runtime_role_arn"):
        safe("runtime role tools-gateway policy", iam.delete_role_policy,
             RoleName=state["runtime_role_arn"].split("/")[-1], PolicyName=f"{PREFIX}-invoke-tools-gateway")
    lam = s.client("lambda")
    for fn in (f"{PREFIX}-alias-interceptor", f"{PREFIX}-postprocess-hook"):
        safe(f"function {fn}", lam.delete_function, FunctionName=fn)
    for role in (f"{PREFIX}-ingress-gw-role", f"{PREFIX}-tools-gw-role", f"{PREFIX}-lambda-role"):
        delete_role(iam, role)
    if not args.keep_bucket and state.get("artifact_bucket"):
        bucket = s.resource("s3").Bucket(state["artifact_bucket"])
        safe("bucket objects", lambda: bucket.objects.all().delete())
        safe(f"bucket {bucket.name}", bucket.delete)
    if not args.keep_cognito and state.get("user_pool_id"):
        safe(f"user pool {state['user_pool_id']}", s.client("cognito-idp").delete_user_pool,
             UserPoolId=state["user_pool_id"])
    print("\nRuntime env vars / header allowlist were left in place; `agentcore deploy` resets them.")
    STATE_FILE.rename(STATE_FILE.with_suffix(".json.deleted"))


if __name__ == "__main__":
    main()
