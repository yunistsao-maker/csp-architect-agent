---
name: strict-networking-review
description: "Checklist for architectures with strict network controls (regulated finance, OT/IT manufacturing, private-only PaaS): hybrid connectivity resilience, segmentation and inspection, private service access, hybrid DNS, ingress/egress, guardrails, multi-region and cells. Use together with an AWS, Azure or GCP diagram skill whenever the request mentions DX/ExpressRoute/Interconnect, VPN, PrivateLink/Private Endpoint/PSC, firewalls, no public IPs or data exfiltration."
---

Use this checklist while designing, and again before returning the diagram. Each item names what must be VISIBLE in the
diagram (an icon, a container or a labelled edge), not only mentioned in the explanation.

## How to use
1. Read `references/control-matrix.md` for the service that implements each control on AWS, Azure and GCP.
2. For every requirement in the request, make sure the matching row is drawn.
3. In the explanation, add a short "Network controls" table: control → how it is implemented → where it appears in the diagram.
   List any requirement you could not satisfy, and why (for example a service not available in the chosen region).

## Checklist
- **Hybrid resilience**: redundant circuits in separate locations/metros, the SLA topology named in a label, VPN as backup with the preference rule (BGP) labelled.
- **Segmentation**: separate routing domains for prod / non-prod / shared, drawn as route tables, routing intent or separate VPCs.
- **Inspection**: east-west and north-south flows pass a firewall; show where (inspection VPC, secured hub, NGFW).
- **Egress**: one controlled egress point with an FQDN/URL allow-list; workload networks have no direct internet path.
- **Private service access**: PaaS/AI services reached only through private endpoints; label the private DNS zones or endpoint policies.
- **Hybrid DNS**: inbound and outbound resolution between on-premises and cloud drawn explicitly.
- **Ingress**: the only public entry point (CDN/WAF/global LB) reaches backends privately; no public load balancers or IPs behind it.
- **Partner access**: partners connect over private connectivity (PrivateLink / Private Link Service / PSC published service), never peering into workload networks.
- **Kubernetes**: private control plane endpoint, network policy, workload identity, controlled egress for nodes.
- **Guardrails**: preventive policies (SCP / Azure Policy / Organization Policy) that deny public IPs, unapproved regions and log tampering.
- **Multi-region and cells**: each region/cell drawn as its own container; replication edges labelled with RPO; the failover control drawn.
