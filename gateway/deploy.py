"""Put the CSP architect agent behind AgentCore Gateway, reachable only through a PrivateLink VPC endpoint.

    client (in VPC) --JWT--> VPC endpoint (bedrock-agentcore.gateway) --> INGRESS gateway (CUSTOM_JWT)
        --> REQUEST interceptor Lambda (JWT alias -> X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias)
        --> AgentCore Runtime target (HTTP, GATEWAY_IAM_ROLE / SigV4) --> agent.py
    agent.py --SigV4 + x-user-alias--> TOOLS gateway (MCP, AWS_IAM)
        --> web-search connector (GATEWAY_IAM_ROLE)
        --> post-processing hook Lambda (GATEWAY_IAM_ROLE, reads propagated x-user-alias)

Prerequisite: the runtime is already deployed (`agentcore deploy`), so .bedrock_agentcore.yaml has its ARN.

    python gateway/deploy.py --vpc-id vpc-0abc --subnet-ids subnet-1,subnet-2 [--security-group-id sg-...]
    python gateway/deploy.py --update-runtime-only     # re-apply runtime settings after `agentcore deploy`

State is written to gateway/.deploy-state.json (used by gateway/client.py and gateway/cleanup.py).
"""

import argparse
import getpass
import io
import json
import secrets
import string
import sys
import time
import zipfile
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".deploy-state.json"
PREFIX = "csp-arch"
INGRESS_GATEWAY = f"{PREFIX}-ingress"
TOOLS_GATEWAY = f"{PREFIX}-tools"
RUNTIME_TARGET = "csp-architect"
WEB_SEARCH_TARGET = "web-search-tool"
HOOK_TARGET = "postprocess-hook"
USER_ALIAS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias"
TOOLS_ALIAS_HEADER = "x-user-alias"


# ---------------------------------------------------------------------------------------------------- helpers
def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=1))


def step(msg: str):
    print(f"\n▶ {msg}", flush=True)


def runtime_from_yaml() -> tuple[str | None, str | None]:
    path = ROOT / ".bedrock_agentcore.yaml"
    if not path.exists():
        return None, None
    cfg = yaml.safe_load(path.read_text())
    agent = cfg["agents"][cfg["default_agent"]]
    return agent["bedrock_agentcore"].get("agent_arn"), agent["aws"].get("region")


def trust_policy(account: str, region: str, gateway_arn: str | None) -> dict:
    """Trust bedrock-agentcore only for this account's gateways (or exactly one gateway once it exists)."""
    source = gateway_arn or f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}, "ArnLike": {"aws:SourceArn": source}}}]}


def ensure_role(iam, name: str, trust: dict, policy: dict, managed: list[str] = ()) -> str:
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        print(f"  created role {name}")
        time.sleep(10)  # IAM propagation
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  role {name} exists (trust policy refreshed)")
    iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


