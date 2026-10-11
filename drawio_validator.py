"""Validate (and lightly auto-fix) draw.io XML produced by the agent.

Checks
  * XML is well-formed and has <mxfile>/<diagram>/<mxGraphModel> (multi-page and compressed pages supported)
  * cell ids are unique per page; every parent / edge source / edge target exists
  * icon names exist in draw.io's own libraries (drawio_icons.json):
      AWS   shape=mxgraph.aws4.*, resIcon=mxgraph.aws4.*, grIcon=mxgraph.aws4.*
      GCP   shape=mxgraph.gcp2.hexIcon;prIcon=*
      Azure image=img/lib/azure2/*.svg
An unknown icon renders as a blank square / empty hexagon / broken image in draw.io, so it counts as an error.

auto_fix_icons() rewrites icon names it is confident about (known aliases, or a single very close match);
anything else is reported with suggestions so the model can repair it in one extra turn.
"""

from __future__ import annotations

import base64
import difflib
import json
import re
import xml.etree.ElementTree as ET
import zlib
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

ICONS_FILE = Path(__file__).parent / "drawio_icons.json"

# Names models commonly produce from memory -> the real draw.io name.
ALIASES = {
    "aws4": {
        "elastic_container_registry": "ecr",
        "elastic_kubernetes_service": "eks",
        "elastic_container_service": "ecs",
        "kms": "key_management_service",
        "msk": "managed_streaming_for_kafka",
        "amazon_msk": "managed_streaming_for_kafka",
        "iam": "identity_and_access_management",
        "iam_identity_center": "single_sign_on",
        "identity_center": "single_sign_on",
        "route53": "route_53",
        "route_53_arc": "route_53_application_recovery_controller",
        "privatelink_endpoint": "vpc_privatelink",
        "vpc_endpoints": "endpoints",
        "vpc_endpoint": "endpoints",
        "alb": "application_load_balancer",
        "nlb": "network_load_balancer",
        "elb": "elastic_load_balancing",
        "s2s_vpn": "site_to_site_vpn",
        "vpn_connection": "site_to_site_vpn",
        "tgw": "transit_gateway",
        "cloudwatch_logs": "cloudwatch_2",
    },
    "gcp2": {
        "kubernetes_engine": "container_engine",
        "gke": "container_engine",
        "google_kubernetes_engine": "container_engine",
        "cloud_build": "container_builder",
        "artifact_registry": "container_registry",
        "vertex_ai": "cloud_machine_learning",
        "pubsub": "cloud_pubsub",
        "pub_sub": "cloud_pubsub",
        "dataflow": "cloud_dataflow",
        "memorystore": "cloud_memorystore",
        "spanner": "cloud_spanner",
        "interconnect": "dedicated_interconnect",
        "cloud_interconnect": "dedicated_interconnect",
        "ha_vpn": "cloud_vpn",
        "kms": "key_management_service",
        "cloud_kms": "key_management_service",
        "iam": "cloud_iam",
        "security_command_center": "cloud_security_command_center",
        "cloud_logging": "logging",
        "cloud_load_balancer": "cloud_load_balancing",
    },
    "azure2": {
        "identity/Azure_AD_Privileged_Identity_Management.svg": "identity/Entra_Privileged_Identity_Management.svg",
        "identity/Privileged_Identity_Management.svg": "identity/Entra_Privileged_Identity_Management.svg",
        "identity/Entra_ID.svg": "identity/Azure_Active_Directory.svg",
        "identity/Microsoft_Entra_ID.svg": "identity/Azure_Active_Directory.svg",
        "ai_machine_learning/Machine_Learning.svg": "ai_machine_learning/Azure_Machine_Learning.svg",
        "networking/Azure_Firewall.svg": "networking/Firewalls.svg",
        "networking/Front_Door.svg": "networking/Front_Doors.svg",
        "networking/ExpressRoute.svg": "networking/ExpressRoute_Circuits.svg",
        "networking/Application_Gateway.svg": "networking/Application_Gateways.svg",
        "networking/Load_Balancer.svg": "networking/Load_Balancers.svg",
        "networking/Private_Endpoints.svg": "networking/Private_Endpoint.svg",
        "security/Defender_for_Cloud.svg": "security/Security_Center.svg",
        "containers/Azure_Kubernetes_Service.svg": "containers/Kubernetes_Services.svg",
    },
}

