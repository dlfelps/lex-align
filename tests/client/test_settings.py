"""Hook installer tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lex_align_client.settings import (
    claude_hooks_status,
    install_claude_hooks,
    install_precommit,
    remove_claude_hooks,
    remove_precommit,
)


def test_install_and_remove_claude_hooks(tmp_path: Path):
    install_claude_hooks(tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]
    assert "SessionStart" in hooks
    assert "PreToolUse" in hooks
    assert (tmp_path / ".claude" / "lex-align-hook.py").exists()
    status = claude_hooks_status(tmp_path)
    assert status["SessionStart"] is True
    assert status["PreToolUse"] is True

    # Idempotent: second install does not duplicate entries.
    install_claude_hooks(tmp_path)
    settings_again = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings_again["hooks"]["SessionStart"] == hooks["SessionStart"]

    remove_claude_hooks(tmp_path)
    settings_after = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "SessionStart" not in settings_after.get("hooks", {})
    assert not (tmp_path / ".claude" / "lex-align-hook.py").exists()


def test_install_precommit_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    hook = install_precommit(tmp_path)
    assert hook is not None
    contents = hook.read_text()
    assert "lex-align-client precommit" in contents

    # Idempotent: re-installing does not add a second block.
    install_precommit(tmp_path)
    contents_again = hook.read_text()
    assert contents_again.count("lex-align pre-commit") == 1


def test_install_precommit_no_op_outside_git(tmp_path: Path):
    assert install_precommit(tmp_path) is None
    assert not (tmp_path / ".git").exists()


def test_install_precommit_appends_to_existing_hook(tmp_path: Path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho original\n")
    install_precommit(tmp_path)
    text = hook_path.read_text()
    assert "echo original" in text
    assert "lex-align-client precommit" in text


def test_remove_precommit_strips_the_block(tmp_path: Path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    install_precommit(tmp_path)
    remove_precommit(tmp_path)
    text = (tmp_path / ".git" / "hooks" / "pre-commit").read_text()
    assert "lex-align" not in text