def zip_files(files: dict[str, Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, path in files.items():
            z.write(path, arcname)
    return buf.getvalue()


def ensure_lambda(lam, name: str, role_arn: str, code: bytes, env: dict, account: str, timeout=60, memory=512) -> str:
    try:
        arn = lam.create_function(FunctionName=name, Runtime="python3.12", Role=role_arn, Handler="handler.lambda_handler",
                                  Code={"ZipFile": code}, Timeout=timeout, MemorySize=memory,
                                  Environment={"Variables": env}, Architectures=["arm64"])["FunctionArn"]
        print(f"  created function {name}")
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=name, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, Role=role_arn, Environment={"Variables": env},
                                          Timeout=timeout, MemorySize=memory)
        arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
        print(f"  updated function {name}")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    try:
        lam.add_permission(FunctionName=name, StatementId="agentcore-gateway", Action="lambda:InvokeFunction",
                           Principal="bedrock-agentcore.amazonaws.com", SourceAccount=account)
    except lam.exceptions.ResourceConflictException:
        pass
    return arn


def find_gateway(ctl, name: str) -> dict | None:
    token = None
    while True:
        resp = ctl.list_gateways(maxResults=50, **({"nextToken": token} if token else {}))
        for gw in resp.get("items", []):
            if gw.get("name") == name:
                return ctl.get_gateway(gatewayIdentifier=gw["gatewayId"])
        token = resp.get("nextToken")
        if not token:
            return None


def wait_ready(get, what: str):
    for _ in range(90):
        status = get()["status"]
        if status == "READY":
            return
        if "FAILED" in status:
            raise SystemExit(f"{what} failed: {get().get('statusReasons')}")
        time.sleep(10)
    raise SystemExit(f"{what} not READY after 15 minutes")


def ensure_gateway(ctl, name: str, **kwargs) -> dict:
    gw = find_gateway(ctl, name)
    if gw:
        print(f"  gateway {name} exists: {gw['gatewayId']}")
    else:
        gw = ctl.create_gateway(name=name, **kwargs)
        print(f"  created gateway {name}: {gw['gatewayId']}")
    wait_ready(lambda: ctl.get_gateway(gatewayIdentifier=gw["gatewayId"]), f"gateway {name}")
    return ctl.get_gateway(gatewayIdentifier=gw["gatewayId"])


def ensure_target(ctl, gateway_id: str, name: str, **kwargs) -> str:
    for t in ctl.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=50).get("items", []):
        if t["name"] == name:
            print(f"  target {name} exists: {t['targetId']}")
            return t["targetId"]
    target_id = ctl.create_gateway_target(gatewayIdentifier=gateway_id, name=name, **kwargs)["targetId"]
    print(f"  created target {name}: {target_id}")
    wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id), f"target {name}")
    return target_id


# ---------------------------------------------------------------------------------------------------- steps
def deploy_cognito(cog, state: dict, alias: str, password: str | None) -> None:
    step(f"Cognito user pool + app client + user '{alias}'")
    if "user_pool_id" not in state:
        pool = cog.create_user_pool(PoolName=f"{PREFIX}-users", Policies={"PasswordPolicy": {
            "MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True, "RequireNumbers": True,
            "RequireSymbols": True}}, AdminCreateUserConfig={"AllowAdminCreateUserOnly": True})["UserPool"]
        state["user_pool_id"] = pool["Id"]
    pool_id = state["user_pool_id"]
    if "client_id" not in state:
        state["client_id"] = cog.create_user_pool_client(
            UserPoolId=pool_id, ClientName=f"{PREFIX}-client", GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            AccessTokenValidity=60, TokenValidityUnits={"AccessToken": "minutes"})["UserPoolClient"]["ClientId"]
    try:
        cog.admin_create_user(UserPoolId=pool_id, Username=alias, MessageAction="SUPPRESS")
        created = True
    except cog.exceptions.UsernameExistsException:
        created = False
    if created or password:
        pw = password or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14)) + "#9aA"
        cog.admin_set_user_password(UserPoolId=pool_id, Username=alias, Password=pw, Permanent=True)
        if not password:
            print(f"  generated password for {alias}: {pw}   (shown once; store it in your password manager)")
    state["discovery_url"] = (f"https://cognito-idp.{state['region']}.amazonaws.com/{pool_id}"
                              "/.well-known/openid-configuration")
    state["test_user"] = alias
    print(f"  user pool {pool_id}, client {state['client_id']}")