AUTO_FIX_CUTOFF = 0.9  # only auto-apply a fuzzy match this close; otherwise report it


@lru_cache(maxsize=1)
def _icons() -> dict:
    data = json.loads(ICONS_FILE.read_text(encoding="utf-8"))
    return {"aws4": set(data["aws4"]), "gcp2": set(data["gcp2"]), "azure2": set(data["azure2"]),
            "shapes": set(data["shapes"])}


def _pages(root: ET.Element) -> list[tuple[str, ET.Element]]:
    """Return [(page name, <mxGraphModel>)] for an <mxfile> or bare <mxGraphModel> root."""
    if root.tag == "mxGraphModel":
        return [("Page-1", root)]
    if root.tag != "mxfile":
        raise ValueError(f"root element is <{root.tag}>, expected <mxfile>")
    pages = []
    for i, d in enumerate(root.findall("diagram")):
        name = d.get("name") or f"Page-{i + 1}"
        gm = d.find("mxGraphModel")
        if gm is None and (d.text or "").strip():  # compressed page
            raw = zlib.decompress(base64.b64decode(d.text.strip()), -15).decode("utf-8")
            gm = ET.fromstring(unquote(raw))
        if gm is None:
            raise ValueError(f"page '{name}' has no <mxGraphModel>")
        pages.append((name, gm))
    if not pages:
        raise ValueError("<mxfile> contains no <diagram> pages")
    return pages


def _cells(gm: ET.Element):
    """Yield (id, mxCell element) including cells wrapped in <object>/<UserObject>."""
    root = gm.find("root")
    for el in (root if root is not None else []):
        if el.tag == "mxCell":
            yield el.get("id"), el
        elif el.tag in ("object", "UserObject"):
            inner = el.find("mxCell")
            if inner is not None:
                yield el.get("id"), inner


_ICON_PATTERNS = [
    ("aws4", re.compile(r"(?:resIcon|grIcon)=mxgraph\.aws4\.([A-Za-z0-9_]+)")),
    ("aws4", re.compile(r"(?:^|;)shape=mxgraph\.aws4\.([A-Za-z0-9_]+)")),
    ("gcp2", re.compile(r"prIcon=([A-Za-z0-9_]+)")),
    ("azure2", re.compile(r"image=img/lib/azure2/([^;\"]+)")),
]


def _icon_refs(style: str):
    for family, pat in _ICON_PATTERNS:
        for m in pat.finditer(style):
            name = m.group(1)
            if family == "gcp2" and "mxgraph.gcp2.hexIcon" not in style:
                continue
            yield family, name


def _is_valid(family: str, name: str) -> bool:
    icons = _icons()
    if family == "aws4":
        return name in icons["aws4"] or f"mxgraph.aws4.{name}" in icons["shapes"]
    return name in icons[family]


def _suggest(family: str, name: str, n: int = 3, cutoff: float = 0.55) -> list[str]:
    alias = ALIASES[family].get(name)
    if alias:
        return [alias]
    return difflib.get_close_matches(name, _icons()[family], n=n, cutoff=cutoff)


