"""Provisioning for the portal agents (used by gateway/deploy.py): session table, uploads bucket, git-pr Lambda,
one ingress target + hardened runtime per agent, and the private endpoints the runtimes need in VPC mode.

The policy / config builders are pure functions so tests can check them without AWS.
"""

import json
import time
from pathlib import Path

import yaml
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PREFIX = "csp-arch"
CONFIG_FILE = HERE / "agents.yaml"
USER_ALIAS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias"
GIT_TARGET = "git-pr"
RUNTIME_KEEP = ["agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
                "lifecycleConfiguration", "authorizerConfiguration", "metadataConfiguration",
                "filesystemConfigurations", "description", "capacityProviderConfiguration", "platformVersion"]


# ---------------------------------------------------------------------------------------------- config
def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    targets = [a["target"] for a in cfg["agents"].values()]
    if len(targets) != len(set(targets)):
        raise ValueError("agents.yaml: duplicate targets")
    kinds = {a.get("kind") for a in cfg["agents"].values() if a.get("kind")}
    missing = [k for k, a in cfg["agents"].items() if GIT_TARGET in a.get("tools", [])
               and a.get("kind") not in (cfg.get("git") or {}).get("profiles", {})]
    if missing:
        raise ValueError(f"agents.yaml: {missing} use {GIT_TARGET} but have no git profile")
    if cfg.get("git") and cfg["git"].get("network", "vpc") not in ("vpc", "public"):
        raise ValueError("agents.yaml: git.network must be 'vpc' (internal Git over TGW / DX) or 'public' (github.com)")
    unknown = set((cfg.get("git") or {}).get("profiles", {})) - kinds
    if unknown:
        raise ValueError(f"agents.yaml: git profiles for unknown kinds {sorted(unknown)}")
    return cfg


def portal_agents(cfg: dict) -> dict:
    """Agents that run on portal/runtime.py (everything except the original CSP architect agent)."""
    return {k: a for k, a in cfg["agents"].items() if a.get("kind")}


def tool_acl(cfg: dict) -> dict:
    """x-agent-kind -> allowed tools-gateway targets, for the tools interceptor (TOOL_ACL)."""
    return {(a.get("kind") or "_default"): sorted(set(a.get("tools", []))) for a in cfg["agents"].values()}


def runtime_arns_from_yaml(cfg: dict, overrides: dict[str, str]) -> dict[str, str]:
    path = ROOT / ".bedrock_agentcore.yaml"
    known = yaml.safe_load(path.read_text())["agents"] if path.exists() else {}
    out = {}
    for key, a in cfg["agents"].items():
        arn = overrides.get(key) or ((known.get(a["agent_name"]) or {}).get("bedrock_agentcore") or {}).get("agent_arn")
        if arn:
            out[key] = arn
    return out