def deploy_lambdas(iam, lam, s3, state: dict, account: str, region: str) -> None:
    step("Artifact bucket + Lambda functions (alias interceptor, post-processing hook)")
    bucket = state.setdefault("artifact_bucket", f"{PREFIX}-artifacts-{account}-{region}")
    try:
        s3.create_bucket(Bucket=bucket, **({} if region == "us-east-1" else
                                            {"CreateBucketConfiguration": {"LocationConstraint": region}}))
        print(f"  created bucket {bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "TLSOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))

    role = ensure_role(iam, f"{PREFIX}-lambda-role",
                       {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                                 "Principal": {"Service": "lambda.amazonaws.com"}}]},
                       {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:PutObject",
                                                                 "Resource": f"arn:aws:s3:::{bucket}/diagrams/*"}]},
                       ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    state["interceptor_arn"] = ensure_lambda(
        lam, f"{PREFIX}-alias-interceptor", role,
        zip_files({"handler.py": HERE / "lambdas/alias_interceptor/handler.py"}),
        {"ALIAS_CLAIMS": "alias,username,cognito:username,preferred_username", "REQUIRE_ALIAS": "true"},
        account, timeout=10, memory=256)
    state["hook_arn"] = ensure_lambda(
        lam, f"{PREFIX}-postprocess-hook", role,
        zip_files({"handler.py": HERE / "lambdas/postprocess/handler.py",
                   "drawio_validator.py": ROOT / "drawio_validator.py",
                   "drawio_icons.json": ROOT / "drawio_icons.json"}),
        {"ARTIFACT_BUCKET": bucket}, account, timeout=60, memory=1024)


def deploy_tools_gateway(iam, ctl, state: dict, account: str, region: str) -> None:
    step("Tools gateway (MCP, AWS_IAM inbound): web-search connector + post-processing hook")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction",
         "Resource": [state["interceptor_arn"], state["hook_arn"]]},
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch",
         "Resource": f"arn:aws:bedrock-agentcore:{region}:aws:tool/web-search.v1"},
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"}]}
    role = ensure_role(iam, f"{PREFIX}-tools-gw-role", trust_policy(account, region, None), policy)
    gw = ensure_gateway(ctl, TOOLS_GATEWAY, roleArn=role, protocolType="MCP", authorizerType="AWS_IAM",
                        interceptorConfigurations=[{
                            "interceptor": {"lambda": {"arn": state["interceptor_arn"]}},
                            "interceptionPoints": ["REQUEST"], "inputConfiguration": {"passRequestHeaders": True}}],
                        description="Tools for the CSP architect agent (IAM only)")
    iam.update_assume_role_policy(RoleName=f"{PREFIX}-tools-gw-role",
                                  PolicyDocument=json.dumps(trust_policy(account, region, gw["gatewayArn"])))
    state.update(tools_gateway_id=gw["gatewayId"], tools_gateway_arn=gw["gatewayArn"],
                 tools_gateway_url=gw["gatewayUrl"])
    alias_headers = {"allowedRequestHeaders": [TOOLS_ALIAS_HEADER]}
    connector = {"targetConfiguration": {"mcp": {"connector": {
        "source": {"connectorId": "web-search"}, "configurations": [{"name": "WebSearch", "parameterValues": {}}]}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]}
    try:
        ensure_target(ctl, gw["gatewayId"], WEB_SEARCH_TARGET, metadataConfiguration=alias_headers, **connector)
    except ClientError as e:  # connectors may not accept header propagation; the interceptor still logs the alias
        print(f"  web-search with header allowlist rejected ({e.response['Error']['Message']}); retrying without")
        ensure_target(ctl, gw["gatewayId"], WEB_SEARCH_TARGET, **connector)
    ensure_target(ctl, gw["gatewayId"], HOOK_TARGET, metadataConfiguration=alias_headers,
                  credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
                  targetConfiguration={"mcp": {"lambda": {"lambdaArn": state["hook_arn"], "toolSchema": {
                      "inlinePayload": [{
                          "name": "process_architecture",
                          "description": "Post-process a generated draw.io architecture: validate, fix icons, "
                                         "store the artifact and audit-log the caller.",
                          "inputSchema": {"type": "object", "required": ["xml"], "properties": {
                              "xml": {"type": "string"}, "explanation": {"type": "string"},
                              "mode": {"type": "string"}, "session_id": {"type": "string"},
                              "agent_validation_ok": {"type": "boolean"}}}}]}}}})


def update_runtime(iam, ctl, state: dict) -> None:
    step("Runtime: allow the alias header, point it at the tools gateway, let its role call the tools gateway (IAM)")
    runtime_id = state["runtime_arn"].split("/")[-1]
    current = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    keep = ["agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
            "lifecycleConfiguration", "authorizerConfiguration", "metadataConfiguration",
            "filesystemConfigurations", "description"]
    kwargs = {k: current[k] for k in keep if current.get(k) is not None}
    env = dict(current.get("environmentVariables") or {})
    env.update(TOOLS_GATEWAY_URL=state["tools_gateway_url"], REQUIRE_CALLER_ALIAS="true",
               WEB_SEARCH_TARGET=WEB_SEARCH_TARGET, HOOK_TARGET=HOOK_TARGET)
    headers = set((current.get("requestHeaderConfiguration") or {}).get("requestHeaderAllowlist", []))
    headers.add(USER_ALIAS_HEADER)
    ctl.update_agent_runtime(agentRuntimeId=runtime_id, environmentVariables=env,
                             requestHeaderConfiguration={"requestHeaderAllowlist": sorted(headers)}, **kwargs)
    wait_ready(lambda: ctl.get_agent_runtime(agentRuntimeId=runtime_id), "runtime update")
    role_name = current["roleArn"].split("/")[-1]
    iam.put_role_policy(RoleName=role_name, PolicyName=f"{PREFIX}-invoke-tools-gateway", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
                                                "Resource": state["tools_gateway_arn"]}]}))
    state["runtime_role_arn"] = current["roleArn"]
    print(f"  runtime {runtime_id}: header allowlist {sorted(headers)}, TOOLS_GATEWAY_URL set")


