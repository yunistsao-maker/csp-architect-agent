# Agent API contract

What a caller (a portal such as Cloud Pro, `gateway/portal_client.py`, a CI job) can rely on when it calls the
runtimes through the ingress gateway: `POST https://<gateway>/<target>/invocations` with a JSON body.

| File | What it is |
| --- | --- |
| `VERSION` | Contract version the runtimes implement (semver), also returned as `api_version` in every response |
| `v1/api.schema.json` | JSON Schema (2020-12) of every request and response, in `$defs` |
| `v1/fixtures/examples.json` | One valid instance per message shape; consumers feed them to their parsers |
| `v1/fixtures/form-flags.json` | Golden cases for the review flag of a filled form item (veto / exception / schedule) |

Targets: `csp-architect` (diagram), `coe-doc` and `aicoe-doc` (documents and forms), `terraform` — the names in
`gateway/agents.yaml`; deployments may rename them, so consumers keep them in configuration.

## Rules

**Both sides**
- Readers ignore fields they don't know. Every object in the schema allows additional properties.
- Errors are `{"error": "...", "error_code": "..."}` from every action. Branch on `error_code`; treat an unknown
  code as a generic failure.
- `trace_id` (optional, `[A-Za-z0-9._:-]{1,128}`) is echoed in job records, results and the runtime's structured
  logs (`event: job_started / job_finished`).

**Runtimes (this repository)**
- Within v1, only add optional request fields and new response fields. Never remove or rename a field, change its
  type, or start requiring a request field.
- A request with `api_version` whose major isn't served gets `error_code: unsupported_api_version`.
- `tests/test_contract.py` validates the schema, the fixtures, every example request against the runtimes'
  own payload checks, and real runtime output against the schema. A change that breaks a consumer fails there.

**Consumers**
- Pin the contract: copy this directory at a commit or tag, and check in CI that the copy still matches it.
- Send `api_version: "1"`. Validate what you send against `*.request` in your own tests; parse the examples.
- Upgrade the pin when you choose to; new minor versions never require a change.

## Versions

- Minor / patch (`1.x.y`): additive changes, documented below; existing consumers keep working unchanged.
- Major: a new `v2/` directory next to `v1/`. Runtimes serve both majors (chosen by `api_version`) until every
  known consumer has moved; only then is `v1/` removed.

## Changelog

- **1.0.0**: first published version: diagram, fill / prepare / review, Terraform, status, upload_url, publish,
  errors, `api_version`, `trace_id`.