# ---------------------------------------------------------------------------------------------- pure builders
def uploads_lifecycle(cfg: dict) -> dict:
    r = cfg.get("retention", {})
    days = int(r.get("upload_days", 90))
    return {"Rules": [
        {"ID": "expire-uploads", "Status": "Enabled", "Filter": {"Prefix": "uploads/"}, "Expiration": {"Days": days}},
        {"ID": "expire-session-offload", "Status": "Enabled", "Filter": {"Prefix": "sessions/"},
         "Expiration": {"Days": int(r.get("session_ttl_days", 90))}},
        {"ID": "abort-incomplete-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]}


def uploads_bucket_policy(bucket: str, s3_interface_vpce: str | None) -> dict:
    arn = f"arn:aws:s3:::{bucket}"
    statements = [{"Sid": "TLSOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                   "Resource": [arn, f"{arn}/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]
    if s3_interface_vpce:
        # Presigned uploads from on-prem must arrive through the S3 interface endpoint (over DX), never the internet.
        statements.append({"Sid": "UploadsOnlyViaEndpoint", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
                           "Resource": f"{arn}/uploads/*",
                           "Condition": {"StringNotEquals": {"aws:SourceVpce": s3_interface_vpce}}})
    return {"Version": "2012-10-17", "Statement": statements}


def runtime_env(state: dict, kind: str, cfg: dict) -> dict:
    return {"TOOLS_GATEWAY_URL": state["tools_gateway_url"], "REQUIRE_CALLER_ALIAS": "true", "ASYNC_DEFAULT": "true",
            "JOB_BUCKET": state["artifact_bucket"], "SESSION_TABLE": state["session_table"],
            "SESSION_TTL_DAYS": str(cfg.get("retention", {}).get("session_ttl_days", 90)),
            "UPLOAD_BUCKET": state["uploads_bucket"], "SESSION_BUCKET": state["uploads_bucket"],
            "GIT_TARGET": GIT_TARGET, "AGENT_KIND": kind}


def runtime_policy(state: dict, account: str, region: str) -> dict:
    up, art = state["uploads_bucket"], state["artifact_bucket"]
    table = f"arn:aws:dynamodb:{region}:{account}:table/{state['session_table']}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ToolsGateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": state["tools_gateway_arn"]},
        {"Sid": "Jobs", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"arn:aws:s3:::{art}/jobs/*"},
        {"Sid": "Sessions", "Effect": "Allow", "Resource": table,
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:Query"]},
        {"Sid": "UploadsAndSessionOffload", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": [f"arn:aws:s3:::{up}/uploads/*", f"arn:aws:s3:::{up}/sessions/*"]}]}


def runtime_resource_policy(resource: str, allowed: list[str]) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "AllowOnlyIngressGatewayRole", "Effect": "Allow", "Principal": {"AWS": allowed},
         "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": resource},
        {"Sid": "DenyEveryoneElse", "Effect": "Deny", "Principal": {"AWS": "*"},
         "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": resource,
         "Condition": {"ArnNotEquals": {"aws:PrincipalArn": allowed}}}]}


def git_lambda_env(cfg: dict, secret_arn: str) -> dict:
    g = cfg["git"]
    env = {"GIT_PROVIDER": g["provider"], "GIT_API_URL": g["api_url"], "GIT_TOKEN_SECRET_ARN": secret_arn,
           "GIT_PROFILES": json.dumps(g["profiles"], separators=(",", ":"))}
    if g.get("ca_bundle"):
        env["GIT_CA_BUNDLE"] = Path(g["ca_bundle"]).name
    return env


# ---------------------------------------------------------------------------------------------- AWS steps
def deploy_session_table(ddb, state: dict) -> None:
    name = state.setdefault("session_table", f"{PREFIX}-portal-sessions")
    try:
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                         SSESpecification={"Enabled": True}, DeletionProtectionEnabled=True)
        ddb.get_waiter("table_exists").wait(TableName=name)
        print(f"  created table {name}")
    except ddb.exceptions.ResourceInUseException:
        print(f"  table {name} exists")
    try:
        ddb.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
    except ClientError as e:
        if "already enabled" not in str(e).lower():
            raise
    ddb.update_continuous_backups(TableName=name, PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True})


def deploy_uploads_bucket(s3, state: dict, cfg: dict, account: str, region: str) -> None:
    bucket = state.setdefault("uploads_bucket", f"{PREFIX}-uploads-{account}-{region}")
    try:
        s3.create_bucket(Bucket=bucket, **({} if region == "us-east-1" else
                                            {"CreateBucketConfiguration": {"LocationConstraint": region}}))
        print(f"  created bucket {bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=uploads_lifecycle(cfg))
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(uploads_bucket_policy(bucket, state.get("s3_interface_vpce"))))


def deploy_git_lambda(iam, lam, ec2, sm, state: dict, cfg: dict, account: str, subnet_ids: list[str], vpc_id: str,
                      ensure_role, ensure_lambda, zip_files) -> None:
    """git-pr Lambda. network: vpc -> attached to the agent VPC (internal Git over TGW / DX, egress SG to the Git
    CIDR only); network: public -> outside any VPC, for a github.com sandbox repository while there is no internal
    Git to reach."""
    g = cfg["git"]
    public = g.get("network", "vpc") == "public"
    secret = sm.describe_secret(SecretId=g["token_secret_name"])["ARN"]  # create the secret yourself (bot token)
    sg = None
    if not public:
        try:
            sg = ec2.create_security_group(GroupName=f"{PREFIX}-git-pr", VpcId=vpc_id,
                                           Description="git-pr Lambda: HTTPS to the internal Git server and VPC endpoints")["GroupId"]
            ec2.revoke_security_group_egress(GroupId=sg, IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
            vpc_cidr = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlock"]
            ec2.authorize_security_group_egress(GroupId=sg, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                "IpRanges": [{"CidrIp": c, "Description": "internal Git / VPC endpoints"} for c in [*g["egress_cidrs"], vpc_cidr]]}])
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
                raise
            sg = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-git-pr"]},
                                                       {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"][0]["GroupId"]
    role = ensure_role(iam, f"{PREFIX}-git-pr-role",
                       {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                                 "Principal": {"Service": "lambda.amazonaws.com"}}]},
                       {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                                                                 "Resource": secret}]},
                       ["arn:aws:iam::aws:policy/service-role/" + ("AWSLambdaBasicExecutionRole" if public
                                                                   else "AWSLambdaVPCAccessExecutionRole")])
    files = {"handler.py": HERE / "lambdas/git_pr/handler.py"}
    if g.get("ca_bundle"):
        files[Path(g["ca_bundle"]).name] = ROOT / g["ca_bundle"]
    # No generic invoke permission: deploy_tools_gateway grants it to the tools gateway ARN only, because the
    # caller identity (alias, agent kind) comes from the invocation's client context.
    state["git_pr_arn"] = ensure_lambda(lam, f"{PREFIX}-git-pr", role, zip_files(files), git_lambda_env(cfg, secret),
                                        account, timeout=60, memory=256, permission=False)
    # empty lists detach a function that was VPC-attached before
    vpc_config = {"SubnetIds": [], "SecurityGroupIds": []} if public else {"SubnetIds": subnet_ids, "SecurityGroupIds": [sg]}
    lam.update_function_configuration(FunctionName=f"{PREFIX}-git-pr", VpcConfig=vpc_config)
    lam.get_waiter("function_updated_v2").wait(FunctionName=f"{PREFIX}-git-pr")
    state["git_pr_sg"], state["git_network"] = sg, "public" if public else "vpc"


def git_target_config(state: dict) -> dict:
    return {"targetConfiguration": {"mcp": {"lambda": {"lambdaArn": state["git_pr_arn"], "toolSchema": {"inlinePayload": [{
        "name": "open_pull_request",
        "description": "Open a pull / merge request in the repository configured for the calling agent.",
        "inputSchema": {"type": "object", "required": ["job_id", "title", "files"], "properties": {
            "agent": {"type": "string"}, "job_id": {"type": "string"}, "title": {"type": "string"},
            "body": {"type": "string"},
            "files": {"type": "array", "items": {"type": "object", "required": ["path", "content"], "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}}}}}}}]}}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        "metadataConfiguration": {"allowedRequestHeaders": ["x-user-alias", "x-agent-kind"]}}


def update_portal_runtime(iam, ctl, state: dict, key: str, kind: str, cfg: dict, account: str, region: str,
                          wait_ready, vpc: dict | None = None) -> None:
    arn = state["agents"][key]["runtime_arn"]
    runtime_id = arn.split("/")[-1]
    current = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    kwargs = {k: current[k] for k in RUNTIME_KEEP if current.get(k) is not None}
    if vpc:  # private: ENIs in the agent VPC, reaching Bedrock / S3 / DynamoDB / tools gateway via endpoints
        kwargs["networkConfiguration"] = {"networkMode": "VPC", "networkModeConfig": {
            "subnets": vpc["subnet_ids"], "securityGroups": [vpc["security_group_id"]]}}
    env = {**(current.get("environmentVariables") or {}), **runtime_env(state, kind, cfg)}
    headers = sorted(set((current.get("requestHeaderConfiguration") or {}).get("requestHeaderAllowlist", []))
                     | {USER_ALIAS_HEADER})
    ctl.update_agent_runtime(agentRuntimeId=runtime_id, environmentVariables=env,
                             requestHeaderConfiguration={"requestHeaderAllowlist": headers}, **kwargs)
    wait_ready(lambda: ctl.get_agent_runtime(agentRuntimeId=runtime_id), f"runtime {key}")
    iam.put_role_policy(RoleName=current["roleArn"].split("/")[-1], PolicyName=f"{PREFIX}-portal-agent",
                        PolicyDocument=json.dumps(runtime_policy(state, account, region)))
    state["agents"][key]["role_arn"] = current["roleArn"]
    print(f"  runtime {key} ({runtime_id}): env + header allowlist + role policy"
          + (" + VPC network mode" if vpc else ""))


ENDPOINT_SERVICES = {  # service suffix -> (type, private DNS)
    "bedrock-runtime": ("Interface", True),
    "logs": ("Interface", True),
    "secretsmanager": ("Interface", True),
    "s3": ("Gateway", None),
    "dynamodb": ("Gateway", None),
}


def _live_endpoints(ec2, vpc_id: str) -> list[dict]:
    return [e for e in ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
            if e.get("State", "").lower() not in ("deleted", "deleting", "failed", "rejected", "expired")]


def _find(endpoints: list[dict], service: str, typ: str) -> dict | None:
    return next((e for e in endpoints if e["ServiceName"] == service and e["VpcEndpointType"] == typ), None)


def deploy_runtime_endpoints(ec2, state: dict, region: str, vpc_id: str, subnet_ids: list[str], sg_id: str) -> None:
    """Endpoints for runtimes in VPC mode and the git-pr Lambda, plus the S3 interface endpoint on-prem uploads use.

    Endpoints that already exist in the VPC (shared with other workloads) are reused and NOT recorded as ours,
    so cleanup.py never deletes them."""
    route_tables = sorted({rt["RouteTableId"] for rt in ec2.describe_route_tables(Filters=[
        {"Name": "association.subnet-id", "Values": subnet_ids}])["RouteTables"]})
    ours = state.setdefault("created_endpoints", {})
    tags = [{"ResourceType": "vpc-endpoint", "Tags": [{"Key": "csp-arch", "Value": "portal"}]}]
    for svc, (typ, private_dns) in ENDPOINT_SERVICES.items():
        name = f"com.amazonaws.{region}.{svc}"
        if _find(_live_endpoints(ec2, vpc_id), name, typ):
            continue
        kw = {"RouteTableIds": route_tables} if typ == "Gateway" else {
            "SubnetIds": subnet_ids, "SecurityGroupIds": [sg_id], "PrivateDnsEnabled": private_dns}
        ours[f"{svc}-{typ.lower()}"] = ec2.create_vpc_endpoint(
            VpcEndpointType=typ, VpcId=vpc_id, ServiceName=name, TagSpecifications=tags, **kw)["VpcEndpoint"]["VpcEndpointId"]
        print(f"  created {typ.lower()} endpoint {name}")
    # S3 interface endpoint for presigned uploads from on-prem. Private DNS only for the inbound Resolver endpoint:
    # on-prem resolves <bucket>.s3.<region>.amazonaws.com to this endpoint; inside the VPC S3 keeps using the gateway
    # endpoint (which must exist first).
    s3_name = f"com.amazonaws.{region}.s3"
    hit = _find(_live_endpoints(ec2, vpc_id), s3_name, "Interface")
    if hit:
        state["s3_interface_vpce"] = hit["VpcEndpointId"]
    else:
        time.sleep(5)
        state["s3_interface_vpce"] = ours["s3-interface"] = ec2.create_vpc_endpoint(
            VpcEndpointType="Interface", VpcId=vpc_id, ServiceName=s3_name, SubnetIds=subnet_ids, SecurityGroupIds=[sg_id],
            PrivateDnsEnabled=True, DnsOptions={"PrivateDnsOnlyForInboundResolverEndpoint": True}, TagSpecifications=tags,
        )["VpcEndpoint"]["VpcEndpointId"]
        print(f"  created interface endpoint {s3_name} (private DNS for on-prem via the inbound Resolver)")


def runtime_security_group(ec2, vpc_id: str) -> str:
    """Runtime ENIs (VPC mode): no inbound, outbound HTTPS to the VPC only (endpoints + gateway endpoint prefixes
    are reached through route tables, so the S3 / DynamoDB prefix lists are added too)."""
    cidr = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlock"]
    try:
        sg = ec2.create_security_group(GroupName=f"{PREFIX}-runtime", VpcId=vpc_id,
                                       Description="AgentCore Runtime ENIs: HTTPS to VPC endpoints only")["GroupId"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        return ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-runtime"]},
                                                     {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"][0]["GroupId"]
    ec2.revoke_security_group_egress(GroupId=sg, IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
    region = ec2.meta.region_name
    prefix_lists = [p["PrefixListId"] for p in ec2.describe_prefix_lists(Filters=[{"Name": "prefix-list-name", "Values": [
        f"com.amazonaws.{region}.s3", f"com.amazonaws.{region}.dynamodb"]}])["PrefixLists"]]
    ec2.authorize_security_group_egress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": cidr, "Description": "VPC endpoints"}],
        "PrefixListIds": [{"PrefixListId": p, "Description": "S3 / DynamoDB gateway endpoints"} for p in prefix_lists]}])
    return sg
