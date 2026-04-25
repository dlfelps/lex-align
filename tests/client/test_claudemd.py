"""Tests for CLAUDE.md install logic."""

from __future__ import annotations

from pathlib import Path

from lex_align_client.claudemd import _SECTION_HEADER, install_claude_md


def test_creates_new_file(tmp_path: Path):
    path, changed = install_claude_md(tmp_path)
    assert changed is True
    assert path == tmp_path / "CLAUDE.md"
    content = path.read_text(encoding="utf-8")
    assert _SECTION_HEADER in content
    assert "lex-align-client check" in content
    assert "ALLOWED" in content
    assert "DENIED" in content


def test_appends_to_existing_file(tmp_path: Path):
    existing = "# My Project\n\nSome existing docs.\n"
    (tmp_path / "CLAUDE.md").write_text(existing, encoding="utf-8")
    path, changed = install_claude_md(tmp_path)
    assert changed is True
    content = path.read_text(encoding="utf-8")
    assert content.startswith("# My Project")
    assert _SECTION_HEADER in content
    assert "Some existing docs." in content


def test_idempotent_when_section_already_present(tmp_path: Path):
    install_claude_md(tmp_path)
    first_content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    path, changed = install_claude_md(tmp_path)
    assert changed is False
    assert path.read_text(encoding="utf-8") == first_content
    assert first_content.count(_SECTION_HEADER) == 1


def test_idempotent_when_appended_section_present(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# Existing\n", encoding="utf-8")
    install_claude_md(tmp_path)
    first_content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    _, changed = install_claude_md(tmp_path)
    assert changed is False
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == first_content