def validate(xml: str) -> dict:
    """Return {"ok", "errors", "icon_errors", "pages", "vertices", "edges"} for a draw.io document."""
    report = {"ok": False, "errors": [], "icon_errors": [], "pages": 0, "vertices": 0, "edges": 0}
    try:
        pages = _pages(ET.fromstring(xml))
    except (ET.ParseError, ValueError, zlib.error) as e:
        report["errors"].append(f"XML is not a valid draw.io document: {e}")
        return report
    report["pages"] = len(pages)
    bad_icons: dict[tuple, int] = {}
    for page, gm in pages:
        cells = list(_cells(gm))
        ids = [cid for cid, _ in cells]
        idset = set(ids)
        dups = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
        if dups:
            report["errors"].append(f"[{page}] duplicate cell ids: {', '.join(dups[:10])}")
        for cid, c in cells:
            if cid is None:
                report["errors"].append(f"[{page}] a cell has no id")
                continue
            parent = c.get("parent")
            if parent and parent not in idset:
                report["errors"].append(f"[{page}] cell '{cid}' has missing parent '{parent}'")
            if c.get("edge") == "1":
                report["edges"] += 1
                for k in ("source", "target"):
                    ref = c.get(k)
                    if ref and ref not in idset:
                        report["errors"].append(f"[{page}] edge '{cid}' has missing {k} '{ref}'")
            elif c.get("vertex") == "1":
                report["vertices"] += 1
            for family, name in _icon_refs(c.get("style") or ""):
                if not _is_valid(family, name):
                    bad_icons[(family, name)] = bad_icons.get((family, name), 0) + 1
    for (family, name), count in sorted(bad_icons.items()):
        report["icon_errors"].append({"family": family, "name": name, "count": count,
                                      "suggestions": _suggest(family, name)})
    report["ok"] = not report["errors"] and not report["icon_errors"]
    return report


def auto_fix_icons(xml: str) -> tuple[str, list[dict]]:
    """Rewrite icon names that have a known alias or one very close match. Returns (xml, fixes)."""
    report = validate(xml)
    fixes = []
    for err in report["icon_errors"]:
        family, name = err["family"], err["name"]
        target = ALIASES[family].get(name)
        if not target:
            close = difflib.get_close_matches(name, _icons()[family], n=2, cutoff=AUTO_FIX_CUTOFF)
            if len(close) == 1:
                target = close[0]
        if not target or not _is_valid(family, target):
            continue
        if family == "aws4":
            pat = re.compile(r"((?:resIcon|grIcon|shape)=mxgraph\.aws4\.)" + re.escape(name) + r"(?=[;\"&]|$)")
        elif family == "gcp2":
            pat = re.compile(r"(prIcon=)" + re.escape(name) + r"(?=[;\"&]|$)")
        else:
            pat = re.compile(r"(image=img/lib/azure2/)" + re.escape(name))
        xml, n = pat.subn(lambda m: m.group(1) + target, xml)
        if n:
            fixes.append({"family": family, "from": name, "to": target, "count": n})
    return xml, fixes


def repair_prompt(report: dict) -> str:
    """Instruction for one repair turn, listing exactly what is wrong."""
    lines = ["Your draw.io XML failed validation. Fix ONLY these problems, keep everything else identical, "
             "and reply in the same format (the complete corrected XML in one code block, then the explanation)."]
    for e in report["errors"][:30]:
        lines.append(f"- {e}")
    for ie in report["icon_errors"]:
        hint = (f" Closest real names: {', '.join(ie['suggestions'])} (use one only if it is the same service)."
                if ie["suggestions"] else "")
        lines.append(f"- Unknown {ie['family']} icon '{ie['name']}' ({ie['count']}x).{hint} "
                     "If no real icon fits, draw a labelled rounded rectangle instead.")
    return "\n".join(lines)


if __name__ == "__main__":  # python drawio_validator.py diagram.drawio [--fix]
    import sys

    src = Path(sys.argv[1]).read_text(encoding="utf-8")
    if "--fix" in sys.argv:
        src, applied = auto_fix_icons(src)
        Path(sys.argv[1]).write_text(src, encoding="utf-8")
        print("fixed:", json.dumps(applied, indent=1))
    print(json.dumps(validate(src), indent=1))
