# Google Cloud diagram style and layout

## Containers (rounded rectangles, `container=1`)
Common base: `rounded=1;arcSize=2;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;fontSize=12;container=1;collapsible=0;`

| Boundary | fillColor | strokeColor | Extra |
|---|---|---|---|
| Organization / folder | none | #5F6368 | `dashed=1;` |
| Project (host or service) | #F6F6F6 | #9E9E9E | |
| VPC network (Shared VPC) | #E8F0FE | #4285F4 | |
| Region | #FFFFFF | #4285F4 | `dashed=1;` |
| Zone / GKE cell | #F1F8E9 | #34A853 | |
| VPC Service Controls perimeter | none | #EA4335 | `dashed=1;strokeWidth=2;` |
| On-premises / stores / partner | #F5F5F5 | #666666 | |
| Legend | #FFFFFF | #BDC1C6 | `fontStyle=0;` |

## Icons
`sketch=0;html=1;fillColor=#5184F3;strokeColor=none;verticalAlign=top;labelPosition=center;verticalLabelPosition=bottom;align=center;spacingTop=-6;fontSize=11;fontStyle=1;fontColor=#5F6368;shape=mxgraph.gcp2.hexIcon;prIcon=<name>;` at 66x58.
Products without an icon: `rounded=1;whiteSpace=wrap;html=1;fillColor=#E6F4EA;strokeColor=#34A853;fontSize=11;` at about 120x50, label with the product name.

## Edges (always `edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;`)
| Traffic | Style |
|---|---|
| Dedicated / Partner Interconnect | `strokeColor=#9334E6;strokeWidth=2;` |
| HA VPN | `strokeColor=#E37400;dashed=1;` |
| Private Service Connect / Private Google Access | `strokeColor=#34A853;dashed=1;` |
| Egress (NAT / Secure Web Proxy) | `strokeColor=#EA4335;dashed=1;` |
| Public ingress | `strokeColor=#FBBC04;strokeWidth=2;` |
| Data / model flow | `strokeColor=#4285F4;` |
Add a legend box listing the styles you used.

## Spacing
- At least 40 px between icons, 30 px padding inside containers, 60 px between sibling containers.
- On-premises and stores on the left, internet users on the top, the VPC-SC perimeter around the data/ML projects at the bottom or right.
- Route long edges along container gaps with waypoints instead of crossing regions diagonally.
