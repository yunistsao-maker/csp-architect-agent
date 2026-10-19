# draw.io XML template (Azure)

Rules: uncompressed XML; cells `0` and `1` always present; every other cell has a unique, descriptive id
(`hub-je-fw`, `spoke-je-aks`); children use geometry relative to their parent container; escape `&`, `<`, `>`, `"`
in attribute values; never put `--` inside XML comments.

```xml
<mxfile host="Electron" type="device">
  <diagram name="Network" id="network">
    <mxGraphModel grid="0" gridSize="10" guides="1" connect="1" arrows="1" page="0" pageWidth="1600" pageHeight="1000">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="onprem" value="Taipei HQ data center" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=#F5F5F5;strokeColor=#666666;" vertex="1" parent="1">
          <mxGeometry x="40" y="420" width="220" height="160" as="geometry" />
        </mxCell>
        <mxCell id="er1" value="ER circuit 1&lt;br&gt;(peering: Taipei)" style="image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/networking/ExpressRoute_Circuits.svg;" vertex="1" parent="onprem">
          <mxGeometry x="85" y="60" width="50" height="50" as="geometry" />
        </mxCell>
        <mxCell id="region-je" value="Japan East" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=none;strokeColor=#0078D4;dashed=1;" vertex="1" parent="1">
          <mxGeometry x="320" y="40" width="900" height="560" as="geometry" />
        </mxCell>
        <mxCell id="hub-je" value="Virtual WAN secured hub" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=#DCEBFA;strokeColor=#0078D4;" vertex="1" parent="region-je">
          <mxGeometry x="30" y="40" width="360" height="200" as="geometry" />
        </mxCell>
        <mxCell id="hub-je-ergw" value="ExpressRoute gateway" style="image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/networking/Virtual_Network_Gateways.svg;" vertex="1" parent="hub-je">
          <mxGeometry x="40" y="80" width="50" height="50" as="geometry" />
        </mxCell>
        <mxCell id="hub-je-fw" value="Azure Firewall Premium" style="image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/networking/Firewalls.svg;" vertex="1" parent="hub-je">
          <mxGeometry x="220" y="80" width="50" height="50" as="geometry" />
        </mxCell>
        <mxCell id="spoke-je" value="Workload spoke VNet" style="rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;fillColor=#E6F2FA;strokeColor=#0078D4;" vertex="1" parent="region-je">
          <mxGeometry x="450" y="40" width="420" height="200" as="geometry" />
        </mxCell>
        <mxCell id="spoke-je-aks" value="AKS (private cluster)" style="image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/containers/Kubernetes_Services.svg;" vertex="1" parent="spoke-je">
          <mxGeometry x="60" y="80" width="50" height="50" as="geometry" />
        </mxCell>
        <mxCell id="spoke-je-pe" value="Private Endpoint" style="image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/networking/Private_Endpoint.svg;" vertex="1" parent="spoke-je">
          <mxGeometry x="280" y="80" width="50" height="50" as="geometry" />
        </mxCell>
        <mxCell id="kv" value="Key Vault&lt;br&gt;(public access disabled)" style="image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/security/Key_Vaults.svg;" vertex="1" parent="region-je">
          <mxGeometry x="730" y="380" width="50" height="50" as="geometry" />
        </mxCell>
        <mxCell id="e-er" value="ExpressRoute (preferred)" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#7030A0;strokeWidth=2;" edge="1" parent="1" source="er1" target="hub-je-ergw">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
        <mxCell id="e-egress" value="0.0.0.0/0 via UDR" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#C00000;dashed=1;" edge="1" parent="region-je" source="spoke-je-aks" target="hub-je-fw">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
        <mxCell id="e-pe" value="privatelink.vaultcore.azure.net" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#0078D4;dashed=1;" edge="1" parent="region-je" source="spoke-je-pe" target="kv">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
```

For several pages, repeat `<diagram name="..." id="...">` blocks inside the same `<mxfile>`; ids must be unique within a page.
