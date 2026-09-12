"""Builds a fake Claude storage tree so tests never touch real data."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Synthetic identifiers. Nothing here corresponds to a real account.
OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
OLD_ORG = "33333333-3333-4333-8333-333333333333"
NEW_ORG = "44444444-4444-4444-8444-444444444444"


def write_session(path: Path, session_id: str, cli_session_id: str, title: str) -> None:
    path.write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "cliSessionId": cli_session_id,
                "cwd": "/tmp/project",
                "title": title,
                "isArchived": False,
            }
        )
    )


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    """A two-account tree mirroring the real layout: old account rich, new account bare.

    Returns the Application Support root; the sibling CLI home is `cli_root`.
    """
    app = tmp_path / "Claude"
    code = app / "claude-code-sessions"
    agent = app / "local-agent-mode-sessions"

    old_code = code / OLD / OLD_ORG
    old_code.mkdir(parents=True)
    for i in range(3):
        write_session(
            old_code / f"local_old{i}.json", f"old{i}", f"cli-old{i}", f"Old session {i}"
        )
    (old_code / "archived-sessions.idx").write_text(
        json.dumps({"v": 1, "archived": ["local_old0"]})
    )
    # Account-scoped state that must NOT be migrated.
    (old_code / "scheduled-tasks.json").write_text("{}")

    old_agent = agent / OLD / OLD_ORG
    old_agent.mkdir(parents=True)
    write_session(old_agent / "local_cw0.json", "cw0", "cli-cw0", "Cowork session")
    (old_agent / "local_cw0").mkdir()
    (old_agent / "local_cw0" / "blob.bin").write_bytes(b"payload")
    (old_agent / "artifacts.json").write_text(json.dumps({"artifacts": []}))
    (old_agent / "cowork-gb-cache.json").write_text("{}")
    (old_agent / "rpm").mkdir()

    new_code = code / NEW / NEW_ORG
    new_code.mkdir(parents=True)
    write_session(
        new_code / "local_current.json", "current", "cli-current", "Current session"
    )
    (agent / NEW / NEW_ORG).mkdir(parents=True)

    # A non-account sibling directory the scanner must ignore.
    (agent / "skills-plugin" / NEW_ORG).mkdir(parents=True)

    (app / "config.json").write_text(json.dumps({"lastKnownAccountUuid": NEW}))

    return app


@pytest.fixture
def cli_root(storage_root: Path) -> Path:
    """The `~/.claude` counterpart holding CLI transcripts. `cli-old2` is absent on purpose."""
    cli = storage_root.parent / "dot-claude"
    projects = cli / "projects" / "-tmp-project"
    projects.mkdir(parents=True)
    for name in ("cli-old0", "cli-old1", "cli-current", "cli-cw0"):
        (projects / f"{name}.jsonl").write_text('{"type":"user"}\n')
    (cli / "settings.json").write_text("{}")
    (cli / "projects" / "-tmp-project" / "notes.txt").write_text("ignored")
    return cli
