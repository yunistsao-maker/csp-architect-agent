"""The runtimes are deployed with `agentcore ... -dt direct_code_deploy`. The starter toolkit builds code.zip with its
own built-in ignore list (not the project's .dockerignore), which drops every directory with one of these names at
any depth. A runtime file under such a directory is missing in /var/task (templates/terraform/standards.yaml was)."""
import fnmatch
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Directory names from bedrock_agentcore_starter_toolkit utils/runtime/templates/dockerignore.template
IGNORED_DIRS = {"build", "dist", "__pycache__", ".venv", "venv", "env", "ENV", "node_modules", ".pytest_cache",
                "htmlcov", ".tox", ".hypothesis", ".mypy_cache", ".ruff_cache", ".vscode", ".idea", ".git", "docs",
                ".github", "tests", ".bedrock_agentcore", "cdk", "terraform"}
IGNORED_FILES = ["*.egg", "*.py[cod]", "*.so", ".env", "*.log", "*.bak", "*.swp", "*.swo", "*~", ".DS_Store",
                 ".gitignore", ".gitattributes", ".dockerignore", ".bedrock_agentcore.yaml", ".coverage*", "*.cover"]
NOT_DEPLOYED = ("docs/", "tests/", ".github/", "evals/", "examples/", "scripts/", "gateway/", ".gitignore")


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p and not p.startswith(NOT_DEPLOYED)]


def test_runtime_files_survive_the_toolkit_packaging():
    dropped = []
    for path in tracked():
        parts = path.split("/")
        if set(parts[:-1]) & IGNORED_DIRS or any(fnmatch.fnmatch(parts[-1], p) for p in IGNORED_FILES):
            dropped.append(path)
    assert not dropped, f"direct_code_deploy would leave these out of the runtime: {dropped}"


def test_terraform_standards_path():
    import terraform_checks
    assert terraform_checks.STANDARDS.exists() and "terraform" not in terraform_checks.STANDARDS.parent.name