def deploy_ingress_gateway(iam, ctl, state: dict, account: str, region: str, allowed_aliases: list[str]) -> None:
    step("Ingress gateway (CUSTOM_JWT) + interceptor + AgentCore Runtime target (GATEWAY_IAM_ROLE)")
    rt = state["runtime_arn"]
    role = ensure_role(iam, f"{PREFIX}-ingress-gw-role", trust_policy(account, region, None), {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": state["interceptor_arn"]},
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
             "Resource": [rt, f"{rt}/runtime-endpoint/*"]}]})
    jwt = {"discoveryUrl": state["discovery_url"], "allowedClients": [state["client_id"]]}
    if allowed_aliases:  # optional: only these Cognito usernames may call the gateway at all
        jwt["customClaims"] = [{"inboundTokenClaimName": "username", "inboundTokenClaimValueType": "STRING",
                                "authorizingClaimMatchValue": {"claimMatchOperator": "EQUALS",
                                                               "claimMatchValue": {"matchValueString": a}}}
                               for a in allowed_aliases]
    gw = ensure_gateway(ctl, INGRESS_GATEWAY, roleArn=role, authorizerType="CUSTOM_JWT",
                        authorizerConfiguration={"customJWTAuthorizer": jwt},
                        interceptorConfigurations=[{
                            "interceptor": {"lambda": {"arn": state["interceptor_arn"]}},
                            "interceptionPoints": ["REQUEST"], "inputConfiguration": {"passRequestHeaders": True}}],
                        description="Private ingress for the CSP architect agent (JWT at the edge, IAM inside)")
    iam.update_assume_role_policy(RoleName=f"{PREFIX}-ingress-gw-role",
                                  PolicyDocument=json.dumps(trust_policy(account, region, gw["gatewayArn"])))
    state.update(ingress_gateway_id=gw["gatewayId"], ingress_gateway_arn=gw["gatewayArn"],
                 ingress_gateway_url=gw["gatewayUrl"], ingress_role_arn=role, runtime_target=RUNTIME_TARGET)
    ensure_target(ctl, gw["gatewayId"], RUNTIME_TARGET,
                  targetConfiguration={"http": {"agentcoreRuntime": {
                      "arn": rt, "qualifier": "DEFAULT",
                      "schema": {"source": {"inlinePayload": (HERE / "schemas/runtime-openapi.yaml").read_text()}}}}},
                  credentialProviderConfigurations=[{
                      "credentialProviderType": "GATEWAY_IAM_ROLE",
                      "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore",
                                                                       "region": region}}}],
                  metadataConfiguration={"allowedRequestHeaders": [USER_ALIAS_HEADER]})


