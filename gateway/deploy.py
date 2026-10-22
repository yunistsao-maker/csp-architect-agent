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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portal_deploy as pd  # noqa: E402  (gateway/portal_deploy.py: portal agents, sessions, uploads, git-pr)

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


def ensure_lambda(lam, name: str, role_arn: str, code: bytes, env: dict, account: str, timeout=60, memory=512,
                  permission: bool = True) -> str:
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
    if not permission:
        return arn
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
        if t["name"] == name:  # re-apply the configuration so schema / header changes take effect
            ctl.update_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"], name=name, **kwargs)
            wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"]),
                       f"target {name}")
            print(f"  target {name} updated: {t['targetId']}")
            return t["targetId"]
    target_id = ctl.create_gateway_target(gatewayIdentifier=gateway_id, name=name, **kwargs)["targetId"]
    print(f"  created target {name}: {target_id}")
    wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id), f"target {name}")
    return target_id


# ---------------------------------------------------------------------------------------------------- steps
USERS_FILE = HERE / ".test-users.json"


def fake_user_names(n: int) -> list[str]:
    return [f"fake-user-{i:02d}" for i in range(1, n + 1)]


def _password() -> str:
    # meets the pool policy: 12+ chars with upper, lower, digit and symbol
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)) + "#9aA"


def deploy_cognito(cog, state: dict, users: list[str], password: str | None = None, reset: bool = False,
                   users_file: Path = USERS_FILE) -> dict:
    """Test identity provider: a Cognito user pool whose access tokens carry `username` = the alias.

    Creates every user in `users` (the first is the primary test user). Generated passwords go to
    gateway/.test-users.json (mode 600, git-ignored), never to the console. `password` sets the first user's
    password; `reset` regenerates everyone's."""
    step(f"Cognito user pool + app client + test users: {', '.join(users)}")
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
    saved = json.loads(users_file.read_text()) if users_file.exists() else {}
    for i, alias in enumerate(users):
        try:
            cog.admin_create_user(UserPoolId=pool_id, Username=alias, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "name", "Value": f"Test user {alias}"}])
            created = True
        except cog.exceptions.UsernameExistsException:
            created = False
        pw = password if (i == 0 and password) else None
        if pw or created or reset or alias not in saved:
            pw = pw or _password()
            cog.admin_set_user_password(UserPoolId=pool_id, Username=alias, Password=pw, Permanent=True)
            saved[alias] = pw
        print(f"  user {alias}: {'created' if created else 'exists'}")
    users_file.touch(mode=0o600, exist_ok=True)
    users_file.chmod(0o600)
    users_file.write_text(json.dumps(saved, indent=1))
    state["discovery_url"] = (f"https://cognito-idp.{state['region']}.amazonaws.com/{pool_id}"
                              "/.well-known/openid-configuration")
    state["test_user"] = users[0]
    state["test_users"] = list(dict.fromkeys([*state.get("test_users", []), *users]))
    print(f"  user pool {pool_id}, client {state['client_id']}; passwords in {users_file} (mode 600)")
    return saved


