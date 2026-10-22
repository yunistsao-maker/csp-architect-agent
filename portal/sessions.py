"""Conversation sessions in DynamoDB.

A gateway call may land in any runtime microVM, so a multi-turn conversation can't live in memory. Strands keeps the
history through a SessionRepository; this one stores it in one DynamoDB table:

    PK = "<alias>#<session_id>"          (a user can only ever address their own sessions)
    SK = "SESSION" | "AGENT#<agent_id>" | "MSG#<agent_id>#<00000042>" | "LOCK"
    data       JSON of the Strands object, or s3_key when the item would pass the 400 KB DynamoDB limit
    expires_at epoch seconds; the table's TTL attribute. Fixed when the session is created (SESSION_TTL_DAYS,
               default 90), so the whole conversation expires together instead of losing its oldest turns.

Large items (diagrams, long documents, image bytes in the history) are offloaded to
s3://SESSION_BUCKET/sessions/<alias>/<session_id>/..., which has the same 90-day lifecycle rule.

SessionLock prevents two jobs from appending to the same conversation at once. It is a lease renewed by the job
heartbeat, so a microVM that dies frees the session within a few minutes.
"""

import json
import re
import time

from boto3.dynamodb.conditions import Attr, Key
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
OFFLOAD_BYTES = 300_000  # DynamoDB items max out at 400 KB including attribute names


class SessionError(ValueError):
    pass


class SessionBusy(SessionError):
    pass


def check_session_id(session_id) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise SessionError("session_id must be 8-128 characters of letters, digits, '.', '_', ':' or '-'.")
    return session_id


class DynamoDBSessionRepository(SessionRepository):
    def __init__(self, table, alias: str, ttl_days: int = 90, s3=None, bucket: str | None = None,
                 clock=time.time):
        if not alias:
            raise SessionError("Sessions need a caller alias.")
        self.table, self.alias, self.ttl_days, self.s3, self.bucket, self.clock = table, alias, ttl_days, s3, bucket, clock
        self._expires: dict[str, int] = {}

    # ------------------------------------------------------------------ storage helpers
    def _pk(self, session_id: str) -> str:
        return f"{self.alias}#{check_session_id(session_id)}"

    def _expires_at(self, session_id: str) -> int:
        if session_id not in self._expires:
            item = self.table.get_item(Key={"PK": self._pk(session_id), "SK": "SESSION"}).get("Item")
            self._expires[session_id] = int(item["expires_at"]) if item else int(self.clock() + self.ttl_days * 86400)
        return self._expires[session_id]

    def _put(self, session_id: str, sk: str, obj: dict, condition=None):
        body = json.dumps(obj, ensure_ascii=False)
        item = {"PK": self._pk(session_id), "SK": sk, "expires_at": self._expires_at(session_id)}
        if len(body.encode()) > OFFLOAD_BYTES:
            if not (self.s3 and self.bucket):
                raise SessionError(f"Session item {sk} is {len(body.encode())} bytes; set SESSION_BUCKET to offload it.")
            key = f"sessions/{self.alias}/{session_id}/{sk.replace('#', '_')}.json"
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode(), ContentType="application/json")
            item["s3_key"] = key
        else:
            item["data"] = body
        kwargs = {"ConditionExpression": condition} if condition is not None else {}
        self.table.put_item(Item=item, **kwargs)

    def _load(self, item: dict | None) -> dict | None:
        if not item or int(item.get("expires_at", 0)) < self.clock():  # TTL deletion can lag by up to ~48 h
            return None
        if "s3_key" in item:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=item["s3_key"])["Body"].read())
        return json.loads(item["data"])

    def _get(self, session_id: str, sk: str) -> dict | None:
        return self._load(self.table.get_item(Key={"PK": self._pk(session_id), "SK": sk}).get("Item"))

    # ------------------------------------------------------------------ SessionRepository
    def create_session(self, session: Session, **kwargs) -> Session:
        self._expires[session.session_id] = int(self.clock() + self.ttl_days * 86400)
        # An expired session may still be in the table until TTL deletion catches up; it can be started over.
        self._put(session.session_id, "SESSION", session.to_dict(),
                  condition=Attr("PK").not_exists() | Attr("expires_at").lt(int(self.clock())))
        return session

    def read_session(self, session_id: str, **kwargs) -> Session | None:
        data = self._get(session_id, "SESSION")
        return Session.from_dict(data) if data else None

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs) -> None:
        self._put(session_id, f"AGENT#{session_agent.agent_id}", session_agent.to_dict())

    def read_agent(self, session_id: str, agent_id: str, **kwargs) -> SessionAgent | None:
        data = self._get(session_id, f"AGENT#{agent_id}")
        return SessionAgent.from_dict(data) if data else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs) -> None:
        self.create_agent(session_id, session_agent)

    @staticmethod
    def _msg_sk(agent_id: str, message_id: int) -> str:
        return f"MSG#{agent_id}#{int(message_id):08d}"

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs) -> None:
        self._put(session_id, self._msg_sk(agent_id, session_message.message_id), session_message.to_dict())

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs) -> SessionMessage | None:
        data = self._get(session_id, self._msg_sk(agent_id, message_id))
        return SessionMessage.from_dict(data) if data else None

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs) -> None:
        self.create_message(session_id, agent_id, session_message)

    def list_messages(self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0,
                      **kwargs) -> list[SessionMessage]:
        items, start = [], None
        while True:
            q = {"KeyConditionExpression": Key("PK").eq(self._pk(session_id)) & Key("SK").begins_with(f"MSG#{agent_id}#"),
                 "ScanIndexForward": True}
            if start:
                q["ExclusiveStartKey"] = start
            resp = self.table.query(**q)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start or (limit is not None and len(items) >= offset + limit):
                break
        items = items[offset:] if limit is None else items[offset:offset + limit]
        return [SessionMessage.from_dict(d) for d in (self._load(i) for i in items) if d]


