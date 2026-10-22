"""Portal agents architecture v2: page 1 network & security path, page 2 agents & data flow.

    python gen_v2.py out_dir      -> portal-agents-architecture.drawio (2 pages) + page1.drawio + page2.drawio
"""
import sys
from pathlib import Path
from xml.sax.saxutils import escape

NET, ML, COMP, STOR, SEC, MGMT, DB = "#8C4FFF", "#01A88D", "#ED7100", "#7AA116", "#DD344C", "#E7157B", "#C925D1"
BLUE, GREEN, GRAY, PURPLE, ORANGE = "#1A73E8", "#2E7D32", "#6B7280", "#7B1FA2", "#C2410C"
TOP = "verticalLabelPosition=top;verticalAlign=bottom;"
RIGHT = "labelPosition=right;verticalLabelPosition=middle;align=left;verticalAlign=middle;"


class Page:
    def __init__(self, pid, name, w, h):
        self.pid, self.name, self.w, self.h = pid, name, w, h
        self.cells, self.edges, self.origin = [], [], {"1": (0, 0)}

    # absolute coordinates in, parent-relative out
    def _rel(self, parent, x, y):
        ox, oy = self.origin[parent]
        return x - ox, y - oy

    def group(self, cid, label, x, y, w, h, kind, parent="1"):
        kinds = {
            "cloud": "grIcon=mxgraph.aws4.group_aws_cloud;strokeColor=#232F3E;fillColor=none;fontColor=#232F3E;dashed=0;",
            "region": "grIcon=mxgraph.aws4.group_region;strokeColor=#00A4A6;fillColor=none;fontColor=#007F80;dashed=1;",
            "vpc": "grIcon=mxgraph.aws4.group_vpc2;strokeColor=#8C4FFF;fillColor=none;fontColor=#8C4FFF;dashed=0;",
            "private": "grIcon=mxgraph.aws4.group_security_group;strokeColor=#147EBA;fillColor=#E6F2F8;fontColor=#147EBA;dashed=0;",
            "onprem": "grIcon=mxgraph.aws4.group_corporate_data_center;strokeColor=#7D8998;fillColor=none;fontColor=#5A6B86;dashed=0;",
        }
        if kind in kinds:
            style = ("points=[];outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;fontSize=12;fontStyle=1;"
                     "shape=mxgraph.aws4.group;verticalAlign=top;align=left;spacingLeft=30;container=1;pointerEvents=0;"
                     "collapsible=0;recursiveResize=0;" + kinds[kind])
        else:  # plain dashed container: kind = "stroke,fill,font"
            stroke, fill, font = kind.split(",")
            style = (f"rounded=1;arcSize=2;whiteSpace=wrap;html=1;dashed=1;strokeColor={stroke};fillColor={fill};"
                     f"verticalAlign=top;align=left;spacingLeft=10;spacingTop=4;fontColor={font};fontStyle=1;fontSize=12;"
                     "container=1;collapsible=0;")
        rx, ry = self._rel(parent, x, y)
        self.cells.append((cid, label, style, rx, ry, w, h, parent))
        self.origin[cid] = (x, y)

    def icon(self, cid, label, name, color, x, y, parent="1", res=True, w=56, h=56, extra=""):
        if res:
            style = (f"sketch=0;outlineConnect=0;fontColor=#232F3E;fillColor={color};strokeColor=#ffffff;dashed=0;"
                     "verticalLabelPosition=bottom;verticalAlign=top;align=center;html=1;fontSize=11;fontStyle=0;"
                     f"aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{name};")
        else:
            style = (f"sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;fillColor={color};strokeColor=none;"
                     "dashed=0;verticalLabelPosition=bottom;verticalAlign=top;align=center;html=1;fontSize=11;fontStyle=0;"
                     f"aspect=fixed;pointerEvents=1;shape=mxgraph.aws4.{name};")
        rx, ry = self._rel(parent, x, y)
        self.cells.append((cid, label, style + extra, rx, ry, w, h, parent))

    def box(self, cid, html, x, y, w, h, fill="#FFFFFF", stroke="#9CA3AF", parent="1", size=11, align="left", extra=""):
        style = (f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;align={align};verticalAlign=top;spacing=8;fontSize={size};"
                 f"fillColor={fill};strokeColor={stroke};fontColor=#1F2937;" + extra)
        rx, ry = self._rel(parent, x, y)
        self.cells.append((cid, html, style, rx, ry, w, h, parent))

    def edge(self, cid, src, dst, label="", color=BLUE, dashed=False, both=False, exit=None, entry=None, pts=None,
             width=2):
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;strokeWidth={width};"
                 f"strokeColor={color};fontSize=10;fontColor={color};fontStyle=1;labelBackgroundColor=#FFFFFF;"
                 + ("dashed=1;" if dashed else "") + ("startArrow=block;startFill=1;" if both else ""))
        if exit:
            style += f"exitX={exit[0]};exitY={exit[1]};exitDx=0;exitDy=0;"
        if entry:
            style += f"entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;"
        self.edges.append((cid, label, style, src, dst, pts or []))

    def xml(self) -> str:
        q = {'"': "&quot;"}
        out = [f'<diagram id="{self.pid}" name="{escape(self.name, q)}"><mxGraphModel dx="1600" dy="1000" grid="1" '
               f'gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
               f'pageWidth="{self.w}" pageHeight="{self.h}" math="0" shadow="0"><root><mxCell id="0"/>'
               '<mxCell id="1" parent="0"/>']
        for cid, label, style, x, y, w, h, parent in self.cells:
            out.append(f'<mxCell id="{self.pid}-{cid}" value="{escape(label, q)}" style="{style}" vertex="1" '
                       f'parent="{parent if parent == "1" else self.pid + "-" + parent}"><mxGeometry x="{x}" y="{y}" '
                       f'width="{w}" height="{h}" as="geometry"/></mxCell>')
        for cid, label, style, src, dst, pts in self.edges:
            geo = '<mxGeometry relative="1" as="geometry">'
            if pts:
                geo += '<Array as="points">' + "".join(f'<mxPoint x="{a}" y="{b}"/>' for a, b in pts) + "</Array>"
            out.append(f'<mxCell id="{self.pid}-{cid}" value="{escape(label, q)}" style="{style}" edge="1" parent="1" '
                       f'source="{self.pid}-{src}" target="{self.pid}-{dst}">{geo}</mxGeometry></mxCell>')
        out.append("</root></mxGraphModel></diagram>")
        return "".join(out)