def deploy_lambdas(iam, lam, s3, state: dict, account: str, region: str, acl: dict | None = None) -> None:
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

    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": "expire-job-records", "Status": "Enabled", "Filter": {"Prefix": "jobs/"},
        "Expiration": {"Days": 7}}]})
    role = ensure_role(iam, f"{PREFIX}-lambda-role",
                       {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                                 "Principal": {"Service": "lambda.amazonaws.com"}}]},
                       {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:PutObject",
                                                                 "Resource": f"arn:aws:s3:::{bucket}/diagrams/*"}]},
                       ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    state["interceptor_arn"] = ensure_lambda(
        lam, f"{PREFIX}-alias-interceptor", role,
        zip_files({"handler.py": HERE / "lambdas/alias_interceptor/handler.py"}),
        {"ALIAS_CLAIMS": "alias,username,cognito:username,preferred_username,upn", "REQUIRE_ALIAS": "true",
         **({"TOOL_ACL": json.dumps(acl, separators=(",", ":"))} if acl else {})},
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
         "Resource": [state["interceptor_arn"], state["hook_arn"], *([state["git_pr_arn"]] if state.get("git_pr_arn") else [])]},
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
    if state.get("git_pr_arn"):
        try:  # only this gateway may invoke git-pr (it trusts the client context for alias / agent kind)
            boto3.client("lambda", region_name=region).add_permission(
                FunctionName=state["git_pr_arn"], StatementId="tools-gateway-only", Action="lambda:InvokeFunction",
                Principal="bedrock-agentcore.amazonaws.com", SourceArn=gw["gatewayArn"], SourceAccount=account)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise
        ensure_target(ctl, gw["gatewayId"], pd.GIT_TARGET, **pd.git_target_config(state))


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
               WEB_SEARCH_TARGET=WEB_SEARCH_TARGET, HOOK_TARGET=HOOK_TARGET,
               # async jobs: a gateway call ends after 15 minutes, so submit + poll; job state in S3
               ASYNC_DEFAULT="true", JOB_BUCKET=state["artifact_bucket"])
    headers = set((current.get("requestHeaderConfiguration") or {}).get("requestHeaderAllowlist", []))
    headers.add(USER_ALIAS_HEADER)
    ctl.update_agent_runtime(agentRuntimeId=runtime_id, environmentVariables=env,
                             requestHeaderConfiguration={"requestHeaderAllowlist": sorted(headers)}, **kwargs)
    wait_ready(lambda: ctl.get_agent_runtime(agentRuntimeId=runtime_id), "runtime update")
    role_name = current["roleArn"].split("/")[-1]
    iam.put_role_policy(RoleName=role_name, PolicyName=f"{PREFIX}-invoke-tools-gateway", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": state["tools_gateway_arn"]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
             "Resource": f"arn:aws:s3:::{state['artifact_bucket']}/jobs/*"}]}))
    state["runtime_role_arn"] = current["roleArn"]
    print(f"  runtime {runtime_id}: header allowlist {sorted(headers)}, TOOLS_GATEWAY_URL set, "
          f"async jobs in s3://{state['artifact_bucket']}/jobs/")


def deploy_ingress_gateway(iam, ctl, state: dict, account: str, region: str, allowed_aliases: list[str]) -> None:
    step("Ingress gateway (CUSTOM_JWT) + interceptor + AgentCore Runtime target (GATEWAY_IAM_ROLE)")
    rt = state["runtime_arn"]
    runtimes = all_runtime_arns(state)
    role = ensure_role(iam, f"{PREFIX}-ingress-gw-role", trust_policy(account, region, None), {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": state["interceptor_arn"]},
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
             "Resource": [r for a in runtimes for r in (a, f"{a}/runtime-endpoint/*")]}]})
    jwt = {"discoveryUrl": state["discovery_url"]}
    if state.get("allowed_clients"):
        jwt["allowedClients"] = state["allowed_clients"]
    if state.get("allowed_audience"):
        jwt["allowedAudience"] = state["allowed_audience"]
    if len(jwt) == 1:
        sys.exit("The JWT authorizer needs --allowed-client and/or --allowed-audience")
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
    for key, a in (state.get("agents") or {}).items():
        if a.get("portal"):
            ensure_target(ctl, gw["gatewayId"], a["target"], **runtime_target_config(a["runtime_arn"], region,
                                                                                   "schemas/portal-agent-openapi.yaml"))


