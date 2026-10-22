"""Extract generated files from model output.

Agents that produce several files (Terraform modules, documents) are asked to wrap each one as

    <file path="modules/network/main.tf">
    ...content...
    </file>

which survives code fences, Markdown and CJK text better than "```hcl title=..." conventions.
"""

import posixpath
import re

FILE_RE = re.compile(r'<file\s+path="([^"]+)"\s*>\n?(.*?)\n?</file>', re.S)
FENCE_RE = re.compile(r"^```[\w+-]*\n(.*)\n```$", re.S)
MAX_FILES = 60
MAX_FILE_BYTES = 200_000


class FileBlockError(ValueError):
    pass


def safe_path(path: str, allowed_suffixes: tuple[str, ...] | None = None) -> str:
    """Normalise a relative path and refuse anything that could escape the target directory."""
    p = path.strip().replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise FileBlockError(f"absolute or empty path: {path!r}")
    norm = posixpath.normpath(p)
    if norm.startswith("..") or "/../" in f"/{norm}/" or norm in (".", ""):
        raise FileBlockError(f"path escapes the output directory: {path!r}")
    if any(part.startswith(".") and part not in (".terraform.lock.hcl",) for part in norm.split("/")):
        raise FileBlockError(f"hidden files are not allowed: {path!r}")
    if not re.fullmatch(r"[A-Za-z0-9._/\-]+", norm):
        raise FileBlockError(f"unsupported characters in path: {path!r}")
    if allowed_suffixes and not norm.endswith(allowed_suffixes):
        raise FileBlockError(f"file type not allowed: {path!r} (allowed: {', '.join(allowed_suffixes)})")
    return norm


def extract_files(text: str, allowed_suffixes: tuple[str, ...] | None = None) -> tuple[dict[str, str], list[str]]:
    """Return ({path: content}, errors). Later blocks for the same path replace earlier ones."""
    files, errors = {}, []
    for raw_path, body in FILE_RE.findall(text or ""):
        try:
            path = safe_path(raw_path, allowed_suffixes)
        except FileBlockError as e:
            errors.append(str(e))
            continue
        m = FENCE_RE.match(body.strip())
        content = (m.group(1) if m else body).rstrip() + "\n"
        if len(content.encode()) > MAX_FILE_BYTES:
            errors.append(f"{path}: larger than {MAX_FILE_BYTES} bytes")
            continue
        files[path] = content
    if len(files) > MAX_FILES:
        errors.append(f"too many files ({len(files)} > {MAX_FILES})")
    return files, errors


def strip_files(text: str) -> str:
    """The prose around the file blocks (the agent's explanation)."""
    return re.sub(r"\n{3,}", "\n\n", FILE_RE.sub("", text or "")).strip()
