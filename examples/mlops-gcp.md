## Architecture Overview
This is an event-driven, pipeline-centric MLOps platform built on Vertex AI and the surrounding GCP data and DevOps services. Data flows from sources into a lake and warehouse, is versioned and served as features, and feeds reproducible, containerized training pipelines. Trained models pass through a registry and an approval gate, are deployed by CI/CD, and are served in real-time or batch mode. Monitoring closes the loop by triggering retraining automatically.

Security (IAM, CMEK, private networking, VPC Service Controls) and observability (logging, monitoring) apply across all stages.

> Note: the icons use draw.io's `mxgraph.gcp2.hexIcon` with `prIcon` names. Vertex AI services share the generic ML icon because gcp2 has no per-service Vertex shapes, so the labels carry the service names. If an icon appears blank in your draw.io version, replace it from the **Google Cloud** shape library, and keep the labels.

## Component Details
- **Pub/Sub and Dataflow**: Streaming and batch ingestion and ETL into the lake and warehouse.
- **Cloud Storage and BigQuery**: Raw and curated data lake plus analytical warehouse, used as offline feature and training sources.
- **Dataset Versioning**: GCS object versioning, BigQuery snapshots and time travel, and Dataplex lineage. Immutable dataset version IDs are recorded with every run.
- **Vertex AI Feature Store**: Offline features for training and low-latency online features for serving, which avoids training/serving skew.
- **Vertex AI Workbench, Experiments and ML Metadata**: Interactive development, run, metric and parameter tracking (TensorBoard), and artifact lineage.
- **Vertex AI Pipelines**: Orchestrates data validation, preprocessing, hyperparameter tuning (Vizier), distributed training (custom jobs on GPU/TPU), and evaluation.
- **Git Repo, Cloud Build and Artifact Registry**: CI runs tests, builds training and serving images, and compiles pipeline specs. CD deploys approved models.
- **Vertex AI Model Registry and Approval Gate**: Versioned models with metrics and lineage. Promotion requires passing automated thresholds plus a manual approval.
- **Online Endpoint and Batch Prediction**: Autoscaled private real-time serving and scheduled large-scale scoring written to BigQuery or GCS.
- **Model Monitoring, Cloud Monitoring, Pub/Sub and Cloud Functions**: Detect drift, skew and performance decay (using ground-truth labels), alert, and submit a new pipeline run.
- **Security and Observability layer**: IAM, KMS, VPC/PSC, VPC Service Controls, Cloud Logging and Audit Logs, and Cloud Monitoring.

## Data Flow
1. Sources publish to Pub/Sub or batch-load. Dataflow cleans the data and writes to Cloud Storage and BigQuery, creating versioned snapshots.
2. BigQuery populates the Feature Store (offline and online).
3. A Git commit triggers Cloud Build. It tests the code, builds images into Artifact Registry, and compiles and submits the Vertex AI Pipeline.
4. The pipeline pulls a dataset version and features, validates the data, preprocesses it, tunes hyperparameters, and runs distributed training. It logs to Experiments and ML Metadata.
5. The pipeline evaluates the model and registers it in the Model Registry.
6. The approval gate checks metrics and requires a manual sign-off. Cloud Build CD then deploys the model.
7. Deployment goes to the online endpoint (canary or traffic split) and to batch prediction jobs.
8. Clients call the endpoint, which fetches online features. Request and response logs flow to Model Monitoring.
9. Model Monitoring compares serving data with the training baseline and with ground truth. A threshold breach raises a Cloud Monitoring alert, publishes to Pub/Sub, and a Cloud Function re-submits the pipeline.

## Architecture Patterns / Database Design Patterns
- **Event-driven automation**: Pub/Sub and Eventarc connect alerts to retraining.
- **Pipeline-as-code (CI/CD/CT)**: Continuous integration, delivery and training from Git-defined pipelines.
- **Feature Store pattern**: One feature definition for training and serving.
- **Gated promotion**: A model registry with a policy and manual approval gate.
- **Defense in depth**: Private connectivity, a service perimeter, CMEK, and least-privilege identities.

## Benefits
- Reproducibility through data version, code version, container image and metadata lineage.
- Automated, closed-loop retraining on drift or performance decay.
- Consistent features between training and serving.
- Governed releases with an auditable approval trail.
- Managed, autoscaling infrastructure with lower operational burden.

## Design Considerations
- Separate dev, staging and prod projects. Promote the same model artifact across them.
- Choose drift thresholds and retraining cadence carefully to avoid retraining storms and cost spikes.
- Ground-truth labels often arrive late, so use proxy metrics in the meantime.
- Use Reduction Server, Spot VMs and TPU/GPU quotas for cost-effective distributed training.
- Private endpoints require Private Service Connect or VPC peering plus DNS planning.

## Best Practices
- Use a dedicated service account per component (pipeline, training, serving, CI/CD) and Workload Identity.
- Enable CMEK on all data and model storage, and route Audit Logs to a log sink or BigQuery.
- Keep an offline evaluation and a shadow or canary deployment step before 100% traffic.
- Pin container image digests and register the dataset version with each model.
- Alert on pipeline failures, endpoint latency and error rates, and cost budgets, not just model drift.

## References
- https://cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning
- https://cloud.google.com/vertex-ai/docs
- https://cloud.google.com/vertex-ai/docs/model-monitoring/overview
- https://cloud.google.com/vpc-service-controls/docs/overview