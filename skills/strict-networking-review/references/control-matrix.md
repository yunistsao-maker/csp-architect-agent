# Network control matrix (AWS / Azure / Google Cloud)

Verify regional availability of each service for the regions in the request; flag anything uncertain.

| Control | AWS | Azure | Google Cloud |
|---|---|---|---|
| Dedicated hybrid link | Direct Connect (dedicated or hosted) via Direct Connect Gateway; transit VIF to Transit Gateway | ExpressRoute circuit → ExpressRoute gateway (hub VNet or Virtual WAN hub) | Dedicated or Partner Interconnect → VLAN attachments → Cloud Router |
| Highest-resilience topology | Maximum resiliency: 2 DX locations × 2 connections | 2 circuits at 2 different peering locations, zone-redundant gateways | 99.99% topology: 4 connections across 2 metros, Cloud Routers in 2 regions |
| Link encryption | MACsec on dedicated connections, or IPsec over DX | MACsec on ExpressRoute Direct, or IPsec over ExpressRoute | MACsec for Cloud Interconnect, or HA VPN over Interconnect |
| Backup over internet | Site-to-Site VPN on Transit Gateway (1.25 Gbps per standard tunnel; size critical flows) | S2S VPN gateway, active-active with BGP | HA VPN (two tunnels per gateway, 99.99% SLA) |
| Site-to-site at scale | Transit Gateway Connect / Cloud WAN | Virtual WAN with SD-WAN partners | Network Connectivity Center (router appliance or hybrid spokes) |
| Segmentation | Transit Gateway route tables, separate VPCs/accounts | Virtual WAN routing intent and route tables, hub-spoke UDRs | Shared VPC per environment, separate VPCs per trust zone, NCC hubs |
| Inspection | AWS Network Firewall in an inspection VPC (TGW appliance mode) or Gateway Load Balancer | Azure Firewall Premium (TLS inspection, IDPS) in secured hub | Cloud NGFW Enterprise (IPS) with hierarchical / global firewall policies |
| Controlled egress | Central egress VPC: NAT Gateway + Network Firewall domain allow-list | UDR 0.0.0.0/0 to Azure Firewall with FQDN rules | Cloud NAT + Secure Web Proxy URL allow-list |
| Private PaaS access | Interface VPC endpoints (PrivateLink) + gateway endpoints (S3, DynamoDB) with endpoint policies | Private Endpoints + `privatelink.*` Private DNS zones; public network access disabled | Private Service Connect endpoints (Google APIs bundles, published services), Private Google Access |
| Exfiltration guardrail | VPC endpoint policies + resource policies (aws:PrincipalOrgID, aws:SourceVpce) | Private Endpoint network policies, storage firewall, Azure Policy deny public access | VPC Service Controls perimeter with access levels / ingress rules |
| Hybrid DNS | Route 53 Resolver inbound + outbound endpoints, forwarding rules shared via RAM | Azure DNS Private Resolver (inbound/outbound) in a hub-connected VNet | Cloud DNS inbound server policy + outbound forwarding zones |
| Private-only ingress | CloudFront + WAF + Shield Advanced → VPC origins → internal ALB | Front Door Premium + WAF → Private Link origin (Private Link Service / private App Gateway) | Global external Application LB + Cloud Armor (+ CDN) → backends; internal ALB for private APIs |
| Partner private access | VPC endpoint service (NLB) → partner interface endpoint | Private Link Service → partner Private Endpoint | PSC published service (service attachment) → partner PSC endpoint |
| Kubernetes | EKS private endpoint, Pod Identity / IRSA, security groups for pods, network policies | AKS private cluster, Workload Identity, Azure CNI Overlay + Cilium network policy, UDR egress | GKE private control plane, Workload Identity Federation, Dataplane V2 network policy, no external node IPs |
| Preventive guardrails | SCPs (deny regions, IGW creation, CloudTrail changes), permission boundaries | Azure Policy (deny public IP / public network access), management groups | Organization Policy (resource locations, vmExternalIpAccess, SA key creation), IAM deny |
| Privileged access | IAM Identity Center permission sets, temporary elevation | Entra ID PIM, Conditional Access | Privileged Access Manager, Cloud Identity + IdP federation |
| Regional failover control | Route 53 ARC (routing controls, Region switch) | Front Door / Traffic Manager priority routing | Global LB health-based routing, Cloud DNS routing policies |
