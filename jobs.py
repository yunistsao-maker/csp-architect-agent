"""Job records for asynchronous diagram generation.

AgentCore Gateway ends an invocation after 15 minutes, and a large networking diagram can take longer than that.
So in async mode one call submits a job and returns its id at once, the agent keeps working in the background
(the runtime stays HealthyBusy, for up to 8 hours), and the client polls with short status calls.

A status call may land in a different runtime session (another microVM) from the one doing the work, so job
state lives in S3, not in memory. A running job writes a heartbeat; if the heartbeat stops (the microVM died),
status reports LOST instead of RUNNING forever.
"""

import json
import re
import threading
import time
import uuid

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = ("SUCCEEDED", "FAILED")


def now() -> float:
    return round(time.time(), 3)


def new_job_id() -> str:
    return uuid.uuid4().hex


class MemoryJobStore:
    """In-process store for local runs (`python agent.py`) and tests. Not shared between microVMs."""

    def __init__(self):
        self._jobs, self._lock = {}, threading.Lock()

    def put(self, record: dict) -> None:
        with self._lock:
            self._jobs[record["job_id"]] = json.loads(json.dumps(record))

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            rec = self._jobs.get(job_id)
            return json.loads(json.dumps(rec)) if rec else None


class S3JobStore:
    """One JSON object per job at s3://BUCKET/jobs/JOB_ID.json (S3 is strongly read-after-write consistent)."""

    def __init__(self, bucket: str, prefix: str = "jobs/", client=None):
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.bucket, self.prefix, self.s3 = bucket, prefix, client

    def _key(self, job_id: str) -> str:
        return f"{self.prefix}{job_id}.json"

    def put(self, record: dict) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(record["job_id"]), ContentType="application/json",
                           Body=json.dumps(record, ensure_ascii=False).encode())

    def get(self, job_id: str) -> dict | None:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self._key(job_id))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None
        except Exception as e:  # noqa: BLE001 - a missing key can also surface as AccessDenied / 404 ClientError
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "AccessDenied"):
                return None
            raise
        return json.loads(body)


def new_record(job_id: str, alias: str | None, request: dict) -> dict:
    t = now()
    return {"job_id": job_id, "alias": alias, "status": "RUNNING", "created_at": t, "updated_at": t,
            "heartbeat_at": t, "request": request}


def public_view(record: dict, stale_after: float, at: float | None = None) -> dict:
    """What a status call returns. A finished job carries the full result (xml, explanation, validation, ...)."""
    at = at or now()
    status = record["status"]
    if status == "RUNNING" and at - record.get("heartbeat_at", record["updated_at"]) > stale_after:
        status = "LOST"
    view = {"job_id": record["job_id"], "status": status, "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "elapsed_s": round((record["updated_at"] if status in TERMINAL else at) - record["created_at"], 1),
            "request": record.get("request", {})}
    if status == "LOST":
        view["error"] = ("The job stopped reporting progress (the runtime session ended). Submit it again; "
                         "large diagrams finish faster with one page per concern.")
        view["error_code"] = "job_lost"
    if status in TERMINAL:
        view.update(record.get("result") or {})
    return view


class Heartbeat:
    """Refreshes heartbeat_at every `interval` seconds while the job runs."""

    def __init__(self, store, record: dict, interval: float, on_beat=None):
        self.store, self.record, self.interval, self.on_beat = store, record, interval, on_beat
        self.error: Exception | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.store.put({**self.record, "heartbeat_at": now()})
            except Exception:  # noqa: BLE001 - a missed beat only matters if every beat fails
                pass
            if self.on_beat:
                try:
                    self.on_beat()  # e.g. renew the session lock lease
                except Exception as e:  # noqa: BLE001 - remembered; the job decides what it means
                    self.error = e

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        # Wait for an in-flight beat: a RUNNING put that lands after the final status would undo it.
        self._thread.join()
        return False