def harden_runtime(ctl, state: dict, extra_principals: list[str]) -> None:
    step("Runtime resource policy: only the ingress gateway role may invoke (explicit Deny for everyone else)")
    rt = state["runtime_arn"]
    allowed = [state["ingress_role_arn"], *extra_principals]
    for resource in (rt, f"{rt}/runtime-endpoint/DEFAULT"):
        policy = {"Version": "2012-10-17", "Statement": [
            {"Sid": "AllowOnlyIngressGatewayRole", "Effect": "Allow", "Principal": {"AWS": allowed},
             "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": resource},
            {"Sid": "DenyEveryoneElse", "Effect": "Deny", "Principal": {"AWS": "*"},
             "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": resource,
             "Condition": {"ArnNotEquals": {"aws:PrincipalArn": allowed}}}]}
        try:
            ctl.put_resource_policy(resourceArn=resource, policy=json.dumps(policy))
            print(f"  policy set on {resource}")
        except ClientError as e:
            print(f"  ! could not set policy on {resource}: {e.response['Error']['Message']}")


def deploy_vpc_endpoint(ec2, state: dict, region: str, vpc_id: str, subnet_ids: list[str], sg_id: str | None) -> None:
    step("Interface VPC endpoint (PrivateLink) for the gateway data plane")
    if not sg_id:
        cidr = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlock"]
        try:
            sg_id = ec2.create_security_group(GroupName=f"{PREFIX}-gateway-vpce", VpcId=vpc_id,
                                              Description="HTTPS to AgentCore Gateway endpoint")["GroupId"]
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                "IpRanges": [{"CidrIp": cidr, "Description": "VPC clients"}]}])
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
                raise
            sg_id = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-gateway-vpce"]},
                                                          {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"][0]["GroupId"]
    # Only our two gateways are reachable through this endpoint. The tools gateway is listed for a runtime in VPC
    # network mode, whose SigV4 calls would resolve to this endpoint through private DNS. That gateway still
    # requires IAM auth itself.
    endpoint_policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": "*", "Action": "bedrock-agentcore:InvokeGateway",
        "Resource": [state["ingress_gateway_arn"], state["tools_gateway_arn"]]}]}
    service = f"com.amazonaws.{region}.bedrock-agentcore.gateway"
    existing = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                   {"Name": "service-name", "Values": [service]}])["VpcEndpoints"]
    if existing:
        vpce = existing[0]["VpcEndpointId"]
        ec2.modify_vpc_endpoint(VpcEndpointId=vpce, PolicyDocument=json.dumps(endpoint_policy))
        print(f"  endpoint {vpce} exists (policy updated)")
    else:
        vpce = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc_id, ServiceName=service,
                                       SubnetIds=subnet_ids, SecurityGroupIds=[sg_id], PrivateDnsEnabled=True,
                                       PolicyDocument=json.dumps(endpoint_policy),
                                       TagSpecifications=[{"ResourceType": "vpc-endpoint",
                                                           "Tags": [{"Key": "Name", "Value": f"{PREFIX}-gateway"}]}]
                                       )["VpcEndpoint"]["VpcEndpointId"]
        print(f"  created endpoint {vpce}; waiting for available...")
        for _ in range(60):
            if ec2.describe_vpc_endpoints(VpcEndpointIds=[vpce])["VpcEndpoints"][0]["State"] == "available":
                break
            time.sleep(10)
    state.update(vpc_id=vpc_id, vpce_id=vpce, vpce_security_group=sg_id)


