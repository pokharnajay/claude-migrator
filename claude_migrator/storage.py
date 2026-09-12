"""Discovery of Claude desktop's on-disk account storage.

Layout this module understands:

    <app support>/config.json                       -> lastKnownAccountUuid
    <app support>/claude-code-sessions/<account>/<org>/local_*.json
    <app support>/local-agent-mode-sessions/<account>/<org>/local_*.json

Both session trees are keyed by account UUID, which is why signing in to a
different account makes existing history disappear from the sidebar: the data
is still on disk, just under the previous account's directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Claude"
CLI_HOME = Path.home() / ".claude"

CODE_TREE = "claude-code-sessions"
AGENT_TREE = "local-agent-mode-sessions"
SESSION_TREES = (CODE_TREE, AGENT_TREE)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_uuid(name: str) -> bool:
    return bool(UUID_RE.match(name))


@dataclass(frozen=True)
class Org:
    """One organization/workspace directory inside an account."""

    uuid: str
    account_uuid: str
    code_dir: Path | None
    agent_dir: Path | None
    code_sessions: int
    agent_sessions: int
    first_activity: float | None
    last_activity: float | None

    @property
    def total_sessions(self) -> int:
        return self.code_sessions + self.agent_sessions

    def dir_for(self, tree: str) -> Path | None:
        return self.code_dir if tree == CODE_TREE else self.agent_dir


@dataclass(frozen=True)
class Account:
    uuid: str
    orgs: list[Org] = field(default_factory=list)
    is_current: bool = False

    @property
    def total_sessions(self) -> int:
        return sum(o.total_sessions for o in self.orgs)

    @property
    def last_activity(self) -> float | None:
        stamps = [o.last_activity for o in self.orgs if o.last_activity is not None]
        return max(stamps) if stamps else None

    @property
    def short(self) -> str:
        return self.uuid.split("-")[0]


def current_account_uuid(root: Path = APP_SUPPORT) -> str | None:
    """The account the desktop app is signed in to, per its own config."""
    config = root / "config.json"
    try:
        value = json.loads(config.read_text()).get("lastKnownAccountUuid")
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and is_uuid(value) else None


def _session_files(org_dir: Path | None) -> list[Path]:
    if org_dir is None or not org_dir.is_dir():
        return []
    return [p for p in org_dir.glob("local_*.json") if p.is_file()]


def _account_dirs(root: Path, tree: str) -> dict[str, Path]:
    base = root / tree
    if not base.is_dir():
        return {}
    # `local-agent-mode-sessions` also holds non-account siblings such as
    # `skills-plugin`, so only UUID-named directories count.
    return {p.name: p for p in base.iterdir() if p.is_dir() and is_uuid(p.name)}


def _build_org(account_uuid: str, org_uuid: str, code_dir: Path | None, agent_dir: Path | None) -> Org:
    code_files = _session_files(code_dir)
    agent_files = _session_files(agent_dir)
    stamps = [p.stat().st_mtime for p in code_files + agent_files]
    return Org(
        uuid=org_uuid,
        account_uuid=account_uuid,
        code_dir=code_dir,
        agent_dir=agent_dir,
        code_sessions=len(code_files),
        agent_sessions=len(agent_files),
        first_activity=min(stamps) if stamps else None,
        last_activity=max(stamps) if stamps else None,
    )


def find_account(root: Path, account_uuid: str) -> Account:
    """Assemble one account from whichever session trees contain it."""
    code_root = root / CODE_TREE / account_uuid
    agent_root = root / AGENT_TREE / account_uuid

    org_uuids: set[str] = set()
    for base in (code_root, agent_root):
        if base.is_dir():
            org_uuids |= {p.name for p in base.iterdir() if p.is_dir() and is_uuid(p.name)}

    orgs = []
    for org_uuid in sorted(org_uuids):
        code_dir = code_root / org_uuid
        agent_dir = agent_root / org_uuid
        orgs.append(
            _build_org(
                account_uuid,
                org_uuid,
                code_dir if code_dir.is_dir() else None,
                agent_dir if agent_dir.is_dir() else None,
            )
        )

    orgs.sort(key=lambda o: (o.last_activity or 0, o.total_sessions), reverse=True)
    return Account(
        uuid=account_uuid,
        orgs=orgs,
        is_current=account_uuid == current_account_uuid(root),
    )


def discover_accounts(root: Path = APP_SUPPORT) -> list[Account]:
    """Every account with storage on this machine, current account first."""
    uuids: set[str] = set()
    for tree in SESSION_TREES:
        uuids |= set(_account_dirs(root, tree))

    accounts = [find_account(root, uuid) for uuid in uuids]
    accounts.sort(key=lambda a: (a.is_current, a.last_activity or 0), reverse=True)
    return accounts


def active_org(account: Account) -> Org | None:
    """The org the app is actually writing to — the most recently touched one."""
    return account.orgs[0] if account.orgs else None