def mxfile(*pages) -> str:
    return '<mxfile host="drawio">' + "".join(p.xml() for p in pages) + "</mxfile>"


# ================================================================================================ page 1
def page1() -> Page:
    p = Page("net", "1. Network and security path", 2420, 1560)
    p.box("title", "<b style='font-size:18px'>Portal agents on AgentCore: on-prem portal (corporate SSO) → Direct Connect → "
          "Transit Gateway → PrivateLink → AgentCore Gateway → 4 AgentCore Runtimes</b><br>JWT is validated once at the "
          "gateway edge; every hop after that is IAM (SigV4). No internet path from on-prem or from the agent VPC. "
          "Sessions in DynamoDB and uploads in S3 expire after 90 days. Terraform / documents become pull requests in the "
          "internal Git.", 20, 10, 2380, 64, fill="none", stroke="none", size=12)
    # ---- on-prem
    p.group("onprem", "Corporate data center (on-prem)", 20, 90, 440, 1250, "onprem")
    p.icon("users", "Employees (browser)", "users", "#232F3D", 80, 140, "onprem", res=False, extra=RIGHT)
    p.icon("idp", "Corporate SSO IdP<br>(OIDC: Entra ID / Okta / ADFS)<br><i>not federated to IAM</i>",
           "single_sign_on", SEC, 320, 140, "onprem")
    p.icon("portal", "On-prem portal<br>4 agents · uploads ·<br>sessions · job polling", "traditional_server",
           "#232F3D", 80, 340, "onprem", res=False, w=44)
    p.icon("dns", "Corporate DNS<br>conditional forwarders:<br>*.gateway.bedrock-agentcore.REGION…<br>"
           "s3.REGION.amazonaws.com", "traditional_server", "#232F3D", 320, 340, "onprem", res=False, w=44)
    p.icon("fw", "Egress firewall<br>443 → endpoint IPs<br>53 → Resolver IPs", "generic_firewall", "#232F3D",
           80, 544, "onprem", res=False, h=48)
    p.icon("router", "Customer edge routers ×2<br>(BGP, MACsec)", "customer_gateway", NET, 320, 540, "onprem", extra=TOP)
    p.icon("git", "Internal Git (GitLab / GHE)<br>infra-live · design-reviews · use-cases<br>"
           "MR + CI (fmt / validate / plan)<br>+ human approval", "traditional_server", "#232F3D", 320, 760,
           "onprem", res=False, w=44)
    p.box("jwt", "<b>JWT the portal backend sends</b><br>• iss = corporate IdP · aud / client_id = portal app<br>"
          "• preferred_username / upn = <b>yhtsao</b> → alias<br>• exp ≤ 60 min, RS256 (IdP JWKS)<br><br>"
          "<b>Every agent call is short</b> (15-min gateway limit)<br>generate → {job_id} · status every 15 s · publish → PR",
          50, 920, 380, 160, fill="#EEF4FF", stroke=BLUE, parent="onprem")
    p.box("jwks", "<b>⚠ JWKS reachability</b><br>The gateway fetches the IdP's OIDC discovery document and JWKS over the "
          "internet, from the AWS side. An internal-only IdP must publish them (WAP / reverse proxy) or federate "
          "through a cloud IdP / Cognito.", 50, 1100, 380, 110, fill="#FFF7E6", stroke="#D97706", parent="onprem")
    # ---- DX / internet
    p.icon("dx", "AWS Direct Connect<br>2 connections × 2 locations<br>(maximum resiliency)", "direct_connect", NET, 567, 540)
    p.icon("internet", "Internet<br>(IdP discovery / JWKS only)", "internet_alt1", "#232F3D", 560, 120, res=False, w=64, h=40)
    # ---- AWS
    p.group("cloud", "AWS Cloud", 730, 90, 1680, 1250, "cloud")
    p.icon("dxgw", "Direct Connect<br>gateway", "direct_connect", NET, 775, 540, "cloud")
    p.group("region", "AWS Region (us-east-1 for the web-search connector)", 880, 130, 1520, 1190, "region", "cloud")
    p.icon("tgw", "Transit Gateway<br>RT: on-prem CIDR<br>↔ agent VPC", "transit_gateway", NET, 920, 540, "region")
    p.group("vpc", "Agent VPC · no IGW · no NAT", 1080, 180, 620, 1120, "vpc", "region")
    p.group("sub", "Private subnets · AZ-a + AZ-b", 1100, 220, 580, 1060, "private", "vpc")
    A, B, C = 1150, 1340, 1530
    p.icon("r53", "Route 53 Resolver<br>inbound endpoint", "route_53_resolver", NET, A, 290, "sub")
    p.icon("vpce_logs", "Interface endpoint<br><b>logs</b>", "endpoints", NET, B, 290, "sub", res=False)
    p.icon("vpce_sm", "Interface endpoint<br><b>secretsmanager</b>", "endpoints", NET, C, 290, "sub", res=False)
    p.icon("tgwa", "TGW attachment", "transit_gateway_attachment", NET, A, 540, "sub", res=False)
    p.icon("vpce_gw", "Interface endpoint<br><b>bedrock-agentcore.gateway</b><br>policy: ingress + tools gateways<br>"
           "SG 443: VPC + on-prem CIDR", "endpoints", NET, B, 540, "sub", res=False, extra=TOP)
    p.icon("eni", "AgentCore Runtime ENIs ×4<br>(VPC network mode)", "elastic_network_interface", NET, A, 710, "sub",
           res=False, extra=TOP)
    p.icon("vpce_s3if", "Interface endpoint <b>s3</b><br>on-prem uploads (presigned PUT)<br>private DNS: inbound Resolver only",
           "endpoints", NET, C, 710, "sub", res=False)
    p.icon("vpce_br", "Interface endpoint<br><b>bedrock-runtime</b><br>policy: approved Claude models", "endpoints",
           NET, B, 880, "sub", res=False)
    p.icon("geni", "git-pr Lambda ENIs<br>SG 443 → Git CIDR + VPC", "elastic_network_interface", NET, C, 880, "sub",
           res=False, extra=TOP)
    p.icon("vpce_ddb", "Gateway endpoint<br><b>dynamodb</b>", "endpoint", NET, A, 1040, "sub", res=False)
    p.icon("vpce_s3", "Gateway endpoint <b>s3</b><br>(route tables)", "endpoint", NET, B, 1040, "sub", res=False)
    p.icon("vpce_note", "", "endpoint", NET, C, 1040, "sub", res=False, w=1, h=1)  # spacer (keeps C column free)
    p.group("managed", "AWS-managed (regional): reached via PrivateLink or service-side IAM", 1720, 180, 670, 1120,
            "#01A88D,#F2FBF9,#016B5A", "region")
    M1, M2, M3 = 1770, 2010, 2250
    p.icon("cwl", "CloudWatch Logs<br>(alias in every line)", "logs", MGMT, M1, 290, "managed", res=False)
    p.icon("icpt", "Interceptor Lambda<br>ingress: JWT claim → alias header<br>tools: tool ACL per agent kind",
           "lambda", COMP, M2, 290, "managed", extra=TOP)
    p.icon("sm", "Secrets Manager<br>(Git bot token)", "secrets_manager", SEC, M3, 290, "managed")
    p.icon("ingw", "AgentCore Gateway <b>csp-arch-ingress</b><br>CUSTOM_JWT (corporate IdP)<br>4 targets · aws:SourceVpce",
           "bedrock_agentcore", ML, M1, 540, "managed")
    p.icon("rt", "AgentCore Runtimes ×4<br>csp-architect · coe-doc<br>aicoe-doc · terraform<br>policy: ingress role only",
           "bedrock_agentcore", ML, M2, 540, "managed")
    p.icon("web", "Web-search connector<br>(egress on the AWS side)", "globe", "#232F3D", M1, 710, "managed", res=False)
    p.icon("tools", "AgentCore Gateway <b>csp-arch-tools</b><br>MCP · AWS_IAM", "bedrock_agentcore", ML, M2, 710, "managed")
    p.icon("bedrock", "Amazon Bedrock<br>Claude", "bedrock", ML, M1, 880, "managed")
    p.icon("hook", "Post-processing hook<br>Lambda", "lambda", COMP, M2, 880, "managed")
    p.icon("gitfn", "git-pr<br>Lambda<br>(VPC)", "lambda", COMP, M3, 880, "managed", extra=RIGHT)
    p.icon("s3", "S3 buckets<br>artifacts: diagrams/ · jobs/ (7 d)<br>uploads: uploads/ · sessions/ (90 d)",
           "bucket", STOR, M1, 1040, "managed", res=False, h=58)
    p.icon("ddb", "DynamoDB sessions<br>PK alias#session · TTL 90 d<br>lock lease per session", "dynamodb", DB,
           M1, 1180, "managed")
    # ---- edges: user path
    p.edge("e1", "users", "portal", "① HTTPS")
    p.edge("e2", "portal", "idp", "② OIDC sign-in → JWT", both=True, exit=(1, 0.25), entry=(0, 0.5), pts=[(265, 354), (265, 168)])
    p.edge("e3", "portal", "dns", "③ resolve", GRAY, dashed=True, width=1.5)
    p.edge("e3b", "dns", "r53", "③ DNS forward (53) over DX", GRAY, dashed=True, width=1.5, exit=(0.5, 0), entry=(0, 0.5),
           pts=[(342, 318)])
    p.edge("e4a", "portal", "fw", "", exit=(0, 0.5), entry=(0, 0.5), pts=[(50, 368), (50, 568)])
    p.edge("e4b", "fw", "router", "④ HTTPS + Bearer JWT")
    p.edge("e4c", "router", "dx", "transit VIF")
    p.edge("e4d", "dx", "dxgw")
    p.edge("e4e", "dxgw", "tgw")
    p.edge("e4f", "tgw", "tgwa")
    p.edge("e4g", "tgwa", "vpce_gw")
    p.edge("e5", "vpce_gw", "ingw", "⑤")
    p.edge("e6", "ingw", "icpt", "⑥ interceptor", ML, both=True, exit=(0.5, 0), entry=(0.5, 1), pts=[(1798, 470), (2038, 470)])
    p.edge("e6b", "ingw", "internet", "JWKS fetch (AWS side)", GRAY, dashed=True, width=1.5, exit=(0, 0.15),
           entry=(0.5, 0), pts=[(1730, 548), (1730, 80), (592, 80)])
    p.edge("e6c", "internet", "idp", "", GRAY, dashed=True, width=1.5, exit=(0, 0.5), entry=(1, 0.5))
    p.edge("e7", "ingw", "rt", "⑦ SigV4 + alias", GREEN)
    # uploads
    p.edge("u1", "tgwa", "vpce_s3if", "③b presigned PUT (uploads) over DX", BLUE, dashed=True, exit=(0.5, 0),
           entry=(0.5, 0), pts=[(1178, 470), (1558, 470)])
    p.edge("u2", "vpce_s3if", "s3", "uploads/ (PUT only via this endpoint)", BLUE, dashed=True, exit=(1, 0.5),
           entry=(0.5, 0), pts=[(1650, 738), (1650, 1000), (1798, 1000)])
    # runtimes in the VPC
    p.edge("r0", "rt", "eni", "runtime ENIs in your subnets", GRAY, dashed=True, width=1.5, exit=(1, 0.5), entry=(0, 0.5),
           pts=[(2395, 568), (2395, 1310), (1125, 1310), (1125, 738)])
    p.edge("r1", "eni", "vpce_logs", "logs", MGMT, dashed=True, width=1.5, exit=(1, 0.1), entry=(0, 0.5),
           pts=[(1255, 716), (1255, 318)])
    p.edge("r1b", "vpce_logs", "cwl", "", MGMT, dashed=True, width=1.5)
    p.edge("r2", "eni", "vpce_gw", "⑨ tools: SigV4 + x-user-alias + x-agent-kind", GREEN, exit=(1, 0.3), entry=(0.5, 1),
           pts=[(1368, 727)])
    p.edge("r2b", "vpce_gw", "tools", "", GREEN, exit=(1, 0.85), entry=(0.5, 0), pts=[(1690, 588), (1690, 668), (2038, 668)])
    p.edge("r3", "eni", "vpce_br", "⑧ Claude", GREEN, exit=(1, 0.7), entry=(0, 0.5), pts=[(1270, 749), (1270, 908)])
    p.edge("r3b", "vpce_br", "bedrock", "", GREEN)
    p.edge("r4", "eni", "vpce_s3", "⑪ jobs / uploads / sessions", PURPLE, exit=(1, 0.9), entry=(0, 0.5),
           pts=[(1305, 760), (1305, 1068)])
    p.edge("r4b", "vpce_s3", "s3", "", PURPLE)
    p.edge("r5", "eni", "vpce_ddb", "⑩ sessions", DB, exit=(0.5, 1), entry=(0.5, 0))
    p.edge("r5b", "vpce_ddb", "ddb", "", DB, exit=(1, 0.9), entry=(0, 0.5), pts=[(1240, 1090), (1240, 1208)])
    # tools
    p.edge("t1", "tools", "web", "", GREEN, exit=(0, 0.5), entry=(1, 0.5))
    p.edge("t2", "tools", "hook", "hook", GREEN, exit=(1, 0.8), entry=(1, 0.5), pts=[(2160, 755), (2160, 908)])
    p.edge("t3", "tools", "gitfn", "⑫ git-pr", GREEN, exit=(1, 0.4), entry=(0.5, 0), pts=[(2278, 732)])
    p.edge("t4", "hook", "s3", "diagrams/", PURPLE, exit=(0, 0.9), entry=(1, 0.5), pts=[(1880, 930), (1880, 1069)])
    # git path
    p.edge("g1", "gitfn", "geni", "ENIs in your subnets", GRAY, dashed=True, width=1.5, exit=(0.5, 1), entry=(0.5, 1),
           pts=[(2278, 990), (1558, 990)])
    p.edge("g2", "geni", "vpce_sm", "token", SEC, dashed=True, width=1.5, exit=(1, 0.2), entry=(1, 0.5),
           pts=[(1660, 891), (1660, 318)])
    p.edge("g3", "geni", "tgwa", "⑬ HTTPS to internal Git (TGW → DX)", ORANGE, exit=(0, 0.5), entry=(0.5, 1),
           pts=[(1440, 908), (1440, 630), (1178, 630)])
    p.edge("g4", "router", "git", "⑬ merge request", ORANGE, exit=(0.5, 1), entry=(0.5, 0))
    p.box("lg", "<b>Lines</b> &nbsp; <font color='#1A73E8'>━ user path (JWT) ┅ uploads</font> &nbsp; "
          "<font color='#2E7D32'>━ IAM SigV4</font> &nbsp; <font color='#7B1FA2'>━ S3</font> &nbsp; "
          "<font color='#C925D1'>━ DynamoDB</font> &nbsp; <font color='#C2410C'>━ Git (PR)</font> &nbsp; "
          "<font color='#6B7280'>┅ DNS / JWKS / ENI placement</font> &nbsp; <font color='#E7157B'>┅ logs</font>",
          20, 1350, 1600, 30, fill="none", stroke="none")
    p.box("steps", "① Sign in to the portal with corporate SSO. ② The portal gets a JWT (not federated to IAM). "
          "③ Corporate DNS forwards the gateway and S3 names to the Route 53 inbound endpoint. ③b Files are uploaded with a "
          "presigned PUT to the S3 interface endpoint. ④ The portal backend calls /&lt;agent&gt;/invocations with the JWT over "
          "DX → TGW. ⑤ PrivateLink to the ingress gateway, which validates the JWT; ⑥ the interceptor puts the alias in a "
          "header. ⑦ The gateway invokes the agent's runtime with its IAM role and gets a job_id. ⑧ The agent calls Claude, "
          "⑨ the tools gateway, ⑩ its DynamoDB session and ⑪ S3, all through VPC endpoints. ⑫ Publishing calls the git-pr "
          "Lambda, which ⑬ opens a merge request in the internal Git over TGW → DX.", 20, 1385, 2380, 70,
          fill="#FFFFFF", stroke="#9CA3AF")
    p.box("controls", "<b>Controls</b> · JWT only at the edge, SigV4 inside · runtime resource policies: ingress role only · "
          "tool ACL per agent kind (TOOL_ACL) · git-pr profiles: repo + path prefix + file types per agent · jobs, sessions "
          "and uploads readable only by their alias · no IGW / NAT · endpoint policies · uploads bucket accepts PUT only "
          "via its endpoint · sessions (DynamoDB TTL) and uploads (S3 lifecycle) deleted after 90 days",
          20, 1465, 2380, 50, fill="#F0FDF4", stroke=GREEN)
    return p


