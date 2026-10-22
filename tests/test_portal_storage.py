"""Portal storage: file blocks, presigned uploads + attachments (S3), conversation sessions + locks (DynamoDB). moto, no AWS."""
import time
from datetime import datetime, timezone

import pytest
from strands import Agent
from strands.session.repository_session_manager import RepositorySessionManager

from portal.files import FileBlockError, extract_files, safe_path, strip_files
from portal.sessions import (DynamoDBSessionRepository, SessionBusy, SessionError, SessionLock, check_session_id,
                             delete_messages_after)
from portal.uploads import UploadError, check_owner, create_upload, load_attachments
from test_skills import ScriptedModel

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


# ---------------------------------------------------------------------------------------------- files
def test_extract_files_and_paths():
    text = ('Intro\n<file path="main.tf">\n```hcl\nresource "x" "y" {}\n```\n</file>\n'
            '<file path="modules/net/variables.tf">variable "a" {}</file>\n<file path="../etc/passwd">x</file>\nBye')
    files, errors = extract_files(text, (".tf",))
    assert files == {"main.tf": 'resource "x" "y" {}\n', "modules/net/variables.tf": 'variable "a" {}\n'}
    assert len(errors) == 1 and "escapes" in errors[0]
    assert strip_files(text) == "Intro\n\nBye"


@pytest.mark.parametrize("bad", ["/abs.tf", "C:/x.tf", "a/../../b.tf", ".hidden/x.tf", "a b.tf", "", "x.sh"])
def test_safe_path_rejects(bad):
    with pytest.raises(FileBlockError):
        safe_path(bad, (".tf",))


