import base64
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

from drawio_validator import auto_fix_icons, repair_prompt, validate

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def doc(cells: str, pages: int = 1) -> str:
    body = "".join(
        f'<diagram name="P{i}" id="d{i}"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>{cells}'
        f"</root></mxGraphModel></diagram>" for i in range(pages))
    return f"<mxfile>{body}</mxfile>"


def icon(cid: str, style: str) -> str:
    return f'<mxCell id="{cid}" value="x" style="{style}" vertex="1" parent="1"><mxGeometry width="50" height="50" as="geometry"/></mxCell>'


AWS = "shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{};"
GCP = "shape=mxgraph.gcp2.hexIcon;prIcon={};"
AZ = "image;html=1;image=img/lib/azure2/{};"


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.drawio")), ids=lambda p: p.name)
def test_committed_examples_are_valid(path):
    report = validate(path.read_text(encoding="utf-8"))
    assert report["ok"], report


def test_valid_icons_pass():
    xml = doc(icon("a", AWS.format("ecr")) + icon("g", GCP.format("container_engine"))
              + icon("z", AZ.format("networking/Front_Doors.svg"))
              + icon("grp", "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_vpc2;")
              + icon("u", "shape=mxgraph.aws4.users;"))
    report = validate(xml)
    assert report["ok"], report
    assert report["vertices"] == 5


def test_unknown_icons_are_errors_with_suggestions():
    xml = doc(icon("a", AWS.format("elastic_container_registry")) + icon("g", GCP.format("kubernetes_engine"))
              + icon("z", AZ.format("identity/Azure_AD_Privileged_Identity_Management.svg"))
              + icon("h", AWS.format("network_firewal")))
    report = validate(xml)
    assert not report["ok"]
    got = {e["name"]: e["suggestions"] for e in report["icon_errors"]}
    assert got["elastic_container_registry"] == ["ecr"]
    assert got["kubernetes_engine"] == ["container_engine"]
    assert got["identity/Azure_AD_Privileged_Identity_Management.svg"] == ["identity/Entra_Privileged_Identity_Management.svg"]
    assert "network_firewall" in got["network_firewal"]


def test_prIcon_only_checked_on_gcp_hex_icons():
    assert validate(doc(icon("x", "shape=foo;prIcon=not_a_gcp_icon;")))["ok"]


def test_auto_fix_aliases_and_close_matches():
    xml = doc(icon("a", AWS.format("elastic_container_registry")) + icon("b", AWS.format("network_firewal"))
              + icon("g", GCP.format("cloud_build")) + icon("g2", GCP.format("cloud_build")))
    fixed, fixes = auto_fix_icons(xml)
    assert validate(fixed)["ok"]
    assert {(f["from"], f["to"], f["count"]) for f in fixes} == {
        ("elastic_container_registry", "ecr", 1), ("network_firewal", "network_firewall", 1),
        ("cloud_build", "container_builder", 2)}


def test_auto_fix_leaves_unknown_names_for_the_model():
    xml = doc(icon("a", AWS.format("totally_made_up_service")))
    fixed, fixes = auto_fix_icons(xml)
    assert fixes == [] and fixed == xml
    prompt = repair_prompt(validate(xml))
    assert "totally_made_up_service" in prompt and "rounded rectangle" in prompt


def test_auto_fix_does_not_touch_prefix_matches():
    xml = doc(icon("a", AWS.format("eks")) + icon("b", AWS.format("elastic_container_registry")))
    fixed, _ = auto_fix_icons(xml)
    assert "resIcon=mxgraph.aws4.eks;" in fixed and "resIcon=mxgraph.aws4.ecr;" in fixed


def test_structural_errors():
    cells = ('<mxCell id="a" vertex="1" parent="1"/><mxCell id="a" vertex="1" parent="1"/>'
             '<mxCell id="b" vertex="1" parent="nope"/>'
             '<mxCell id="e" edge="1" parent="1" source="a" target="ghost"/>')
    report = validate(doc(cells))
    text = " | ".join(report["errors"])
    assert "duplicate cell ids: a" in text and "missing parent 'nope'" in text and "missing target 'ghost'" in text


def test_malformed_xml():
    report = validate(doc(icon("a", AWS.format("ecr")))[:-30])
    assert not report["ok"] and "not a valid draw.io document" in report["errors"][0]


def test_multi_page_and_object_cells():
    cells = '<object id="o" label="wrapped"><mxCell style="shape=mxgraph.gcp2.hexIcon;prIcon=bigquery;" vertex="1" parent="1"/></object>'
    report = validate(doc(cells, pages=3))
    assert report["ok"] and report["pages"] == 3 and report["vertices"] == 3


def test_compressed_page():
    model = '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>' + icon("a", AWS.format("eksx")) + "</root></mxGraphModel>"
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = base64.b64encode(c.compress(quote(model).encode()) + c.flush()).decode()
    report = validate(f'<mxfile><diagram name="z">{data}</diagram></mxfile>')
    assert report["icon_errors"][0]["name"] == "eksx"