def runtime_target_config(arn: str, region: str, schema: str) -> dict:
    return {"targetConfiguration": {"http": {"agentcoreRuntime": {
                "arn": arn, "qualifier": "DEFAULT", "schema": {"source": {"inlinePayload": (HERE / schema).read_text()}}}}},
            "credentialProviderConfigurations": [{
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore", "region": region}}}],
            "metadataConfiguration": {"allowedRequestHeaders": [USER_ALIAS_HEADER]}}


def all_runtime_arns(state: dict) -> list[str]:
    arns = [state["runtime_arn"], *[a["runtime_arn"] for a in (state.get("agents") or {}).values()]]
    return list(dict.fromkeys(arns))


def harden_runtime(ctl, state: dict, extra_principals: list[str]) -> None:
    step("Runtime resource policies: only the ingress gateway role may invoke (explicit Deny for everyone else)")
    allowed = [state["ingress_role_arn"], *extra_principals]
    for resource in [r for rt in all_runtime_arns(state) for r in (rt, f"{rt}/runtime-endpoint/DEFAULT")]:
        policy = pd.runtime_resource_policy(resource, allowed)
        try:
            ctl.put_resource_policy(resourceArn=resource, policy=json.dumps(policy))
            print(f"  policy set on {resource}")
        except ClientError as e:  # fail closed: without it any IAM principal could call the runtime directly
            sys.exit(f"Could not set the resource policy on {resource}: {e.response['Error']['Message']}")


def endpoint_security_group(ec2, vpc_id: str, client_cidrs: list[str]) -> str:
    """HTTPS into the endpoint ENIs from the VPC (runtimes, Lambda) and from on-prem clients (portal over DX/TGW)."""
    cidr = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlock"]
    try:
        sg_id = ec2.create_security_group(GroupName=f"{PREFIX}-gateway-vpce", VpcId=vpc_id,
                                          Description="HTTPS to the VPC endpoints from the VPC and on-prem")["GroupId"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        sg_id = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-gateway-vpce"]},
                                                      {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"][0]["GroupId"]
    for c, desc in [(cidr, "VPC"), *[(c, "on-prem clients") for c in client_cidrs]]:
        try:
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": c, "Description": desc}]}])
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise
    return sg_id


def deploy_vpc_endpoint(ec2, state: dict, region: str, vpc_id: str, subnet_ids: list[str], sg_id: str | None,
                        client_cidrs: list[str] = ()) -> None:
    step("Interface VPC endpoint (PrivateLink) for the gateway data plane")
    if not sg_id:
        sg_id = endpoint_security_group(ec2, vpc_id, list(client_cidrs))
        if not client_cidrs:
            print("  ! no --client-cidr given: only clients inside the VPC can reach the endpoint")
    # Only our two gateways are reachable through this endpoint. The tools gateway is listed for a runtime in VPC
    # network mode, whose SigV4 calls would resolve to this endpoint through private DNS. That gateway still
    # requires IAM auth itself.
    endpoint_policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": "*", "Action": "bedrock-agentcore:InvokeGateway",
        "Resource": [state["ingress_gateway_arn"], state["tools_gateway_arn"]]}]}
    service = f"com.amazonaws.{region}.bedrock-agentcore.gateway"
    existing = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                   {"Name": "service-name", "Values": [service]}])["VpcEndpoints"]
    existing = [e for e in existing if e.get("State", "").lower() not in ("deleted", "deleting", "failed", "rejected")]
    created = state.get("vpce_created", False) and state.get("vpce_id")
    if existing:
        vpce = existing[0]["VpcEndpointId"]
        if created == vpce:
            ec2.modify_vpc_endpoint(VpcEndpointId=vpce, PolicyDocument=json.dumps(endpoint_policy))
            print(f"  endpoint {vpce} exists (policy updated)")
        else:  # someone else's endpoint: don't narrow its policy, don't delete it on cleanup
            print(f"  ! reusing existing endpoint {vpce}; its policy was NOT changed - allow "
                  f"{state['ingress_gateway_arn']} and {state['tools_gateway_arn']} in it yourself")
            state["vpce_created"] = False
    else:
        vpce = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc_id, ServiceName=service,
                                       SubnetIds=subnet_ids, SecurityGroupIds=[sg_id], PrivateDnsEnabled=True,
                                       PolicyDocument=json.dumps(endpoint_policy),
                                       TagSpecifications=[{"ResourceType": "vpc-endpoint",
                                                           "Tags": [{"Key": "Name", "Value": f"{PREFIX}-gateway"}]}]
                                       )["VpcEndpoint"]["VpcEndpointId"]
        state["vpce_created"] = vpce
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