# ================================================================================================ page 2
def card(p, cid, title, body, x, y, w=520, h=120, color=ML):
    p.box(cid, f"<b style='font-size:13px'>{title}</b><br>{body}", x, y, w, h, fill="#FFFFFF", stroke=color,
          extra="spacingLeft=70;")
    p.icon(cid + "_i", "", "bedrock_agentcore", color, x + 10, y + 12)


def page2() -> Page:
    p = Page("agents", "2. Agents and data flow", 2100, 1200)
    p.box("title", "<b style='font-size:18px'>Portal agents: what each agent does, what it stores, how results reach "
          "Git</b><br>All four share portal/runtime.py: caller alias · async jobs (submit + poll) · uploads · DynamoDB "
          "sessions · publish as a pull request.", 20, 10, 2060, 56, fill="none", stroke="none", size=12)
    # portal
    p.group("portal", "On-prem portal (corporate SSO)", 20, 90, 330, 780, "#5A6B86,#F8FAFC,#5A6B86")
    p.icon("user", "yhtsao", "users", "#232F3D", 50, 130, "portal", res=False, extra=RIGHT)
    tabs = [("tab_csp", "CSP 架構圖", "prompt → draw.io"), ("tab_coe", "COE 文件", "prepare · review"),
            ("tab_aicoe", "AICOE 文件", "prepare · review"), ("tab_tf", "Terraform", "需求 / 架構圖 → module")]
    for i, (cid, t, s) in enumerate(tabs):
        p.box(cid, f"<b>{t}</b><br>{s}", 50, 220 + i * 110, 270, 80, fill="#EEF4FF", stroke=BLUE, parent="portal",
              align="center")
    p.box("p_up", "<b>上傳</b> 圖片 / 文件<br>upload_url → presigned PUT<br>png · jpeg · pdf · docx · xlsx · md",
          50, 680, 270, 80, fill="#FFFFFF", stroke=BLUE, parent="portal", align="center")
    p.box("p_sess", "session_id = 同一份文件的多輪對話", 50, 780, 270, 60, fill="#FFFFFF", stroke="#C925D1",
          parent="portal", align="center")
    # gateway
    p.icon("ingw", "AgentCore Gateway<br><b>csp-arch-ingress</b><br>JWT → alias header<br>/csp-architect · /coe-doc<br>"
           "/aicoe-doc · /terraform", "bedrock_agentcore", ML, 420, 450)
    # agents
    X = 600
    p.group("agents", "4 AgentCore Runtimes · one per agent · own IAM role · VPC mode", 580, 80, 570, 790,
            "#01A88D,#FFFFFF,#016B5A")
    card(p, "a_csp", "CSP architect · /csp-architect",
         "prompt (+ web search) → draw.io XML<br>validator + icon auto-fix · post-processing hook<br>"
         "→ xml, explanation · job_id reusable by Terraform", X, 120)
    card(p, "a_coe", "COE documents · /coe-doc",
         "prepare: template sections, 【待確認】 for unknowns<br>review: every checklist item pass / partial / fail / n/a<br>"
         "templates/coe/*.yaml · → docs/coe/… .md", X, 310)
    card(p, "a_aicoe", "AICOE documents · /aicoe-doc",
         "use-case assessment · model change<br>NIST AI RMF · ISO/IEC 42001 · 金管會 AI 指引 · 個資法<br>"
         "templates/aicoe/*.yaml · → docs/aicoe/… .md", X, 500)
    card(p, "a_tf", "Terraform · /terraform",
         "requirements · uploaded diagram · CSP job_id<br>terraform_checks TF001–TF014 + repair turns<br>"
         "standards.yaml · → terraform/&lt;name&gt;/…", X, 690)
    for cid in ("a_csp", "a_coe", "a_aicoe", "a_tf"):
        p.edge("g_" + cid, "ingw", cid, "", GREEN, exit=(1, 0.5), entry=(0, 0.5))
    p.edge("p_gw", "tab_coe", "ingw", "JWT · job_id · status", BLUE, exit=(1, 0.5), entry=(0, 0.5))
    p.edge("csp_tf", "a_csp", "a_tf", "architecture_job_id", PURPLE, dashed=True, exit=(1, 0.8), entry=(1, 0.2),
           pts=[(1135, 216), (1135, 726)])
    # shared runtime services
    p.group("shared", "Shared runtime (portal/runtime.py) · through VPC endpoints", 1200, 90, 400, 780,
            "#01A88D,#F2FBF9,#016B5A")
    p.icon("bedrock", "Amazon Bedrock · Claude<br>(bedrock-runtime endpoint)", "bedrock", ML, 1240, 140, "shared")
    p.icon("ddb", "DynamoDB sessions<br>PK alias#session · SK MSG#…<br>TTL 90 d · lease lock", "dynamodb", DB,
           1240, 300, "shared")
    p.icon("s3up", "S3 uploads<br>uploads/alias/… (90 d)<br>sessions/ offload (90 d)", "s3",
           STOR, 1240, 470, "shared")
    p.icon("s3jobs", "S3 artifacts<br>jobs/ (7 d) · diagrams/", "s3", STOR, 1240, 640, "shared")
    p.box("sh_note", "• Only the alias that created a job, session or upload can use it<br>"
          "• One running job per session (lease)<br>• Images / documents → Converse content blocks<br>"
          "• max_tokens → history rolled back in DynamoDB too", 1360, 140, 220, 130, fill="#FFFFFF", stroke="#9CA3AF",
          parent="shared", size=10)
    p.edge("sh1", "agents", "shared", "every agent", ML, width=3, exit=(1, 0.32), entry=(0, 0.3))
    # tools
    p.icon("tools", "AgentCore Gateway<br><b>csp-arch-tools</b> (MCP, IAM)<br>interceptor: TOOL_ACL", "bedrock_agentcore",
           ML, 1680, 450)
    p.icon("web", "web-search-tool", "globe", "#232F3D", 1880, 300, res=False)
    p.icon("hook", "postprocess-hook", "lambda", COMP, 1880, 450)
    p.icon("gitpr", "git-pr Lambda<br>profile per agent kind", "lambda", COMP, 1880, 600)
    p.edge("t1", "tools", "web", "", GREEN, exit=(1, 0.2), entry=(0, 0.5))
    p.edge("t2", "tools", "hook", "", GREEN)
    p.edge("t3", "tools", "gitpr", "", GREEN, exit=(1, 0.8), entry=(0, 0.5))
    p.edge("a_tools", "a_tf", "tools", "publish → git-pr", GREEN, exit=(1, 0.5), entry=(0, 0.8), pts=[(1650, 750), (1650, 495)])
    p.box("acl", "<b>Tool ACL</b> (x-agent-kind)<br>csp-architect: web search, hook<br>coe / aicoe: git-pr<br>"
          "terraform: web search, git-pr", 1640, 90, 200, 80, fill="#FFFFFF", stroke=GREEN, size=10)
    # git
    p.group("gitg", "Internal Git (on-prem, via TGW → DX)", 1640, 900, 440, 270, "#C2410C,#FFF7ED,#C2410C")
    p.box("r_tf", "<b>platform/infra-live</b><br>terraform/&lt;name&gt;/** · .tf .md", 1660, 940, 200, 70,
          fill="#FFFFFF", stroke=ORANGE, parent="gitg", size=10)
    p.box("r_coe", "<b>ccoe/design-reviews</b><br>docs/coe/** · .md", 1660, 1020, 200, 60, fill="#FFFFFF",
          stroke=ORANGE, parent="gitg", size=10)
    p.box("r_ai", "<b>aicoe/use-cases</b><br>docs/aicoe/** · .md", 1660, 1090, 200, 60, fill="#FFFFFF", stroke=ORANGE,
          parent="gitg", size=10)
    p.box("r_ci", "<b>Merge request</b><br>branch agent/&lt;kind&gt;/&lt;alias&gt;/&lt;job&gt;<br>Requested-by: alias<br>"
          "CI: terraform fmt / validate / plan<br>human review + approval", 1870, 940, 195, 210, fill="#FFFFFF",
          stroke=ORANGE, parent="gitg", size=10)
    p.edge("gp", "gitpr", "r_tf", "open MR", ORANGE, exit=(1, 0.5), entry=(0.5, 0), pts=[(2070, 628), (2070, 880), (1760, 880)])
    # bottom: sequence
    p.box("seq", "<b>One request, end to end</b><br>"
          "1. <b>upload_url</b> {filename, content_type, size} → presigned PUT (5 min, exact type and size) → PUT over DX to "
          "the S3 interface endpoint<br>"
          "2. <b>generate</b> {prompt, mode, doc_type, attachments, session_id} → {job_id} in ~1 s (session locked)<br>"
          "3. <b>status</b> every 15 s → RUNNING … SUCCEEDED with document / review / files + checks (or FAILED / LOST)<br>"
          "4. the next turn in the same session_id refines the same draft (history restored from DynamoDB on any microVM)<br>"
          "5. <b>publish</b> {job_id} → only if checks passed → git-pr → merge request (idempotent: one job, one MR)",
          20, 900, 1580, 110, fill="#FFFFFF", stroke="#9CA3AF")
    p.box("ret", "<b>Retention</b> · DynamoDB sessions: TTL 90 days from creation · S3 uploads/ and sessions/: lifecycle 90 "
          "days · jobs/: 7 days · diagrams/: kept (artifact bucket) · Git: permanent, reviewed", 20, 1020, 1580, 40,
          fill="#F5F3FF", stroke="#C925D1")
    return p


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    p1, p2 = page1(), page2()
    (out / "portal-agents-architecture.drawio").write_text(mxfile(p1, p2), encoding="utf-8")
    (out / "page1-network.drawio").write_text(mxfile(p1), encoding="utf-8")
    (out / "page2-agents.drawio").write_text(mxfile(p2), encoding="utf-8")
    print("written to", out)
