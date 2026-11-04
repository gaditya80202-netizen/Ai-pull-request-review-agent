from __future__ import annotations

from pathlib import PurePosixPath

ALLOWED_EXTENSIONS = {
    ".py", ".pyi",
    ".md", ".mdx", ".txt", ".rst",
    ".json", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".properties",
    ".js", ".jsx", ".ts", ".tsx",
    ".html", ".css", ".scss",
    ".sql", ".sh",
    ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp",
    ".xml", ".gradle",
}

ALLOWED_EXTENSIONLESS_NAMES = {
    "readme",
    "dockerfile",
    "makefile",
    "justfile",
}

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "coverage",
    "__pycache__",
    ".next",
}

IGNORED_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "cargo.lock",
    "gemfile.lock",
    "secrets.json",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}

MAX_FILE_BYTES = 256_000


def should_index_file(
    path: str,
    size: int | None,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> bool:
    """Return whether a repository path is eligible for code indexing."""
    if not path or path.startswith("/") or "\\" in path:
        return False

    parts = path.split("/")

    if any(part in {"", ".", ".."} for part in parts):
        return False

    candidate = PurePosixPath(path)
    name = candidate.name.lower()

    if any(part.lower() in IGNORED_DIRECTORIES for part in parts[:-1]):
        return False

    # Exclude dotenv files, private keys, certificates, and common
    # credential-related filenames.
    if name.startswith(".env"):
        return False

    if name in IGNORED_FILENAMES:
        return False

    if any(
        name.endswith(suffix)
        for suffix in (
            ".pem", ".key", ".p12", ".pfx", ".keystore",
            ".lock",
        )
    ):
        return False

    if name in ALLOWED_EXTENSIONLESS_NAMES:
        supported = True
    else:
        supported = candidate.suffix.lower() in ALLOWED_EXTENSIONS

    if not supported:
        return False

    return size is None or 0 <= size <= max_file_bytes
