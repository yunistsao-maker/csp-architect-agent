"""The AgentCore entrypoint logic shared by every portal agent.

    app = PortalAgentApp("coe", handler=run_coe, validate=check_coe_payload)
    @app.entrypoint
    def invoke(payload, context): return app.invoke(payload, context)

Actions (all behind the ingress gateway; the caller alias comes from the interceptor's header):
    {"prompt", ..., "session_id"?, "attachments"?: [upload keys], "async"?}   -> {"job_id"} (or the result if sync)
    {"action": "status",     "job_id"}                                      -> RUNNING | SUCCEEDED | FAILED | LOST
    {"action": "upload_url", "filename", "content_type", "size"}            -> presigned PUT to the uploads bucket
    {"action": "publish",    "job_id", "title"?, "description"?}            -> pull request with the job's files

handler(payload, ctx: RunContext) -> dict does the agent-specific work. It gets the caller alias, the attachments as
Converse content blocks, and a Strands session manager backed by DynamoDB when the request names a session_id.
A result that should be publishable carries "files": {path: content} and optionally "summary".
"""

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from jobs import JOB_ID_RE, Heartbeat, MemoryJobStore, S3JobStore, new_job_id, new_record, now, public_view
from portal.api import API_VERSION, check_version, event, stamp, trace_id
from portal.sessions import (DynamoDBSessionRepository, LeaseRenewer, SessionBusy, SessionError, SessionLock,
                             check_session_id)
from portal.uploads import UploadError, check_owner, create_upload, load_attachments
from tools_gateway import ToolsGatewayClient, ToolsGatewayError, tool_json

USER_ALIAS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Alias"


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


