## Architecture Overview
This diagram shows an end-to-end MLOps platform on AWS, laid out as five left-to-right stages inside a private VPC. Data is ingested, versioned and turned into features. SageMaker Pipelines then runs distributed training and evaluation, and the result goes to a Model Registry with a human approval gate. Approval triggers a CI/CD pipeline that deploys real-time and batch inference. Model Monitor watches the production data, and a drift or quality alarm automatically starts the training pipeline again (the pink dashed loop).

Security and observability (IAM, KMS, PrivateLink, Secrets Manager, CloudTrail, CloudWatch Logs, Config, Macie) apply across all stages and are shown in the bottom band.

## Component Details
- **Kinesis Data Firehose / S3 Data Lake / Glue**: Firehose ingests streaming data into the S3 lake, and Glue handles ETL and the Data Catalog.
- **S3 Versioned Datasets + CodeCommit (DVC)**: Immutable, versioned training datasets in S3. Git holds code and DVC pointer files, so every model can be traced to an exact dataset and code commit.
- **SageMaker Feature Store**: Online store for low-latency inference features and offline store for training. The same feature definitions are used in both, which avoids training/serving skew.
- **SageMaker Pipelines**: Orchestrates processing, training, evaluation and conditional registration as a DAG.
- **Distributed Training**: Multi-node GPU training jobs using data or model parallelism, with managed Spot capacity and custom images from ECR.
- **Experiments (managed MLflow)**: Tracks parameters, metrics and artifacts for every run.
- **Studio**: Notebooks and IDE for data scientists. Code is committed to CodeCommit.
- **Model Registry + Model Cards**: Versioned model packages with metrics, lineage and status (PendingManualApproval, Approved or Rejected).
- **Approver, EventBridge, CodePipeline, CodeBuild, CloudFormation**: An approved status change triggers CI/CD. The pipeline runs tests, deploys to staging, then to production through IaC.
- **API Gateway + Lambda + Real-time Endpoint**: Clients call a private, WAF-protected API. Lambda pre- and post-processes and invokes an auto-scaling, multi-AZ endpoint.
- **Batch Transform + Step Functions + Scheduler**: Scheduled or event-driven batch scoring, with input and output in S3.
- **Model Monitor**: Compares captured traffic and ground-truth labels against baselines for data quality, drift, model quality and bias.
- **CloudWatch, SNS, EventBridge rule, Lambda**: Violations raise alarms, send notifications and start the retraining pipeline.
- **Security and logging band**: IAM roles, KMS keys, VPC endpoints, Secrets Manager, CloudTrail, CloudWatch Logs, Config and Macie.

SageMaker capabilities share the standard SageMaker icon and are distinguished by their labels, because draw.io has no separate icon for each one.

## Data Flow
1. Source systems stream or batch data through Firehose into the S3 data lake.
2. Glue cleans and catalogs the data, writes versioned datasets to S3 and ingests features into Feature Store. DVC pointers are committed to Git.
3. A data scientist works in Studio and commits pipeline code to CodeCommit.
4. SageMaker Pipelines reads features and datasets, runs Processing, then distributed Training using the ECR image.
5. Training and evaluation log to Experiments. Artifacts and metrics are written to S3.
6. If the metrics pass the threshold, the model is registered in the Model Registry as PendingManualApproval.
7. An approver reviews the model and sets it to Approved. EventBridge detects the state change and starts CodePipeline.
8. CodeBuild runs tests, then CloudFormation deploys to staging and production, creating the real-time endpoint and the batch job definition.
9. Clients call API Gateway, then Lambda, then the endpoint. Requests and predictions are captured to S3.
10. Batch scoring runs on a schedule through Step Functions and Batch Transform.
11. Model Monitor analyses captured data and ground-truth labels, writes reports to S3 and publishes metrics to CloudWatch.
12. A CloudWatch alarm notifies through SNS and fires an EventBridge rule. Lambda then starts the SageMaker Pipeline, closing the loop.

## Benefits
- Full reproducibility: dataset version, code commit, features, hyperparameters and model are all linked.
- Governed releases: a mandatory approval gate and an automated, tested deployment path.
- Continuous quality: drift and performance degradation are detected and trigger retraining without manual work.
- Scalable: managed distributed training, auto-scaled endpoints and serverless glue services.

## Security Considerations
- Run all SageMaker jobs and endpoints in private subnets with network isolation. Reach AWS services only through VPC endpoints (S3 gateway, SageMaker API and Runtime, ECR, Logs, STS).
- Use separate least-privilege IAM roles for data engineering, training, pipeline execution, CI/CD and inference. Add permission boundaries and SCPs.
- Encrypt S3, EBS volumes, Feature Store, model artifacts and endpoint storage with customer-managed KMS keys. Enforce TLS in transit.
- Keep credentials in Secrets Manager. Use Macie to find PII before it enters training data.
- Enable CloudTrail, CloudWatch Logs and Config rules for audit and compliance. Protect API Gateway with WAF and IAM or Cognito authorization.

## Cost Optimization
- Use Managed Spot Training with checkpointing for training jobs.
- Use Savings Plans, multi-model endpoints and auto-scaling for real-time inference. Use Serverless Inference for spiky traffic.
- Prefer Batch Transform for non-latency-sensitive workloads.
- Use S3 lifecycle policies on raw data and captured traffic. Sample the data capture percentage.
- Use Pipeline step caching to skip unchanged steps.

## Best Practices
- Define all infrastructure and pipelines as code, and use separate AWS accounts for dev, staging and prod.
- Use champion/challenger or canary deployments with automatic rollback on alarms.
- Create Model Monitor baselines from the training data, and set alarm thresholds carefully to avoid retraining storms.
- Tag all resources and propagate lineage into Model Cards.
- Keep the retraining trigger rate-limited, and gate the resulting model through the same registry approval.

## References
- https://docs.aws.amazon.com/sagemaker/latest/dg/mlops.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/pipelines.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/feature-store.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/model-registry.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/model-monitor.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/security.html
- https://aws.amazon.com/architecture/machine-learning/