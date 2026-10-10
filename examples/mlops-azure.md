## Architecture Overview
This is an end-to-end MLOps platform on Azure, organized as six horizontal tiers. Data flows from ingestion and feature engineering, through versioned training and tracking in Azure Machine Learning, and into a gated model registry. Azure DevOps handles CI/CD, and the model is served through real-time (AKS) and batch (AML Batch Endpoint) inference.

A closed loop sits on top of this. Azure Monitor and AML Model Monitoring detect data drift and performance decay, publish events to Event Grid, and an Azure Function triggers a retraining pipeline. Security (Entra ID, Key Vault, private networking) and centralized logging span every tier.

## Component Details
- **Data Factory / Event Hubs**: Batch and streaming ingestion from SQL, SaaS, files and IoT sources.
- **ADLS Gen2 + Delta Lake**: Lakehouse storage (bronze/silver/gold). Delta time travel gives data versioning.
- **Azure Databricks**: Scalable Spark transformations and feature engineering.
- **AML Managed Feature Store**: Offline store for point-in-time training sets. It is materialized to **Redis** as the online store for low-latency inference.
- **Versioned Data Assets**: Immutable, versioned dataset references in the AML workspace that tie each model to its exact data.
- **AML Pipeline + GPU Cluster**: Reproducible prep → train → evaluate pipeline. Distributed training runs on a GPU cluster (PyTorch DDP/Horovod).
- **Experiment Tracking (MLflow)**: Parameters, metrics, artifacts and run comparison.
- **Model Registry + Approval Gate**: Versioned models with lineage. Promotion requires metric thresholds plus a manual approval.
- **Azure DevOps**: CI/CD that builds the scoring image, runs tests and deploys. It also receives retrain triggers.
- **Container Registry**: Stores versioned scoring images.
- **Staging tests**: Smoke, load and shadow tests before production promotion.
- **AKS + API Management**: Real-time scoring with blue/green rollout and autoscaling, fronted by authentication and throttling.
- **AML Batch Endpoint**: Scheduled large-scale scoring that writes predictions to ADLS.
- **Azure Monitor / App Insights / AML Model Monitoring**: Operational telemetry plus data drift, prediction drift and performance monitoring.
- **Event Grid + Azure Function**: Event routing and the automated retraining trigger.
- **Log Analytics, Key Vault, Entra ID, VNet, Private Link**: Central logging, customer-managed keys and secrets, RBAC and managed identities, network isolation and private endpoints.

## Data Flow
1. Batch data (Data Factory) and streaming data (Event Hubs) land in ADLS Gen2 Delta tables.
2. Databricks cleans the data and builds features, which are published to the Feature Store. Features are materialized to the Redis online store.
3. A point-in-time training set is registered as a versioned data asset.
4. The AML pipeline runs distributed training on the GPU cluster and logs runs to MLflow.
5. The best model is registered, then passes the approval gate (automatic metric checks plus manual sign-off).
6. Azure DevOps builds the image, pushes it to ACR and deploys it to staging for tests.
7. After tests pass, the model is promoted to AKS (real-time, behind API Management) and the Batch Endpoint.
8. Telemetry and captured inference data go to Azure Monitor/App Insights and AML Model Monitoring. Diagnostics are sent to Log Analytics.
9. A drift or performance breach raises an Event Grid event. The Azure Function starts the DevOps pipeline, which submits a retraining job, and the loop repeats.

## Architecture Patterns / Database Design Patterns
- **Lakehouse (medallion)**: Bronze/silver/gold Delta layers give lineage and reproducibility.
- **Feature Store (offline/online)**: Consistent features for training and serving, avoiding training/serving skew.
- **Event-driven retraining**: Monitor → Event Grid → Function → pipeline.
- **Gated promotion / GitOps-style CD**: Registry approval plus staging validation before production.
- **Blue/green deployment**: Safe rollout and rollback on AKS.
- **Zero-trust networking**: Private endpoints with managed identities and no public access.

## Benefits
- Full reproducibility through data, code, model and environment versioning.
- Automated, governed path from experiment to production.
- Continuous quality assurance via drift detection and automated retraining.
- Secure, auditable and compliant by design.

## Design Considerations
- Set drift and performance thresholds, with cooldowns to prevent retrain storms.
- Choose AKS or AML managed online endpoints depending on how much cluster control you need.
- Plan GPU quotas and low-priority/spot nodes for cost control.
- Ground-truth labels often arrive late, so performance monitoring needs a label-join job.
- Use private DNS zones for all Private Link endpoints.

## Best Practices
- Use managed identities instead of keys, and store secrets and CMKs in Key Vault.
- Separate dev/test/prod workspaces and subscriptions, and define infrastructure as code (Bicep/Terraform).
- Pin environments and container images, and sign and scan images in ACR.
- Enforce Azure Policy and diagnostic settings so all logs reach Log Analytics.
- Always retrain through the same pipeline and approval gate used for new models.

## References
- Azure MLOps overview: https://learn.microsoft.com/azure/machine-learning/concept-model-management-and-deployment
- AML managed feature store: https://learn.microsoft.com/azure/machine-learning/concept-what-is-managed-feature-store
- AML model monitoring: https://learn.microsoft.com/azure/machine-learning/concept-model-monitoring
- AML secure workspace: https://learn.microsoft.com/azure/machine-learning/how-to-network-security-overview
- MLOps v2 solution accelerator: https://github.com/Azure/mlops-v2