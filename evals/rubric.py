"""Requirement-coverage rubric for evals/usecases.json.

Each check is a case-insensitive regex matched against the diagram's labels (all pages). A requirement found only in
the explanation scores "text-only"; otherwise "missing".
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drawio_validator import _pages  # noqa: E402

RUBRIC = {
    "uc1-aws-fsi-bank": [
        ("HA", "Primary Taipei ap-east-2", r"ap-east-2|taipei"),
        ("HA", "DR Tokyo ap-northeast-1", r"ap-northeast-1|tokyo"),
        ("HA", "3 Availability Zones", r"(availability zone|\baz)\s*3|3 azs?|zone 3"),
        ("HA", "Aurora Global Database", r"aurora.*global|global (db|database)"),
        ("HA", "Route 53 ARC", r"\barc\b|application recovery"),
        ("Cell", "Cell router (CloudFront Functions)", r"cloudfront function"),
        ("Cell", "KeyValueStore cell map", r"keyvaluestore|key ?value ?store"),
        ("Cell", ">=2 cells in primary", r"cell 2|cell-2|cell b"),
        ("Cell", "Standby cells in DR", r"standby cell"),
        ("Net", "Direct Connect (2 locations)", r"direct connect location 2|dx location 2|location 2"),
        ("Net", "MACsec on DX", r"macsec"),
        ("Net", "DX Gateway", r"direct connect gateway|dx gateway|dxgw"),
        ("Net", "Transit VIF", r"transit vif"),
        ("Net", "Transit Gateway per region", r"transit gateway"),
        ("Net", "Site-to-Site VPN backup", r"site-to-site vpn|s2s vpn"),
        ("Net", "TGW route tables (prod/non-prod/shared)", r"route table"),
        ("Net", "Inspection VPC + Network Firewall", r"inspection vpc"),
        ("Net", "TGW appliance mode", r"appliance mode"),
        ("Net", "Central egress VPC + domain allow-list", r"egress vpc"),
        ("Net", "No IGW/NAT in workload VPCs", r"no igw|no internet gateway"),
        ("Net", "CloudFront + WAF + Shield Advanced", r"shield advanced"),
        ("Net", "CloudFront VPC origins -> internal ALB", r"vpc origin"),
        ("Net", "Internal ALB (no public ALB)", r"internal alb"),
        ("Net", "PrivateLink endpoint service (NLB) for partners", r"endpoint service|privatelink"),
        ("Net", "Route 53 Resolver in/outbound endpoints", r"resolver"),
        ("Net", "Interface endpoints (ECR/STS/KMS/...)", r"\bsts\b"),
        ("Net", "S3 gateway endpoint", r"s3.*gateway|gateway endpoint"),
        ("Compute", "Private EKS (private API endpoint)", r"private (api )?endpoint|private eks"),
        ("Compute", "Karpenter", r"karpenter"),
        ("Compute", "HPA", r"\bhpa\b"),
        ("Compute", "Security groups for pods", r"sg.for.pods|security groups? for pods|sgs? for pods"),
        ("Compute", "EKS Pod Identity", r"pod identity"),
        ("MLOps", "Kubeflow Pipelines", r"kubeflow"),
        ("MLOps", "MLflow registry", r"mlflow"),
        ("MLOps", "GPU Spot node pools", r"gpu.*spot|spot.*gpu"),
        ("MLOps", "Argo CD canary per cell", r"argo ?cd"),
        ("MLOps", "MSK -> ElastiCache online features", r"\bmsk\b"),
        ("IAM", "Organizations OUs", r"\bou\b|ous\b|organizations"),
        ("IAM", "IAM Identity Center", r"identity center"),
        ("IAM", "SCPs", r"\bscp"),
        ("IAM", "Permission boundaries", r"permission boundar"),
        ("IAM", "KMS CMKs", r"\bkms\b"),
        ("Audit", "Org CloudTrail -> Log Archive", r"log archive"),
        ("Audit", "GuardDuty / Security Hub / Config", r"guardduty"),
    ],
    "uc2-azure-manufacturing": [
        ("HA", "Japan East primary", r"japan east"),
        ("HA", "Southeast Asia secondary", r"southeast asia"),
        ("HA", "Active-active", r"active[- ]active"),
        ("Cell", "Deployment stamps (2/region)", r"stamp"),
        ("Cell", "Global routing layer factory->stamp", r"routing layer|stamp catalog|factory.*stamp"),
        ("Net", "Virtual WAN secured hubs", r"virtual wan|vwan"),
        ("Net", "Azure Firewall Premium (TLS/IDPS)", r"firewall premium"),
        ("Net", "Routing intent", r"routing intent"),
        ("Net", "2 ExpressRoute circuits", r"er circuit #?2|expressroute circuit 2|circuit #2"),
        ("Net", "ExpressRoute Global Reach", r"global reach"),
        ("Net", "S2S VPN (Vietnam, active-active BGP)", r"s2s vpn|site-to-site"),
        ("Net", "Private Endpoints for PaaS", r"\bpe\b|private endpoint"),
        ("Net", "Private DNS zones", r"private dns zone"),
        ("Net", "DNS Private Resolver", r"dns private resolver|private resolver"),
        ("Net", "Public network access disabled", r"publicnetworkaccess|public network access"),
        ("Net", "Internal App Gateway WAF v2 (private only)", r"app(lication)? gateway waf"),
        ("Net", "Front Door Premium + WAF", r"front door premium"),
        ("Net", "Private Link Service origin on internal LB", r"private link service"),
        ("Net", "UDR egress via Azure Firewall", r"userdefinedrouting|udr|route table"),
        ("Compute", "Private AKS", r"aks private|private (aks|cluster)"),
        ("Compute", "Azure CNI Overlay + Cilium", r"cilium"),
        ("Compute", "Workload Identity", r"workload identity"),
        ("Compute", "Cluster autoscaler + KEDA", r"keda"),
        ("Compute", "GPU node pool across 3 AZs", r"gpu.*(az|zone)"),
        ("MLOps", "AML managed VNet (approved outbound)", r"managed vnet"),
        ("MLOps", "Low-priority GPU compute clusters", r"low-priority|low priority"),
        ("MLOps", "MLflow registry", r"mlflow"),
        ("MLOps", "Azure ML Registry cross-region", r"ml registry"),
        ("MLOps", "Flux GitOps canary", r"flux"),
        ("Edge", "Arc-enabled AKS at edge", r"arc"),
        ("Edge", "ACR connected registry", r"connected registry"),
        ("Edge", "p99 < 50 ms", r"50 ?ms"),
        ("IAM", "Entra ID", r"entra"),
        ("IAM", "PIM", r"\bpim\b|privileged identity"),
        ("IAM", "Conditional Access", r"conditional access"),
        ("IAM", "Managed identities", r"managed identit"),
        ("IAM", "Management groups (CAF)", r"management group"),
        ("IAM", "Azure Policy deny public IP", r"azure policy"),
        ("Audit", "Defender for Cloud / Log Analytics / Monitor", r"defender"),
    ],
    "uc3-gcp-retail": [
        ("HA", "asia-east1 Taiwan", r"asia-east1"),
        ("HA", "asia-northeast1 Tokyo", r"asia-northeast1"),
        ("HA", "3 zones per region", r"zones? a ?/ ?b ?/ ?c|3 zones"),
        ("HA", "Spanner (global DB) or alternative", r"spanner"),
        ("HA", "Memorystore Redis", r"memorystore"),
        ("Cell", "2 GKE cells per region", r"tw-2|cell.*2"),
        ("Cell", "URL map / header cell routing", r"url map|header"),
        ("Cell", "Cell-assignment service", r"cell-assignment|cell assignment"),
        ("Net", "Shared VPC host projects", r"shared vpc"),
        ("Net", "Hierarchical firewall policies", r"hierarchical firewall"),
        ("Net", "Cloud NGFW Enterprise", r"ngfw"),
        ("Net", "Dedicated Interconnect 99.99% (2 metros)", r"dedicated i(c|nterconnect)"),
        ("Net", "Cloud Routers in both regions", r"cloud router"),
        ("Net", "HA VPN from stores", r"ha vpn"),
        ("Net", "NCC hub + hybrid spokes", r"\bncc\b|network connectivity center"),
        ("Net", "PSC endpoint for Google APIs (vpc-sc)", r"psc endpoint.*google apis|vpc-sc bundle"),
        ("Net", "PSC endpoint for Vertex AI prediction", r"psc.*vertex|vertex.*psc"),
        ("Net", "PSC published service (partner)", r"psc published|service attachment"),
        ("Net", "Cloud DNS private zones + server policies", r"cloud dns"),
        ("Net", "VPC Service Controls perimeter", r"vpc service controls|vpc-sc"),
        ("Net", "Cloud NAT", r"cloud nat"),
        ("Net", "Secure Web Proxy", r"secure web proxy"),
        ("Net", "Global external ALB", r"global external alb|global external application"),
        ("Net", "Cloud Armor (WAF + Adaptive)", r"cloud armor"),
        ("Net", "Cloud CDN", r"cloud cdn"),
        ("Net", "Multi-cluster Gateway", r"multi-cluster gateway"),
        ("Net", "Cross-region internal ALB (POS)", r"cross-region internal"),
        ("Compute", "GKE private clusters", r"gke enterprise|private cluster|regional private"),
        ("Compute", "Dataplane V2", r"dataplane v2"),
        ("Compute", "Workload Identity Federation", r"workload identity"),
        ("Compute", "Autoscaler + NAP + HPA", r"auto-provisioning|\bnap\b"),
        ("Compute", "L4 GPU node pool", r"\bl4\b"),
        ("MLOps", "Pub/Sub + Dataflow -> BigQuery", r"dataflow"),
        ("MLOps", "Vertex AI Pipelines", r"vertex ai pipelines"),
        ("MLOps", "Vertex AI Feature Store", r"feature store"),
        ("MLOps", "Model Registry", r"model registry"),
        ("MLOps", "Cloud Build + Cloud Deploy canary", r"cloud deploy"),
        ("MLOps", "Binary Authorization", r"binary authorization"),
        ("MLOps", "Model Monitoring -> retrain", r"model monitoring"),
        ("IAM", "Cloud Identity federation", r"cloud identity"),
        ("IAM", "Folders prod/non-prod/shared", r"folder"),
        ("IAM", "IAM Deny policies", r"iam deny"),
        ("IAM", "Org Policy (locations, ext IP, SA keys)", r"resourcelocations|org(anization)? polic"),
        ("IAM", "Privileged Access Manager", r"privileged access manager|\bpam\b"),
        ("Audit", "CMEK / Audit Logs / SCC", r"security command center"),
    ],
}



def diagram_labels(xml: str) -> str:
    labels = []
    try:
        for _, gm in _pages(ET.fromstring(xml)):
            root = gm.find("root")
            for el in (root if root is not None else []):
                labels.append(el.get("value") or el.get("label") or "")
    except Exception:  # noqa: BLE001 - an invalid diagram simply has no labels
        return ""
    text = re.sub(r"<[^>]+>", " ", " \n ".join(labels).lower())  # HTML tags inside labels (<br>, <b>)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)


def score(xml: str, explanation: str, usecase_id: str) -> dict:
    labels, expl = diagram_labels(xml), (explanation or "").lower()
    rows = []
    for category, name, pattern in RUBRIC[usecase_id]:
        status = "diagram" if re.search(pattern, labels) else "text-only" if re.search(pattern, expl) else "missing"
        rows.append({"category": category, "requirement": name, "status": status})
    drawn = sum(r["status"] == "diagram" for r in rows)
    text_only = sum(r["status"] == "text-only" for r in rows)
    return {"total": len(rows), "in_diagram": drawn, "text_only": text_only,
            "missing": len(rows) - drawn - text_only, "coverage_pct": round(100 * drawn / len(rows), 1), "rows": rows}
