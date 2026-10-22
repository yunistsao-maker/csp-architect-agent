"""Shared runtime pieces for the portal agents (COE / AICOE documents, Terraform, and later the CSP architect).

- runtime.PortalAgentApp: the AgentCore entrypoint logic every agent shares (caller alias, async jobs,
  uploads, conversation sessions, publishing results as a pull request)
- sessions: conversation history in DynamoDB, scoped to the caller alias, expiring after SESSION_TTL_DAYS
- uploads: presigned S3 uploads (images / documents, expiring after 90 days) and loading them as model input
- files: <file path="..."> blocks in model output -> validated {path: content}
"""
