---
name: azure-architecture-diagram
description: "Generate validated Microsoft Azure architecture diagrams as draw.io XML using draw.io's Azure icon library (img/lib/azure2). Use for Azure landing zones, hub-spoke or Virtual WAN networks, hybrid connectivity (ExpressRoute, S2S VPN), AKS, Azure Machine Learning and multi-region designs."
---

You produce Azure architecture diagrams as draw.io XML that a network or platform architect can review: every resource sits inside the boundary it really lives in, and every traffic path that matters is drawn and labelled.

## Workflow

1. **List the boundaries before drawing.** Management groups / subscriptions (only when the request is about governance), regions, Virtual WAN hubs or hub VNets, spoke VNets, subnets that matter (AzureFirewallSubnet, GatewaySubnet, AKS node/pod, private endpoint subnet), on-premises sites, external parties.
2. **Read `references/style-and-layout.md`** for container styles, colors, edge/line conventions and spacing.
3. **Pick icons.** Read `references/icon-index.md`, then only the category files you need (usually `networking`, `containers`, `compute`, `databases`, `storage`, `security`, `identity`, `ai_machine_learning`, `analytics`, `management_governance`). Use ONLY paths listed there. If a service has no icon, draw a labelled rounded rectangle.
4. **Read `references/xml-template.md`** and generate the XML from it.
5. **Self-check** with the list below, then return the diagram.

## Rules that matter most

- Hub-spoke / vWAN: draw the hub (Azure Firewall, VPN/ER gateways, DNS Private Resolver in a shared-services spoke) and show spoke-to-hub peering or hub connections. A Private DNS zone cannot be linked to a vWAN hub: link zones to a hub-connected shared-services VNet.
- Private Endpoints: draw the PE icon inside the spoke subnet, and a dashed edge from the PE to the PaaS resource, labelled with the `privatelink.*` zone. Mark PaaS resources "public network access disabled" when the request requires it.
- Hybrid: ExpressRoute circuits (one icon per circuit, labelled with the peering location) → ExpressRoute gateway in the hub; S2S VPN through the VPN gateway; label which path is preferred (BGP) and which is backup.
- Ingress: Front Door Premium + WAF → Private Link origin (Private Link Service on an internal load balancer, or a private-only App Gateway) when nothing may be public.
- Egress: UDR 0.0.0.0/0 → hub firewall (Azure Firewall Premium for TLS inspection/IDPS); AKS `outboundType=userDefinedRouting`.
- Multi-region: one container per region; replication edges (Cosmos DB multi-region, ACR geo-replication, Azure ML Registry) dashed and labelled.
- Large designs (about 60+ icons): split into several `<diagram>` pages in one `<mxfile>` (for example "Network", "Workloads", "Data & MLOps").

## Self-check before returning

- Every `image=` path appears in the icon catalog files you read.
- Every edge has an existing `source` and `target`; every child's `parent` exists; ids are unique.
- No icon overlaps another; containers are big enough for their children (child geometry is relative to the parent).
- All edges use `edgeStyle=orthogonalEdgeStyle`, and a small legend explains line styles.
