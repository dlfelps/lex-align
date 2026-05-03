"""`.lexalign.toml` reader/writer.

Schema:

  project    = "lex-align"
  server_url = "http://127.0.0.1:8765"
  mode       = "single-user"      # or "org"
  fail_open  = true               # ignored unless server is unreachable
  api_key_env_var = "LEXALIGN_API_KEY"  # only used when mode = "org"
  auto_request_approval = true    # PROVISIONALLY_ALLOWED → enqueue review
                                  # automatically. Defaults to true in
                                  # single-user mode, false in org mode.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tomli_w


CONFIG_FILENAME = ".lexalign.toml"


@dataclass
class ClientConfig:
    project: str
    server_url: str = "http://127.0.0.1:8765"
    mode: str = "single-user"
    fail_open: bool = True
    api_key_env_var: str = "LEXALIGN_API_KEY"
    # When the PreToolUse hook sees a PROVISIONALLY_ALLOWED verdict it
    # auto-fires `request-approval` so the user-as-reviewer flow doesn't
    # require a second manual command. Off by default in org mode where
    # the rationale should be reviewer-authored, not auto-generated.
    auto_request_approval: bool = True

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "server_url": self.server_url,
            "mode": self.mode,
            "fail_open": self.fail_open,
            "api_key_env_var": self.api_key_env_var,
            "auto_request_approval": self.auto_request_approval,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ClientConfig":
        mode = str(d.get("mode", "single-user"))
        # Org mode defaults to manual approval; single-user defaults to auto.
        auto_default = mode != "org"
        return cls(
            project=str(d["project"]),
            server_url=str(d.get("server_url", "http://127.0.0.1:8765")),
            mode=mode,
            fail_open=bool(d.get("fail_open", True)),
            api_key_env_var=str(d.get("api_key_env_var", "LEXALIGN_API_KEY")),
            auto_request_approval=bool(d.get("auto_request_approval", auto_default)),
        )


def config_path(project_root: Path) -> Path:
    return project_root / CONFIG_FILENAME


def load_config(project_root: Path) -> Optional[ClientConfig]:
    path = config_path(project_root)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None
    return ClientConfig.from_dict(data)


def save_config(project_root: Path, config: ClientConfig) -> Path:
    path = config_path(project_root)
    path.write_bytes(tomli_w.dumps(config.to_dict()).encode("utf-8"))
    return path


def find_project_root(start: Optional[Path] = None) -> Path:
    """Walk up looking for `.lexalign.toml`. Falls back to cwd."""
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / CONFIG_FILENAME).exists():
            return parent
    return cwd
