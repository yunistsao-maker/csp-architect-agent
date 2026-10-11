---
name: gcp-architecture-diagram
description: "Generate validated Google Cloud architecture diagrams as draw.io XML using draw.io's GCP2 icon library (mxgraph.gcp2.hexIcon). Use for Google Cloud organizations, Shared VPC, hybrid connectivity (Interconnect, HA VPN, NCC), Private Service Connect, VPC Service Controls, GKE and Vertex AI designs."
---

You produce Google Cloud architecture diagrams as draw.io XML that a network or platform architect can review: every resource sits inside the boundary it really lives in, and every traffic path that matters is drawn and labelled.

## Workflow

1. **List the boundaries before drawing.** Organization → folders → projects (host and service projects for Shared VPC), VPC networks, regions and zones, VPC Service Controls perimeters, on-premises sites, partners.
2. **Read `references/style-and-layout.md`** for container styles, colors, edge conventions and spacing.
3. **Pick icons from `references/gcp2-icons.md`.** Use ONLY names listed there. These products have NO GCP2 icon and must be labelled rounded rectangles: Private Service Connect (endpoints, published services), VPC Service Controls (draw the perimeter as a container), Network Connectivity Center, Secure Web Proxy, Cloud NGFW Enterprise, Binary Authorization, Cloud Deploy, Organization Policy, Privileged Access Manager, Artifact Registry (or reuse `container_registry` with an explicit label).
4. **Read `references/xml-template.md`** and generate the XML from it.
5. **Self-check** with the list below, then return the diagram.

## Rules that matter most

- Common GCP2 names: `container_engine` (GKE), `container_builder` (Cloud Build), `container_registry`, `cloud_machine_learning` (Vertex AI), `cloud_pubsub`, `cloud_dataflow`, `bigquery`, `cloud_spanner`, `cloud_memorystore`, `cloud_storage`, `cloud_router`, `cloud_vpn`, `dedicated_interconnect`, `partner_interconnect`, `cloud_nat`, `cloud_dns`, `cloud_load_balancing`, `cloud_armor`, `cloud_cdn`, `cloud_iam`, `key_management_service`, `cloud_security_command_center`, `logging`, `cloud_monitoring`. Names like `kubernetes_engine`, `cloud_build`, `vertex_ai` or `pubsub` do NOT exist.
- Interconnect: draw each VLAN attachment path into a Cloud Router in the region it lands in; the 99.99% topology is 4 connections across 2 metros with Cloud Routers in 2 regions.
- HA VPN: two tunnels per peer; for many branches show the SD-WAN or NCC hub pattern rather than one icon per branch.
- PSC: draw the endpoint inside the consumer VPC and a dashed edge to the producer (Google APIs bundle, Vertex AI endpoint, or a published service attachment).
- VPC-SC: a dashed red container around the protected projects/services, with ingress rules or access levels labelled on the edges that cross it.
- Ingress: Global external Application LB + Cloud Armor + Cloud CDN → GKE (multi-cluster Gateway) or backends; internal-only APIs use an internal (or cross-region internal) Application LB.
- Egress: Cloud NAT and/or Secure Web Proxy; nodes without external IPs.
- Large designs (about 60+ icons): split into several `<diagram>` pages in one `<mxfile>`.

## Self-check before returning

- Every `prIcon=` name appears in `references/gcp2-icons.md`.
- Every edge has an existing `source` and `target`; every child's `parent` exists; ids are unique.
- No icon overlaps another; containers fit their children (child geometry is relative to the parent).
- All edges use `edgeStyle=orthogonalEdgeStyle`, and a small legend explains line styles.