def restrict_gateway_to_vpce(ctl, state: dict) -> None:
    step("Ingress gateway resource policy: only requests that arrive through the VPC endpoint")
    arn, vpce = state["ingress_gateway_arn"], state["vpce_id"]
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "AllowViaVpce", "Effect": "Allow", "Principal": "*", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": arn, "Condition": {"StringEquals": {"aws:SourceVpce": vpce}}},
        {"Sid": "DenyOutsideVpce", "Effect": "Deny", "Principal": "*", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": arn, "Condition": {"StringNotEquals": {"aws:SourceVpce": vpce}}}]}
    ctl.put_resource_policy(resourceArn=arn, policy=json.dumps(policy))
    print(f"  {arn} accepts only {vpce}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region")
    ap.add_argument("--runtime-arn", help="default: .bedrock_agentcore.yaml")
    ap.add_argument("--vpc-id")
    ap.add_argument("--subnet-ids", help="comma-separated, one per AZ")
    ap.add_argument("--security-group-id", help="for the endpoint ENIs (default: create one allowing 443 from the VPC)")
    ap.add_argument("--test-user", default="yhtsao", help="Cognito username = alias claim")
    ap.add_argument("--password", help="set the test user's password (default: generate on first create)")
    ap.add_argument("--allowed-alias", action="append", default=[],
                    help="optional: gateway customClaims so only these usernames are accepted (repeatable)")
    ap.add_argument("--allow-invoke-principal", action="append", default=[],
                    help="extra IAM principal ARN allowed to invoke the runtime directly (break-glass)")
    ap.add_argument("--update-runtime-only", action="store_true",
                    help="re-apply runtime env vars + header allowlist after `agentcore deploy` overwrote them")
    ap.add_argument("--skip-vpc", action="store_true", help="deploy everything except the VPC endpoint + policy")
    args = ap.parse_args()

    yaml_arn, yaml_region = runtime_from_yaml()
    state = load_state()
    region = args.region or state.get("region") or yaml_region or boto3.Session().region_name or "us-east-1"
    state["region"] = region
    state["runtime_arn"] = args.runtime_arn or state.get("runtime_arn") or yaml_arn
    if not state["runtime_arn"]:
        sys.exit("No runtime ARN: run `agentcore deploy` first or pass --runtime-arn")
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    iam, ctl = session.client("iam"), session.client("bedrock-agentcore-control")

    try:
        if args.update_runtime_only:
            update_runtime(iam, ctl, state)
            return
        if not args.skip_vpc and not (args.vpc_id and args.subnet_ids) and "vpce_id" not in state:
            sys.exit("--vpc-id and --subnet-ids are required (or --skip-vpc)")
        password = args.password or (getpass.getpass(f"Password for {args.test_user} (blank = generate): ") or None
                                     if sys.stdin.isatty() and "user_pool_id" not in state else None)
        deploy_cognito(session.client("cognito-idp"), state, args.test_user, password)
        save_state(state)
        deploy_lambdas(iam, session.client("lambda"), session.client("s3"), state, account, region)
        save_state(state)
        deploy_tools_gateway(iam, ctl, state, account, region)
        save_state(state)
        update_runtime(iam, ctl, state)
        save_state(state)
        deploy_ingress_gateway(iam, ctl, state, account, region, args.allowed_alias)
        save_state(state)
        harden_runtime(ctl, state, args.allow_invoke_principal)
        if not args.skip_vpc:
            deploy_vpc_endpoint(session.client("ec2"), state, region, args.vpc_id or state["vpc_id"],
                                (args.subnet_ids or "").split(",") if args.subnet_ids else [], args.security_group_id)
            save_state(state)
            restrict_gateway_to_vpce(ctl, state)
    finally:
        save_state(state)
    print(f"\n✔ Done. State: {STATE_FILE}")
    print(f"  Invoke URL: {state['ingress_gateway_url'].rstrip('/')}/{RUNTIME_TARGET}/invocations  (from inside {state.get('vpc_id', 'the VPC')})")
    print("  Test:       python gateway/client.py --all-checks")


if __name__ == "__main__":
    main()
