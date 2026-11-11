"""Tests for sidekick/artifacts.py — bridge-written deliverables."""

from datetime import datetime
from pathlib import Path

import pytest

from sidekick.artifacts import ArtifactStore, slugify

FIXED = datetime(2026, 9, 17, 22, 5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Design a cache layer", "design-a-cache-layer"),
        ("What's the *best* API?!", "what-s-the-best-api"),
        ("Café naïve résumé", "cafe-naive-resume"),
        ("one two three four five six seven eight", "one-two-three-four-five-six"),
        ("", "untitled"),
        ("!!!", "untitled"),
        ("x" * 100, "x" * 48),
        ("def foo():\n    return 1", "def-foo-return-1"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "sidekick-artifacts", clock=lambda: FIXED)


def test_save_names_file(tmp_path: Path) -> None:
    path = _store(tmp_path).save("architect", "Design a cache", "body", model="m", session="s")
    assert path == tmp_path / "sidekick-artifacts" / "20260917-2205-architect-design-a-cache.md"


def test_save_writes_header_and_body(tmp_path: Path) -> None:
    path = _store(tmp_path).save(
        "review", "Check auth", "The review.", model="gemini-3.8-flash", session="pr-12"
    )
    text = path.read_text()
    assert text.startswith("# review — Check auth\n")
    assert "- model: gemini-3.8-flash" in text
    assert "- session: pr-12" in text
    assert "- saved: 2026-09-17 22:05" in text
    assert text.rstrip().endswith("The review.")


def test_collisions_get_numeric_suffix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    paths = [store.save("review", "same", str(i), model="m", session="s") for i in range(3)]
    assert [p.name for p in paths] == [
        "20260917-2205-review-same.md",
        "20260917-2205-review-same-2.md",
        "20260917-2205-review-same-3.md",
    ]
    assert paths[0].read_text().rstrip().endswith("0")


def test_title_uses_first_line_only(tmp_path: Path) -> None:
    path = _store(tmp_path).save("review", "line one\nline two", "b", model="m", session="s")
    assert path.read_text().startswith("# review — line one\n")