class SessionLock:
    """One running job per (alias, session). A lease, renewed by the job heartbeat."""

    def __init__(self, table, alias: str, session_id: str, owner: str, lease_seconds: float = 300, clock=time.time):
        self.table, self.owner, self.lease, self.clock = table, owner, lease_seconds, clock
        self.pk = f"{alias}#{check_session_id(session_id)}"

    def acquire(self) -> None:
        now = self.clock()
        try:
            self.table.put_item(
                Item={"PK": self.pk, "SK": "LOCK", "owner": self.owner, "lease_until": int(now + self.lease),
                      "expires_at": int(now + 86400)},
                ConditionExpression=Attr("PK").not_exists() | Attr("lease_until").lt(int(now)) | Attr("owner").eq(self.owner))
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            raise SessionBusy("Another request is still running in this session; poll its job first.") from None

    def renew(self) -> None:
        now = self.clock()
        self.table.update_item(Key={"PK": self.pk, "SK": "LOCK"}, UpdateExpression="SET lease_until = :l",
                               ConditionExpression=Attr("owner").eq(self.owner),
                               ExpressionAttributeValues={":l": int(now + self.lease)})

    def release(self) -> None:
        try:
            self.table.delete_item(Key={"PK": self.pk, "SK": "LOCK"}, ConditionExpression=Attr("owner").eq(self.owner))
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            pass  # the lease expired and someone else holds it now


def delete_messages_after(repo: DynamoDBSessionRepository, session_id: str, agent_id: str, last_kept: int | None):
    """Remove persisted messages newer than `last_kept` (None = all), e.g. after a max_tokens rollback."""
    for msg in repo.list_messages(session_id, agent_id):
        if last_kept is None or msg.message_id > last_kept:
            repo.table.delete_item(Key={"PK": repo._pk(session_id), "SK": repo._msg_sk(agent_id, msg.message_id)})
