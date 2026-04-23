from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SessionState:
    """Ephemeral per-session state persisted between hook invocations."""

    def __init__(self, sessions_dir: Path, session_id: str):
        self.sessions_dir = sessions_dir
        self.session_id = session_id
        self._state_file = sessions_dir / f".state-{session_id}.json"
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except Exception:
                pass
        return {"dep_changes_pending": [], "observed_prompts_fired": [], "propose_called_for": []}

    def _save(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self._data, indent=2))

    def record_dep_change(self, packages: list[str]) -> None:
        for pkg in packages:
            if pkg not in self._data["dep_changes_pending"]:
                self._data["dep_changes_pending"].append(pkg)
        self._save()

    def record_propose_called(self, packages: Optional[list[str]] = None) -> None:
        self._data["propose_called_for"].extend(packages or [])
        self._save()

    def record_observed_prompt(self, adr_id: str) -> None:
        if adr_id not in self._data["observed_prompts_fired"]:
            self._data["observed_prompts_fired"].append(adr_id)
        self._save()

    def has_observed_prompt_fired(self, adr_id: str) -> bool:
        return adr_id in self._data["observed_prompts_fired"]

    def unresolved_dep_changes(self) -> list[str]:
        pending = set(self._data["dep_changes_pending"])
        covered = set(self._data["propose_called_for"])
        return sorted(pending - covered)

    def cleanup(self) -> None:
        if self._state_file.exists():
            self._state_file.unlink()


class EventLogger:
    """Writes JSONL event log for a session."""

    VOLUNTARY = {"show", "plan", "history", "check-constraint", "propose", "promote"}
    AUTOMATED = {"session-start", "pre-tool-use", "post-tool-use", "session-end", "reconciliation"}

    def __init__(self, sessions_dir: Path, session_id: str):
        self.sessions_dir = sessions_dir
        self.session_id = session_id
        self._log_file = sessions_dir / f"{session_id}.jsonl"

    def log(self, event_type: str, command: str, targets: Optional[list[str]] = None) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "session_id": self.session_id,
            "event_type": event_type,
            "command": command,
            "targets": targets or [],
        }
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def log_voluntary(self, command: str, targets: Optional[list[str]] = None) -> None:
        self.log("voluntary", command, targets)

    def log_automated(self, command: str, targets: Optional[list[str]] = None) -> None:
        self.log("automated", command, targets)


def get_current_session_id(sessions_dir: Path) -> Optional[str]:
    current_file = sessions_dir / ".current-session"
    if current_file.exists():
        return current_file.read_text().strip() or None
    return None


def set_current_session_id(sessions_dir: Path, session_id: str) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / ".current-session").write_text(session_id)


def clear_current_session(sessions_dir: Path) -> None:
    current_file = sessions_dir / ".current-session"
    if current_file.exists():
        current_file.unlink()