def update_all_runtimes(iam, ctl, state: dict, cfg: dict, account: str, region: str, vpc: dict | None) -> None:
    update_runtime(iam, ctl, state)
    for key, a in state.get("agents", {}).items():
        if a.get("portal"):
            step(f"Portal runtime '{key}': env, header allowlist, role policy" + (", VPC mode" if vpc else ""))
            pd.update_portal_runtime(iam, ctl, state, key, a["kind"], cfg, account, region, wait_ready, vpc)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region")
    ap.add_argument("--runtime-arn", help="CSP architect runtime (default: .bedrock_agentcore.yaml)")
    ap.add_argument("--runtime", action="append", default=[], metavar="KEY=ARN",
                    help="runtime ARN for an agent in gateway/agents.yaml, e.g. terraform=arn:... (repeatable)")
    ap.add_argument("--agents-config", default=str(pd.CONFIG_FILE))
    ap.add_argument("--vpc-id")
    ap.add_argument("--subnet-ids", help="comma-separated private subnets, one per AZ")
    ap.add_argument("--security-group-id", help="for the endpoint ENIs (default: create one, 443 from the VPC + --client-cidr)")
    ap.add_argument("--client-cidr", action="append", default=[],
                    help="on-prem CIDR of the portal backend (reaches the endpoints over DX / TGW; repeatable)")
    ap.add_argument("--runtime-vpc", action="store_true",
                    help="put the portal runtimes in VPC network mode and create their VPC endpoints "
                         "(bedrock-runtime, logs, secretsmanager, S3 / DynamoDB gateway endpoints)")
    ap.add_argument("--discovery-url", help="corporate IdP OIDC discovery URL (skips the Cognito test user pool)")
    ap.add_argument("--allowed-client", action="append", default=[], help="JWT client_id(s) to accept (repeatable)")
    ap.add_argument("--allowed-audience", action="append", default=[], help="JWT aud value(s) to accept (repeatable)")
    ap.add_argument("--test-user", action="append", default=[],
                    help="Cognito username = alias claim (repeatable; default yhtsao). The first is the primary user")
    ap.add_argument("--fake-users", type=int, default=2,
                    help="also create N fake users fake-user-01.. for multi-user / isolation tests (default 2)")
    ap.add_argument("--password", help="set the primary test user's password (default: generated)")
    ap.add_argument("--reset-passwords", action="store_true", help="generate new passwords for all test users")
    ap.add_argument("--allowed-alias", action="append", default=[],
                    help="optional: gateway customClaims so only these usernames are accepted (repeatable)")
    ap.add_argument("--allow-invoke-principal", action="append", default=[],
                    help="extra IAM principal ARN allowed to invoke the runtimes directly (break-glass)")
    ap.add_argument("--update-runtime-only", action="store_true",
                    help="re-apply runtime env vars + header allowlist after `agentcore deploy` overwrote them")
    ap.add_argument("--skip-vpc", action="store_true", help="deploy everything except the VPC endpoints + policies")
    args = ap.parse_args()

    cfg = pd.load_config(Path(args.agents_config))
    yaml_arn, yaml_region = runtime_from_yaml()
    state = load_state()
    region = args.region or state.get("region") or yaml_region or boto3.Session().region_name or "us-east-1"
    state["region"] = region
    arns = pd.runtime_arns_from_yaml(cfg, dict(x.split("=", 1) for x in args.runtime))
    state["runtime_arn"] = args.runtime_arn or state.get("runtime_arn") or arns.get("csp-architect") or yaml_arn
    if not state["runtime_arn"]:
        sys.exit("No runtime ARN: run `agentcore deploy` first or pass --runtime-arn")
    state["agents"] = {}
    for key, a in pd.portal_agents(cfg).items():
        if key in arns:
            state["agents"][key] = {"runtime_arn": arns[key], "target": a["target"], "kind": a["kind"], "portal": True}
        else:
            print(f"  (agent '{key}' has no runtime yet: `agentcore configure -e {a['entrypoint']} -n {a['agent_name']}`"
                  " + `agentcore deploy`, then re-run)")
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    iam, ctl, ec2 = session.client("iam"), session.client("bedrock-agentcore-control"), session.client("ec2")
    vpc_id = args.vpc_id or state.get("vpc_id")
    subnet_ids = args.subnet_ids.split(",") if args.subnet_ids else state.get("subnet_ids", [])
    runtime_vpc = None

    if args.runtime_vpc and (args.skip_vpc or not (vpc_id and subnet_ids)):
        sys.exit("--runtime-vpc needs the VPC endpoints: pass --vpc-id/--subnet-ids and don't use --skip-vpc")
    try:
        if args.runtime_vpc:
            state["subnet_ids"] = subnet_ids
            runtime_vpc = {"subnet_ids": subnet_ids,
                           "security_group_id": state.get("runtime_sg") or pd.runtime_security_group(ec2, vpc_id)}
            state["runtime_sg"] = runtime_vpc["security_group_id"]
        if args.update_runtime_only:
            if runtime_vpc and not state.get("s3_interface_vpce"):
                sys.exit("Run a full deploy with --runtime-vpc first (creates the endpoints VPC-mode runtimes need)")
            update_all_runtimes(iam, ctl, state, cfg, account, region, runtime_vpc)
            return
        if not args.skip_vpc and not (vpc_id and subnet_ids):
            sys.exit("--vpc-id and --subnet-ids are required (or --skip-vpc)")
        if args.discovery_url:  # corporate IdP: no Cognito
            state.update(discovery_url=args.discovery_url, allowed_clients=args.allowed_client,
                         allowed_audience=args.allowed_audience)
        else:
            users = list(dict.fromkeys((args.test_user or ["yhtsao"]) + fake_user_names(args.fake_users)))
            deploy_cognito(session.client("cognito-idp"), state, users, args.password, args.reset_passwords)
            state.setdefault("allowed_clients", [state["client_id"]])
        save_state(state)
        deploy_lambdas(iam, session.client("lambda"), session.client("s3"), state, account, region, pd.tool_acl(cfg))
        if state["agents"]:
            step("Portal agents: session table (DynamoDB, TTL) + uploads bucket (S3, 90-day lifecycle)")
            pd.deploy_session_table(session.client("dynamodb"), state)
            pd.deploy_uploads_bucket(session.client("s3"), state, cfg, account, region)
            if cfg.get("git") and (cfg["git"].get("network") == "public" or not args.skip_vpc):
                step("git-pr Lambda (" + ("public network: github.com sandbox" if cfg["git"].get("network") == "public"
                                         else "VPC-attached: internal Git over TGW / DX") + "; token from Secrets Manager)")
                pd.deploy_git_lambda(iam, session.client("lambda"), ec2, session.client("secretsmanager"), state, cfg,
                                     account, subnet_ids, vpc_id, ensure_role, ensure_lambda, zip_files)
        save_state(state)
        deploy_tools_gateway(iam, ctl, state, account, region)
        save_state(state)
        update_all_runtimes(iam, ctl, state, cfg, account, region, None)  # env + roles; VPC mode comes last
        save_state(state)
        deploy_ingress_gateway(iam, ctl, state, account, region, args.allowed_alias)
        save_state(state)
        harden_runtime(ctl, state, args.allow_invoke_principal)
        if not args.skip_vpc:
            deploy_vpc_endpoint(ec2, state, region, vpc_id, subnet_ids, args.security_group_id, args.client_cidr)
            save_state(state)
            if state["agents"]:
                step("VPC endpoints for the runtimes / git-pr Lambda, and the S3 interface endpoint for on-prem uploads")
                pd.deploy_runtime_endpoints(ec2, state, region, vpc_id, subnet_ids, state["vpce_security_group"])
                pd.deploy_uploads_bucket(session.client("s3"), state, cfg, account, region)  # now pinned to the endpoint
                save_state(state)
            restrict_gateway_to_vpce(ctl, state)
            if runtime_vpc:  # only now: every endpoint the runtimes need exists
                update_all_runtimes(iam, ctl, state, cfg, account, region, runtime_vpc)
                save_state(state)
    finally:
        save_state(state)
    base = state["ingress_gateway_url"].rstrip("/")
    print(f"\n✔ Done. State: {STATE_FILE}")
    print(f"  CSP architect: {base}/{RUNTIME_TARGET}/invocations")
    for key, a in state["agents"].items():
        print(f"  {key + ':':14} {base}/{a['target']}/invocations")
    print("  Test:          python gateway/client.py --all-checks [--agent coe|aicoe|terraform]")


if __name__ == "__main__":
    main()
