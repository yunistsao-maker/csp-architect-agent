# draw.io XML template (Google Cloud)

Rules: uncompressed XML; cells `0` and `1` always present; every other cell has a unique, descriptive id
(`vpc-prod-cr`, `cell-tw1-gke`); children use geometry relative to their parent container; escape `&`, `<`, `>`, `"`
in attribute values; never put `--` inside XML comments.

```xml
<mxfile host="Electron" type="device">
  <diagram name="Network" id="network">
    <mxGraphModel grid="0" gridSize="10" guides="1" connect="1" arrows="1" page="0" pageWidth="1600" pageHeight="1000">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="onprem" value="HQ data center" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=#F5F5F5;strokeColor=#666666;" vertex="1" parent="1">
          <mxGeometry x="40" y="160" width="180" height="160" as="geometry" />
        </mxCell>
        <mxCell id="onprem-rtr" value="Edge routers (BGP)" style="sketch=0;html=1;fillColor=#5184F3;strokeColor=none;verticalAlign=top;labelPosition=center;verticalLabelPosition=bottom;align=center;spacingTop=-6;fontSize=11;fontStyle=1;fontColor=#5F6368;shape=mxgraph.gcp2.hexIcon;prIcon=external_data_center;" vertex="1" parent="onprem">
          <mxGeometry x="57" y="60" width="66" height="58" as="geometry" />
        </mxCell>
        <mxCell id="ic" value="Dedicated Interconnect" style="sketch=0;html=1;fillColor=#5184F3;strokeColor=none;verticalAlign=top;labelPosition=center;verticalLabelPosition=bottom;align=center;spacingTop=-6;fontSize=11;fontStyle=1;fontColor=#5F6368;shape=mxgraph.gcp2.hexIcon;prIcon=dedicated_interconnect;" vertex="1" parent="1">
          <mxGeometry x="280" y="220" width="66" height="58" as="geometry" />
        </mxCell>
        <mxCell id="vpc" value="Shared VPC prod-vpc (host project)" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=#E8F0FE;strokeColor=#4285F4;" vertex="1" parent="1">
          <mxGeometry x="400" y="80" width="560" height="320" as="geometry" />
        </mxCell>
        <mxCell id="vpc-cr" value="Cloud Router (asia-east1)" style="sketch=0;html=1;fillColor=#5184F3;strokeColor=none;verticalAlign=top;labelPosition=center;verticalLabelPosition=bottom;align=center;spacingTop=-6;fontSize=11;fontStyle=1;fontColor=#5F6368;shape=mxgraph.gcp2.hexIcon;prIcon=cloud_router;" vertex="1" parent="vpc">
          <mxGeometry x="40" y="140" width="66" height="58" as="geometry" />
        </mxCell>
        <mxCell id="vpc-gke" value="GKE private cluster" style="sketch=0;html=1;fillColor=#5184F3;strokeColor=none;verticalAlign=top;labelPosition=center;verticalLabelPosition=bottom;align=center;spacingTop=-6;fontSize=11;fontStyle=1;fontColor=#5F6368;shape=mxgraph.gcp2.hexIcon;prIcon=container_engine;" vertex="1" parent="vpc">
          <mxGeometry x="240" y="140" width="66" height="58" as="geometry" />
        </mxCell>
        <mxCell id="vpc-psc" value="PSC endpoint&lt;br&gt;(Google APIs, vpc-sc)" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#E6F4EA;strokeColor=#34A853;fontSize=11;" vertex="1" parent="vpc">
          <mxGeometry x="400" y="144" width="130" height="50" as="geometry" />
        </mxCell>
        <mxCell id="perimeter" value="VPC Service Controls perimeter" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=none;strokeColor=#EA4335;dashed=1;strokeWidth=2;" vertex="1" parent="1">
          <mxGeometry x="1020" y="140" width="200" height="200" as="geometry" />
        </mxCell>
        <mxCell id="bq" value="BigQuery" style="sketch=0;html=1;fillColor=#5184F3;strokeColor=none;verticalAlign=top;labelPosition=center;verticalLabelPosition=bottom;align=center;spacingTop=-6;fontSize=11;fontStyle=1;fontColor=#5F6368;shape=mxgraph.gcp2.hexIcon;prIcon=bigquery;" vertex="1" parent="perimeter">
          <mxGeometry x="67" y="80" width="66" height="58" as="geometry" />
        </mxCell>
        <mxCell id="e-vlan" value="VLAN attachments" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#9334E6;strokeWidth=2;" edge="1" parent="1" source="onprem-rtr" target="ic">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
        <mxCell id="e-bgp" value="BGP" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#9334E6;strokeWidth=2;" edge="1" parent="1" source="ic" target="vpc-cr">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
        <mxCell id="e-gke-psc" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;" edge="1" parent="vpc" source="vpc-gke" target="vpc-psc">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
        <mxCell id="e-psc-bq" value="private API access" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#34A853;dashed=1;" edge="1" parent="1" source="vpc-psc" target="bq">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
```

For several pages, repeat `<diagram name="..." id="...">` blocks inside the same `<mxfile>`; ids must be unique within a page.
