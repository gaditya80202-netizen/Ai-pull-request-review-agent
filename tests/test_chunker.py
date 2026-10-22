import pytest

from backend.memory.chunker import split_text_into_chunks
from backend.memory.file_filter import should_index_file
from backend.security.masking import mask_secrets


def test_small_text_creates_one_chunk():
    chunks = split_text_into_chunks(
        "example.py",
        "def calculate():\n    return 42\n",
    )

    assert len(chunks) == 1
    assert chunks[0].path == "example.py"
    assert chunks[0].symbol == "calculate"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2
    assert "# File: example.py" in chunks[0].content


def test_large_text_uses_overlapping_chunks():
    text = "\n".join(f"line_{i}" for i in range(1, 101))

    chunks = split_text_into_chunks(
        "example.py",
        text,
        chunk_lines=60,
        overlap_lines=10,
    )

    assert len(chunks) == 2
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 60
    assert chunks[1].start_line == 51
    assert chunks[1].end_line == 100


def test_very_long_lines_are_split_without_truncation():
    text = "\n".join(["a" * 1500] * 5)

    chunks = split_text_into_chunks(
        "example.txt",
        text,
        chunk_lines=5,
        overlap_lines=1,
        max_chunk_chars=2000,
    )

    recovered = "\n".join(
        line
        for chunk in chunks
        for line in chunk.content.splitlines()
        if not line.startswith("# File:")
        and not line.startswith("# Lines:")
    )

    assert all(len(chunk.content) <= 2000 for chunk in chunks)
    assert "a" * 1500 in recovered


def test_oversized_single_line_is_rejected():
    with pytest.raises(ValueError, match="exceeds the chunk-size limit"):
        split_text_into_chunks(
            "example.txt",
            "x" * 10_000,
            max_chunk_chars=200,
        )


def test_secret_masking_preserves_line_count():
    text = (
        'API_KEY = "very-secret-value-12345"\n'
        "safe_value = 1\n"
    )

    masked = mask_secrets(text)

    assert "very-secret-value-12345" not in masked
    assert masked.count("\n") == text.count("\n")
    assert "safe_value = 1" in masked


def test_private_key_content_is_masked():
    text = (
        "before\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "private-material-here\n"
        "-----END PRIVATE KEY-----\n"
        "after\n"
    )

    masked = mask_secrets(text)

    assert "private-material-here" not in masked
    assert "before" in masked
    assert "after" in masked
    assert masked.count("\n") == text.count("\n")


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.example",
        "config/private.pem",
        "credentials.json",
        "vendor/library.py",
        "node_modules/package/index.js",
        "package-lock.json",
        "../outside.py",
        "/absolute/path.py",
    ],
)
def test_sensitive_or_generated_paths_are_excluded(path):
    assert should_index_file(path, 100) is False


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "backend/main.py",
        ".github/workflows/tests.yml",
        "scripts/migrations/init.sql",
        "requirements.txt",
    ],
)
def test_normal_text_files_are_eligible(path):
    assert should_index_file(path, 100) is True


def test_large_file_is_excluded():
    assert should_index_file("large.py", 300_000) is False


def test_unknown_file_type_is_excluded():
    assert should_index_file("image.png", 100) is False
