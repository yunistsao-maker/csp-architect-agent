# Azure diagram style and layout

## Containers (rounded rectangles, `container=1`)
Common base: `rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;`

| Boundary | fillColor | strokeColor | Extra |
|---|---|---|---|
| Region | none | #0078D4 | `dashed=1;` |
| Virtual WAN hub / hub VNet | #DCEBFA | #0078D4 | |
| Spoke VNet | #E6F2FA | #0078D4 | |
| Subnet | #FFFFFF | #7FBA00 | `dashed=1;fontSize=11;fontStyle=0;` |
| Deployment stamp / cell | #F3F9EC | #57A300 | |
| On-premises / factory / branch | #F5F5F5 | #666666 | |
| Management group / subscription | none | #605E5C | `dashed=1;` |
| Legend | #FFFFFF | #A19F9D | `fontStyle=0;` |

## Icons
`image;aspect=fixed;html=1;align=center;fontSize=12;image=img/lib/azure2/<path>;` at 50x50 (48-64 is fine).
Labels go under the icon (default). Use `&lt;br&gt;` for a second line with the role, e.g. `Azure Firewall Premium&lt;br&gt;TLS inspection + IDPS`.

## Edges (always `edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;`)
| Traffic | Style |
|---|---|
| ExpressRoute | `strokeColor=#7030A0;strokeWidth=2;` |
| ExpressRoute Global Reach | `strokeColor=#7030A0;dashed=1;` |
| S2S / P2S VPN | `strokeColor=#E36C09;dashed=1;` |
| Private Endpoint / Private Link | `strokeColor=#0078D4;dashed=1;` |
| Egress via firewall | `strokeColor=#C00000;dashed=1;` |
| Public ingress (the only internet path) | `strokeColor=#D4A017;strokeWidth=2;` |
| Data / model flow | `strokeColor=#2E7D32;` |
Add a legend box listing the styles you used.

## Spacing
- At least 40 px between icons, 30 px padding inside containers, 60 px between sibling containers.
- Put on-premises sites along the bottom or left edge, internet/users on the top or left, global services (Front Door, Entra ID, management) in a band at the top.
- Route long edges along container gaps with waypoints (`<Array as="points"><mxPoint x=".." y=".."/></Array>`) instead of crossing regions diagonally.