@dataclass
class Settings:
    # Default on: a runtime whose env vars were reset by `agentcore deploy` must not fall back to "no alias".
    require_alias: bool = field(default_factory=lambda: _flag("REQUIRE_CALLER_ALIAS", "true"))
    async_default: bool = field(default_factory=lambda: _flag("ASYNC_DEFAULT"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    job_bucket: str | None = field(default_factory=lambda: os.environ.get("JOB_BUCKET"))
    session_table: str | None = field(default_factory=lambda: os.environ.get("SESSION_TABLE"))
    session_bucket: str | None = field(default_factory=lambda: os.environ.get("SESSION_BUCKET") or os.environ.get("UPLOAD_BUCKET"))
    session_ttl_days: int = field(default_factory=lambda: int(os.environ.get("SESSION_TTL_DAYS", "90")))
    upload_bucket: str | None = field(default_factory=lambda: os.environ.get("UPLOAD_BUCKET"))
    upload_endpoint_url: str | None = field(default_factory=lambda: os.environ.get("UPLOAD_ENDPOINT_URL"))
    tools_gateway_url: str | None = field(default_factory=lambda: os.environ.get("TOOLS_GATEWAY_URL"))
    git_target: str | None = field(default_factory=lambda: os.environ.get("GIT_TARGET"))
    heartbeat_seconds: float = field(default_factory=lambda: float(os.environ.get("JOB_HEARTBEAT_SECONDS", "60")))
    stale_seconds: float = field(default_factory=lambda: float(os.environ.get("JOB_STALE_SECONDS", "300")))
    poll_hint_seconds: int = field(default_factory=lambda: int(os.environ.get("JOB_POLL_HINT_SECONDS", "15")))


@dataclass
class RunContext:
    kind: str
    alias: str | None
    job_id: str | None
    session_id: str | None
    session_manager: Any = None
    attachments: list = field(default_factory=list)
    attachment_meta: list = field(default_factory=list)
    tools: Callable[[], ToolsGatewayClient] | None = None

    def user_message(self, text: str):
        """The prompt plus any uploaded images / documents, as one user turn."""
        return [{"text": text}, *self.attachments] if self.attachments else text


def error(message: str, code: str, **extra) -> dict:
    return {"error": message, "error_code": code, "api_version": API_VERSION, **extra}


class PortalAgentApp:
    def __init__(self, kind: str, handler: Callable[[dict, RunContext], dict],
                 validate: Callable[[dict], dict | None] | None = None, app=None, settings: Settings | None = None):
        self.kind, self.handler, self.validate = kind, handler, validate
        self.settings = settings or Settings()
        if app is None:
            from bedrock_agentcore.runtime import BedrockAgentCoreApp
            app = BedrockAgentCoreApp()
        self.app = app
        self._job_store = self._s3 = self._presign_s3 = self._table = None

    # ------------------------------------------------------------------ AWS clients (lazy, replaceable in tests)
    @property
    def job_store(self):
        if self._job_store is None:
            self._job_store = S3JobStore(self.settings.job_bucket) if self.settings.job_bucket else MemoryJobStore()
        return self._job_store

    @property
    def s3(self):
        """Data-plane S3 for the runtime itself (reaches S3 through the VPC's gateway endpoint)."""
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=self.settings.region)
        return self._s3

    @property
    def presign_s3(self):
        """Presigned upload URLs must use the REGIONAL host (<bucket>.s3.<region>.amazonaws.com): on-prem DNS
        forwards s3.<region>.amazonaws.com to the inbound Resolver, which answers with the S3 interface endpoint.
        boto3's default for us-east-1 is the global host, which would resolve publicly and be denied by the bucket
        policy. UPLOAD_ENDPOINT_URL can point at the endpoint-specific DNS name instead."""
        if self._presign_s3 is None:
            import boto3
            from botocore.config import Config
            url = self.settings.upload_endpoint_url or f"https://s3.{self.settings.region}.amazonaws.com"
            self._presign_s3 = boto3.client("s3", region_name=self.settings.region, endpoint_url=url,
                                            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
        return self._presign_s3

    @property
    def table(self):
        if self._table is None and self.settings.session_table:
            import boto3
            self._table = boto3.resource("dynamodb", region_name=self.settings.region).Table(self.settings.session_table)
        return self._table

    def tools_client(self, alias: str | None) -> ToolsGatewayClient:
        return ToolsGatewayClient(self.settings.tools_gateway_url, self.settings.region, alias, agent_kind=self.kind)

    def spawn(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    # ------------------------------------------------------------------ entrypoint
    def invoke(self, payload: dict, context=None) -> dict:
        alias = self.caller_alias(context)
        if self.settings.require_alias and not alias:
            return error("Caller alias header missing; invoke this agent through the ingress gateway.", "forbidden")
        problem = check_version(payload)
        if problem:
            return stamp(problem, payload)
        action = payload.get("action", "generate")
        handlers = {"generate": self._generate, "status": self._status, "upload_url": self._upload_url,
                    "publish": self._publish}
        if action not in handlers:
            return error(f"Invalid action '{action}'. Use one of: {', '.join(handlers)}.", "bad_request")
        try:
            return handlers[action](payload, alias, context)
        except (UploadError, SessionError) as e:
            code = "session_busy" if isinstance(e, SessionBusy) else "bad_request"
            return error(str(e), code)

    @staticmethod
    def caller_alias(context) -> str | None:
        headers = getattr(context, "request_headers", None) or {}
        for k, v in headers.items():
            if k.lower() == USER_ALIAS_HEADER.lower():
                return v
        return None

    # ------------------------------------------------------------------ generate (sync or async job)
    def _generate(self, payload: dict, alias: str | None, context) -> dict:
        if not str(payload.get("prompt") or "").strip():
            return error("Missing 'prompt' in payload", "bad_request")
        problem = self.validate(payload) if self.validate else None
        if problem:
            return problem
        for key in payload.get("attachments") or []:
            check_owner(key, alias)
        if payload.get("attachments") and not self.settings.upload_bucket:
            return error("Attachments are not enabled (UPLOAD_BUCKET is not set).", "bad_request")
        session_id = payload.get("session_id")
        if session_id is not None:
            check_session_id(session_id)
            if not self.settings.session_table:
                return error("Sessions are not enabled (SESSION_TABLE is not set).", "bad_request")
            if not alias:
                return error("Sessions need a caller alias.", "forbidden")
        job_id = new_job_id()
        lock = SessionLock(self.table, alias, session_id, owner=job_id, lease_seconds=self.settings.stale_seconds) \
            if session_id else None
        if lock:
            lock.acquire()
        request = {"agent": self.kind, "mode": payload.get("mode"), "session_id": session_id,
                   "attachments": len(payload.get("attachments") or []), "prompt_chars": len(payload["prompt"]),
                   "trace_id": trace_id(payload)}
        if not bool(payload.get("async", self.settings.async_default)):
            try:
                if not lock:
                    return {**self._run(payload, alias, job_id, session_id), "caller": {"alias": alias}}
                with LeaseRenewer(lock, self.settings.heartbeat_seconds) as renewer:
                    result = self._run(payload, alias, job_id, session_id)
                if renewer.error:
                    return error(f"Session conflict: {renewer.error}", "session_conflict", caller={"alias": alias})
                return {**result, "caller": {"alias": alias}}
            finally:
                if lock:
                    lock.release()
        record = new_record(job_id, alias, request)
        self.job_store.put(record)
        self.spawn(self._run_job, record, payload, alias, lock)
        event("job_started", agent=self.kind, job_id=job_id, alias=alias, trace_id=trace_id(payload), mode=payload.get("mode"))
        return stamp({"job_id": job_id, "status": "RUNNING", "poll_after_s": self.settings.poll_hint_seconds,
                      "agent": self.kind, "session_id": session_id, "caller": {"alias": alias}}, payload)

    def _context(self, payload: dict, alias: str | None, job_id: str, session_id: str | None) -> RunContext:
        blocks, meta = [], []
        if payload.get("attachments"):
            blocks, meta = load_attachments(self.s3, self.settings.upload_bucket, payload["attachments"], alias)
        manager = None
        if session_id:
            from strands.session.repository_session_manager import RepositorySessionManager
            repo = DynamoDBSessionRepository(self.table, alias, self.settings.session_ttl_days,
                                             s3=self.s3 if self.settings.session_bucket else None,
                                             bucket=self.settings.session_bucket)
            manager = RepositorySessionManager(session_id=session_id, session_repository=repo)
        tools = (lambda: self.tools_client(alias)) if self.settings.tools_gateway_url else None
        return RunContext(self.kind, alias, job_id, session_id, manager, blocks, meta, tools)

    def _run(self, payload: dict, alias: str | None, job_id: str, session_id: str | None) -> dict:
        try:
            ctx = self._context(payload, alias, job_id, session_id)
        except UploadError as e:
            return error(str(e), "bad_request", agent=self.kind)
        result = self.handler(payload, ctx)
        out = stamp({**result, "agent": self.kind, "job_id": job_id}, payload)
        if session_id:
            out["session_id"] = session_id
        if ctx.attachment_meta:
            out["attachments"] = ctx.attachment_meta
        return out

    def _run_job(self, record: dict, payload: dict, alias: str | None, lock: SessionLock | None) -> None:
        task_id = self.app.add_async_task(f"{self.kind}_job", {"job_id": record["job_id"]})
        try:
            with Heartbeat(self.job_store, record, self.settings.heartbeat_seconds,
                           on_beat=lock.renew if lock else None) as beat:
                try:
                    result = self._run(payload, alias, record["job_id"], payload.get("session_id"))
                except Exception as e:  # noqa: BLE001 - the job record must always reach a terminal state
                    busy = isinstance(e, SessionBusy) or isinstance(getattr(e, "__cause__", None), SessionBusy)
                    result = error(f"{type(e).__name__}: {e}", "session_conflict" if busy else "internal_error",
                                   agent=self.kind)
            if beat.error and not result.get("error"):
                result = error(f"Session conflict: {beat.error}", "session_conflict", agent=self.kind)
            result = {**result, "caller": {"alias": alias}}
            status = "FAILED" if result.get("error") else "SUCCEEDED"
            self.job_store.put({**record, "status": status, "updated_at": now(), "heartbeat_at": now(),
                                "result": stamp(result, payload)})
            event("job_finished", agent=self.kind, job_id=record["job_id"], status=status, trace_id=trace_id(payload),
                  error_code=result.get("error_code"), usage=result.get("usage"))
        finally:
            if lock:
                lock.release()
            self.app.complete_async_task(task_id)

    # ------------------------------------------------------------------ status / uploads
    def _owned_record(self, job_id, alias):
        if not job_id or not JOB_ID_RE.match(str(job_id)):
            return None
        record = self.job_store.get(job_id)
        return record if record and record.get("alias") == alias else None

    def _status(self, payload: dict, alias: str | None, context) -> dict:
        record = self._owned_record(payload.get("job_id"), alias)
        if record is None:
            return error(f"Job '{payload.get('job_id')}' not found.", "not_found", job_id=payload.get("job_id"))
        view = public_view(record, self.settings.stale_seconds)
        return stamp({**view, "caller": {"alias": alias}}, trace=view.get("trace_id") or (view.get("request") or {}).get("trace_id")
                     or trace_id(payload))

    def _upload_url(self, payload: dict, alias: str | None, context) -> dict:
        if not self.settings.upload_bucket:
            return error("Uploads are not enabled (UPLOAD_BUCKET is not set).", "bad_request")
        return stamp({**create_upload(self.presign_s3, self.settings.upload_bucket, alias, payload.get("filename", ""),
                                      payload.get("content_type", ""), payload.get("size")), "caller": {"alias": alias}}, payload)

    # ------------------------------------------------------------------ publish as a pull request
    def _publish(self, payload: dict, alias: str | None, context) -> dict:
        record = self._owned_record(payload.get("job_id"), alias)
        if record is None:
            return error(f"Job '{payload.get('job_id')}' not found.", "not_found")
        result = record.get("result") or {}
        if (record.get("request") or {}).get("agent") != self.kind:
            return error(f"Job '{payload.get('job_id')}' not found.", "not_found")
        if record["status"] != "SUCCEEDED" or not result.get("files"):
            return error("Only a finished job that produced files can be published.", "bad_request",
                         status=record["status"])
        if result.get("pull_request"):  # idempotent: one job, one PR
            return stamp({"job_id": record["job_id"], "pull_request": result["pull_request"], "caller": {"alias": alias}}, payload)
        if not (self.settings.tools_gateway_url and self.settings.git_target):
            return error("Publishing is not configured (TOOLS_GATEWAY_URL / GIT_TARGET).", "bad_request")
        if result.get("publishable") is False:
            return error("The output did not pass its checks; fix it in a follow-up turn before publishing.",
                         "checks_failed", checks=result.get("checks"))
        title = (payload.get("title") or result.get("title") or f"[{self.kind}] {record['job_id'][:8]}")[:200]
        body = "\n\n".join(x for x in [
            payload.get("description") or "", result.get("summary") or "",
            f"Generated by the `{self.kind}` portal agent for `{alias}` (job `{record['job_id']}`)."] if x)
        try:
            client = self.tools_client(alias)
            name = client.find_tool(self.settings.git_target, "open_pull_request")["name"]
            pr = tool_json(client.call_tool(name, {
                "agent": self.kind, "job_id": record["job_id"], "title": title, "body": body,
                "files": [{"path": p, "content": c} for p, c in sorted(result["files"].items())]}))
        except (ToolsGatewayError, OSError, ValueError, KeyError) as e:
            return error(f"Could not open the pull request: {e}", "publish_failed")
        if pr.get("error"):
            return error(f"Could not open the pull request: {pr['error']}", "publish_failed")
        self.job_store.put({**record, "result": {**result, "pull_request": pr}, "updated_at": now()})
        return stamp({"job_id": record["job_id"], "pull_request": pr, "caller": {"alias": alias}}, payload)