# ---------------------------------------------------------------------------------------------- uploads
def test_presigned_upload_is_scoped_to_alias_type_and_size(aws):
    import boto3
    from botocore.config import Config
    s3v4 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))  # as the runtime uses
    up = create_upload(s3v4, "uploads-bucket", "yhtsao", "../白板 photo.PNG", "image/png", 1234,
                       now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert up["key"].startswith("uploads/yhtsao/2026/10/22/") and up["key"].endswith("/photo.PNG")
    assert up["method"] == "PUT" and up["headers"] == {"Content-Type": "image/png", "Content-Length": "1234"}
    assert "X-Amz-Signature=" in up["url"] and "content-type" in up["url"].lower()
    assert up["retention_days"] == 90


@pytest.mark.parametrize("ctype,size,msg", [("application/x-msdownload", 10, "Unsupported"),
                                            ("image/png", 4_000_000, "must be"), ("application/pdf", 0, "must be"),
                                            ("image/png", "big", "size")])
def test_upload_rejections(aws, ctype, size, msg):
    with pytest.raises(UploadError, match=msg):
        create_upload(aws["s3"], "uploads-bucket", "yhtsao", "x", ctype, size)


def test_attachments_load_as_converse_blocks(aws):
    s3 = aws["s3"]
    s3.put_object(Bucket="uploads-bucket", Key="uploads/yhtsao/a/diagram.png", Body=PNG, ContentType="image/png")
    s3.put_object(Bucket="uploads-bucket", Key="uploads/yhtsao/b/設計 v2.docx", Body=b"PK..", ContentType=
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    s3.put_object(Bucket="uploads-bucket", Key="uploads/yhtsao/c/設計 v2.md", Body=b"# x", ContentType="text/markdown")
    blocks, meta = load_attachments(s3, "uploads-bucket", ["uploads/yhtsao/a/diagram.png", "uploads/yhtsao/b/設計 v2.docx",
                                                           "uploads/yhtsao/c/設計 v2.md"], "yhtsao")
    assert blocks[0] == {"image": {"format": "png", "source": {"bytes": PNG}}}
    assert blocks[1]["document"]["format"] == "docx" and blocks[2]["document"]["format"] == "md"
    names = [b["document"]["name"] for b in blocks[1:]]
    assert len(set(names)) == 2 and all(n.replace(" ", "").replace("(", "").replace(")", "").replace("-", "").isalnum() for n in names)
    assert [m["kind"] for m in meta] == ["image", "document", "document"]


def test_attachments_ownership_and_content_checks(aws):
    s3 = aws["s3"]
    s3.put_object(Bucket="uploads-bucket", Key="uploads/eve/x.png", Body=PNG, ContentType="image/png")
    s3.put_object(Bucket="uploads-bucket", Key="uploads/yhtsao/fake.png", Body=b"MZ..", ContentType="image/png")
    for key in ("uploads/eve/x.png", "uploads/yhtsao/../eve/x.png", "uploads/yhtsao/missing.png"):
        with pytest.raises(UploadError, match="not found"):
            load_attachments(s3, "uploads-bucket", [key], "yhtsao")
    with pytest.raises(UploadError, match="does not look like"):
        load_attachments(s3, "uploads-bucket", ["uploads/yhtsao/fake.png"], "yhtsao")
    with pytest.raises(UploadError):
        check_owner("uploads/yhtsao/x", None)


# ---------------------------------------------------------------------------------------------- sessions
def agent_with(repo, session_id, turns):
    model = ScriptedModel(turns)
    mgr = RepositorySessionManager(session_id=session_id, session_repository=repo)
    return Agent(model=model, agent_id="coe", session_manager=mgr, callback_handler=None), model, mgr


def test_conversation_survives_a_new_microvm(aws):
    """Turn 1 and turn 2 run in different Agent objects (as on two runtime sessions); history comes from DynamoDB."""
    repo = DynamoDBSessionRepository(aws["table"], "yhtsao")
    a1, _, _ = agent_with(repo, "pay-2026-001", [{"text": "draft v1"}])
    a1("prepare the doc")
    a2, model, _ = agent_with(DynamoDBSessionRepository(aws["table"], "yhtsao"), "pay-2026-001", [{"text": "draft v2"}])
    assert [m["role"] for m in a2.messages] == ["user", "assistant"]
    a2("add the cost section")
    assert [c["text"] for m in a2.messages for c in m["content"]] == [
        "prepare the doc", "draft v1", "add the cost section", "draft v2"]
    items = aws["table"].scan()["Items"]
    assert {i["PK"] for i in items} == {"yhtsao#pay-2026-001"}
    assert len({i["expires_at"] for i in items}) == 1  # the whole conversation expires together
    assert abs(int(items[0]["expires_at"]) - (time.time() + 90 * 86400)) < 120


def test_sessions_are_scoped_to_the_alias(aws):
    a1, _, _ = agent_with(DynamoDBSessionRepository(aws["table"], "yhtsao"), "shared-id-123", [{"text": "secret"}])
    a1("my private draft")
    other = DynamoDBSessionRepository(aws["table"], "eve")
    assert other.read_session("shared-id-123") is None and other.list_messages("shared-id-123", "coe") == []
    a3, _, _ = agent_with(other, "shared-id-123", [{"text": "hi"}])
    assert a3.messages == []


def test_expired_session_starts_over(aws):
    clock = [1000.0]
    repo = DynamoDBSessionRepository(aws["table"], "yhtsao", ttl_days=1, clock=lambda: clock[0])
    a1, _, _ = agent_with(repo, "old-session-1", [{"text": "v1"}])
    a1("hello")
    clock[0] += 2 * 86400  # TTL passed, item not yet deleted by DynamoDB
    repo2 = DynamoDBSessionRepository(aws["table"], "yhtsao", ttl_days=1, clock=lambda: clock[0])
    a2, _, _ = agent_with(repo2, "old-session-1", [{"text": "v2"}])
    assert a2.messages == []
    a2("again")
    assert [m["role"] for m in a2.messages] == ["user", "assistant"]


def test_large_messages_are_offloaded_to_s3(aws):
    repo = DynamoDBSessionRepository(aws["table"], "yhtsao", s3=aws["s3"], bucket="uploads-bucket")
    big = "x" * 400_000
    a1, _, _ = agent_with(repo, "big-session-1", [{"text": big}])
    a1("make it huge")
    keys = [o["Key"] for o in aws["s3"].list_objects_v2(Bucket="uploads-bucket", Prefix="sessions/")["Contents"]]
    assert keys == ["sessions/yhtsao/big-session-1/MSG_coe_00000001.json"]
    a2, _, _ = agent_with(DynamoDBSessionRepository(aws["table"], "yhtsao", s3=aws["s3"], bucket="uploads-bucket"),
                          "big-session-1", [])
    assert a2.messages[1]["content"][0]["text"] == big
    with pytest.raises(Exception, match="SESSION_BUCKET"):  # Strands wraps it in EventLoopException
        a3, _, _ = agent_with(DynamoDBSessionRepository(aws["table"], "yhtsao"), "big-session-2", [{"text": big}])
        a3("again")


def test_rollback_deletes_persisted_messages(aws):
    repo = DynamoDBSessionRepository(aws["table"], "yhtsao")
    a1, _, mgr = agent_with(repo, "rollback-1", [{"text": "one"}, {"text": "two"}])
    a1("first")
    a1("second")
    delete_messages_after(repo, "rollback-1", "coe", 1)
    assert [m.message_id for m in repo.list_messages("rollback-1", "coe")] == [0, 1]


def test_session_lock_lease(aws):
    clock = [1000.0]
    lock_a = SessionLock(aws["table"], "yhtsao", "busy-session", "job-a", lease_seconds=300, clock=lambda: clock[0])
    lock_b = SessionLock(aws["table"], "yhtsao", "busy-session", "job-b", lease_seconds=300, clock=lambda: clock[0])
    lock_a.acquire()
    with pytest.raises(SessionBusy):
        lock_b.acquire()
    SessionLock(aws["table"], "eve", "busy-session", "job-e", clock=lambda: clock[0]).acquire()  # other alias: other key
    clock[0] += 200
    lock_a.renew()
    clock[0] += 200
    with pytest.raises(SessionBusy):  # renewed lease still valid
        lock_b.acquire()
    clock[0] += 400
    lock_b.acquire()  # lease expired (job's microVM died) -> taken over
    lock_a.release()  # no effect on b's lock
    with pytest.raises(SessionBusy):
        SessionLock(aws["table"], "yhtsao", "busy-session", "job-c", clock=lambda: clock[0]).acquire()
    lock_b.release()
    SessionLock(aws["table"], "yhtsao", "busy-session", "job-c", clock=lambda: clock[0]).acquire()


@pytest.mark.parametrize("sid", ["short", "has space here", "x" * 129, None, "../../etc"])
def test_session_ids(sid):
    with pytest.raises(SessionError):
        check_session_id(sid)


# ---------------------------------------------------------------------------------------------- review findings
def test_two_writers_cannot_overwrite_a_turn(aws):
    repo1, repo2 = (DynamoDBSessionRepository(aws["table"], "yhtsao") for _ in range(2))
    a1, _, _ = agent_with(repo1, "race-session", [{"text": "v1"}])
    a1("first")
    b1, _, _ = agent_with(repo1, "race-session", [{"text": "from b"}])  # both restored the same history
    c1, _, _ = agent_with(repo2, "race-session", [{"text": "from c"}])
    b1("second (b)")
    with pytest.raises(Exception, match="already exists"):
        c1("second (c)")
    texts = [m.message["content"][0]["text"] for m in repo1.list_messages("race-session", "coe")]
    assert texts == ["first", "v1", "second (b)", "from b"]


def test_attachment_bytes_are_not_persisted(aws):
    repo = DynamoDBSessionRepository(aws["table"], "yhtsao")
    a1, model, _ = agent_with(repo, "attach-session", [{"text": "I see a diagram"}])
    a1([{"text": "what is this?"}, {"image": {"format": "png", "source": {"bytes": PNG}}},
        {"document": {"format": "pdf", "name": "spec", "source": {"bytes": b"%PDF-1.7"}}}])
    assert model.seen[0]["content"][1]["image"]["source"]["bytes"] == PNG  # the model saw the real image
    stored = repo.list_messages("attach-session", "coe")[0].message["content"]
    assert stored[0] == {"text": "what is this?"} and "image" not in stored[1] and "not resent" in stored[1]["text"]
    assert "'spec' (pdf)" in stored[2]["text"]


class MaxTokensModel(ScriptedModel):
    async def stream(self, messages, *a, **kw):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "<document>truncated"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "max_tokens"}}


