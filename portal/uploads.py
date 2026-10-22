"""User uploads (screenshots, whiteboard photos, existing documents) for the portal agents.

Flow:
  1. The portal calls the agent with {"action": "upload_url", "filename", "content_type", "size"}.
  2. The runtime returns a presigned PUT URL for s3://UPLOAD_BUCKET/uploads/<alias>/<yyyy>/<mm>/<dd>/<id>/<name>,
     valid for a few minutes, bound to that content type and exact size.
  3. The portal PUTs the file over Direct Connect to the S3 interface endpoint. On-prem DNS resolves the bucket
     host to the endpoint, so no internet path is used. Alternatively, set UPLOAD_ENDPOINT_URL to the
     endpoint-specific DNS name.
  4. The portal sends {"prompt", "attachments": ["uploads/<alias>/..."]}. The runtime checks that every key
     belongs to the caller alias, and passes images / documents to Claude as Converse content blocks.

The bucket's lifecycle rule deletes uploads/ after 90 days (gateway/deploy.py).
"""

import re
import uuid
from datetime import datetime, timezone

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
DOCUMENT_TYPES = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "text/html": "html",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
# Bedrock Converse limits: images 3.75 MB each, documents 4.5 MB each, at most 5 documents per request.
MAX_IMAGE_BYTES = 3_750_000
MAX_DOCUMENT_BYTES = 4_500_000
MAX_IMAGES, MAX_DOCUMENTS = 10, 5
UPLOAD_URL_SECONDS = 300
PREFIX = "uploads/"
MAGIC = {"png": b"\x89PNG", "jpeg": b"\xff\xd8\xff", "gif": b"GIF8", "webp": b"RIFF", "pdf": b"%PDF"}


class UploadError(ValueError):
    """A problem the caller can fix (wrong type, too big, not their file)."""


def _clean_name(filename: str) -> str:
    base = re.split(r"[\\/]", filename or "")[-1]
    stem, dot, ext = base.rpartition(".")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem if dot else base).strip("-")[:80] or "file"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8] if dot else ""
    return f"{stem}.{ext}" if ext else stem


def kind_of(content_type: str) -> tuple[str, str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in IMAGE_TYPES:
        return "image", IMAGE_TYPES[ct]
    if ct in DOCUMENT_TYPES:
        return "document", DOCUMENT_TYPES[ct]
    raise UploadError(f"Unsupported content type '{content_type}'. Images: {', '.join(IMAGE_TYPES)}; "
                      f"documents: {', '.join(DOCUMENT_TYPES)}.")


def create_upload(s3, bucket: str, alias: str, filename: str, content_type: str, size: int,
                  now: datetime | None = None) -> dict:
    if not alias:
        raise UploadError("Uploads need a caller alias.")
    kind, _fmt = kind_of(content_type)
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise UploadError("'size' (bytes) is required.") from None
    if not 0 < size <= limit:
        raise UploadError(f"{kind.capitalize()} uploads must be 1..{limit} bytes (got {size}).")
    now = now or datetime.now(timezone.utc)
    key = f"{PREFIX}{alias}/{now:%Y/%m/%d}/{uuid.uuid4().hex}/{_clean_name(filename)}"
    ct = content_type.split(";")[0].strip().lower()
    url = s3.generate_presigned_url("put_object", ExpiresIn=UPLOAD_URL_SECONDS, HttpMethod="PUT", Params={
        "Bucket": bucket, "Key": key, "ContentType": ct, "ContentLength": size})
    return {"key": key, "url": url, "method": "PUT", "headers": {"Content-Type": ct, "Content-Length": str(size)},
            "expires_in": UPLOAD_URL_SECONDS, "kind": kind, "retention_days": 90}


def check_owner(key: str, alias: str | None) -> str:
    if not isinstance(key, str) or not alias or not key.startswith(f"{PREFIX}{alias}/") or ".." in key:
        raise UploadError(f"Attachment '{key}' was not found.")  # same answer for "not yours" and "missing"
    return key


def _doc_name(key: str, used: set[str]) -> str:
    """Bedrock document names: letters, digits, single spaces, hyphens, parentheses, brackets; unique."""
    stem = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ()\[\]-]", " ", stem)).strip()[:60] or "document"
    base, n = name, 2
    while name in used:
        name, n = f"{base} ({n})", n + 1
    used.add(name)
    return name


def load_attachments(s3, bucket: str, keys: list[str], alias: str | None) -> tuple[list[dict], list[dict]]:
    """Return (Converse content blocks, metadata for the response). Raises UploadError on any bad key."""
    if not keys:
        return [], []
    if not isinstance(keys, list):
        raise UploadError("'attachments' must be a list of upload keys.")
    blocks, meta, used, counts = [], [], set(), {"image": 0, "document": 0}
    for key in keys:
        check_owner(key, alias)
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "AccessDenied") or type(e).__name__ == "NoSuchKey":
                raise UploadError(f"Attachment '{key}' was not found.") from None
            raise
        kind, fmt = kind_of(obj.get("ContentType", ""))
        data = obj["Body"].read()
        limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
        if len(data) > limit:
            raise UploadError(f"Attachment '{key}' is larger than {limit} bytes.")
        if fmt in MAGIC and not data.startswith(MAGIC[fmt]):
            raise UploadError(f"Attachment '{key}' does not look like a {fmt} file.")
        counts[kind] += 1
        if counts["image"] > MAX_IMAGES or counts["document"] > MAX_DOCUMENTS:
            raise UploadError(f"At most {MAX_IMAGES} images and {MAX_DOCUMENTS} documents per request.")
        if kind == "image":
            blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
        else:
            blocks.append({"document": {"format": fmt, "name": _doc_name(key, used), "source": {"bytes": data}}})
        meta.append({"key": key, "kind": kind, "format": fmt, "bytes": len(data)})
    return blocks, meta