def test_max_tokens_rolls_back_the_persisted_turn(aws):
    from portal.model import OutputTruncated, run_turn
    repo = DynamoDBSessionRepository(aws["table"], "yhtsao")
    a1, _, mgr = agent_with(repo, "maxtok-session", [{"text": "v1"}])
    run_turn(a1, "first", None, mgr)
    a1.model = MaxTokensModel([])
    with pytest.raises(OutputTruncated):
        run_turn(a1, "make it huge", None, mgr)
    assert [m.message_id for m in repo.list_messages("maxtok-session", "coe")] == [0, 1] and len(a1.messages) == 2
    a1.model = ScriptedModel([{"text": "smaller"}])
    run_turn(a1, "smaller please", None, mgr)
    assert [m.message_id for m in repo.list_messages("maxtok-session", "coe")] == [0, 1, 2, 3]


def test_heartbeat_exit_waits_for_an_in_flight_beat():
    import jobs

    class SlowStore(jobs.MemoryJobStore):
        def put(self, record):
            if record["status"] == "RUNNING":
                time.sleep(0.3)  # slow S3 put of the RUNNING beat
            super().put(record)

    store = SlowStore()
    rec = jobs.new_record(jobs.new_job_id(), "yhtsao", {})
    with jobs.Heartbeat(store, rec, interval=0.01):
        time.sleep(0.05)  # a beat is now in flight
    store.put({**rec, "status": "SUCCEEDED"})
    time.sleep(0.4)
    assert store.get(rec["job_id"])["status"] == "SUCCEEDED"
