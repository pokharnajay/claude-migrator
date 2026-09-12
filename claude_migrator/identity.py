"""Puts a human name on an account UUID.

Claude stores no single account directory, so identities are recovered from
three places, most trustworthy first:

1. `~/.claude.json` -> `oauthAccount`, which describes the signed-in account.
2. `~/.claude/backups/.claude.json.backup.*`, which can still describe accounts
   that were signed in earlier.
3. The desktop app's Electron stores, where a profile payload containing the
   e-mail sits near the account UUID it belongs to. This is the only source
   that recovers a *previous* account, so it is worth the binary scan — but it
   is a scrape of an undocumented format, so it is consulted last and never
   allowed to overwrite a value from a structured source.

Nothing here writes, and nothing leaves the machine.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# Every quantifier here is bounded, and no two adjacent tokens can match the
# same character. An earlier version used `[A-Za-z0-9.-]+\.` for the domain —
# because the class contained a dot, the engine could split a long run of dots
# and letters in exponentially many ways and would hang for minutes on a blob
# of binary data that never matched at all.
EMAIL_PATTERN = re.compile(
    rb"[A-Za-z0-9._%+\-]{1,64}@(?:[A-Za-z0-9\-]{1,63}\.){1,8}[A-Za-z]{2,24}"
)

# Addresses that belong to tooling bundled with the app, not to the user.
EMAIL_NOISE = ("sentry", "example.com", "noreply", "@types", "localhost")

# How far either side of an e-mail to look for the UUID it describes. The
# profile blob that pairs them is a few hundred bytes wide.
SCAN_WINDOW = 400

# Electron stores holding profile payloads. Files above the cap are log/segment
# files far too large to be a profile record, and scanning them wastes seconds.
ELECTRON_STORES = ("Local Storage", "IndexedDB", "WebStorage", "Partitions")
MAX_SCAN_BYTES = 64 * 1024 * 1024

# Compiled-script and asset caches live under these stores but never hold a
# profile payload, and they are the bulk of the bytes. Skipping them turns a
# multi-second scan into a fraction of one.
SKIP_DIRECTORIES = ("Cache", "Code Cache", "GPUCache", "CacheStorage", "ScriptCache", "blob_storage")

# A name on an account is a nicety; a window that never opens is not. If the
# scan cannot finish in this long, whatever is left falls back to a short UUID.
SCAN_BUDGET_SECONDS = 5.0


@dataclass(frozen=True)
class Identity:
    """What we know about one account."""

    uuid: str
    email: str | None = None
    full_name: str | None = None
    org_name: str | None = None
    source: str = "unknown"

    @property
    def short(self) -> str:
        return self.uuid.split("-")[0]

    @property
    def label(self) -> str:
        """The best human-readable name available, falling back to the UUID."""
        return self.email or self.full_name or self.short

    @property
    def sublabel(self) -> str | None:
        """A secondary line, only when it adds something the label does not."""
        if self.email and self.full_name:
            return self.full_name
        return None


def _identity_from_oauth(block: dict, source: str) -> Identity | None:
    uuid = block.get("accountUuid")
    if not isinstance(uuid, str) or not UUID_PATTERN.fullmatch(uuid):
        return None
    return Identity(
        uuid=uuid,
        email=block.get("emailAddress") or None,
        full_name=block.get("displayName") or block.get("fullName") or None,
        org_name=block.get("organizationName") or None,
        source=source,
    )


def _from_claude_json(path: Path, source: str) -> Identity | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    block = data.get("oauthAccount")
    return _identity_from_oauth(block, source) if isinstance(block, dict) else None


def _from_backups(backups_dir: Path) -> list[Identity]:
    if not backups_dir.is_dir():
        return []
    found = []
    # Newest first, so an older backup never shadows a newer one.
    files = sorted(
        backups_dir.glob(".claude.json.backup.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files[:20]:
        identity = _from_claude_json(path, "backup")
        if identity:
            found.append(identity)
    return found


def _scan_blob(data: bytes) -> dict[str, Identity]:
    """Pair each e-mail with the account UUID sitting closest to it."""
    found: dict[str, Identity] = {}
    for match in EMAIL_PATTERN.finditer(data):
        email = match.group().decode("utf-8", "replace")
        if any(noise in email.lower() for noise in EMAIL_NOISE):
            continue

        start = max(0, match.start() - SCAN_WINDOW)
        end = min(len(data), match.end() + SCAN_WINDOW)
        window = data[start:end].decode("utf-8", "replace")

        uuids = UUID_PATTERN.findall(window)
        if not uuids:
            continue

        # The account UUID is the one nearest the e-mail; org UUIDs sit further
        # out in these payloads.
        offset_in_window = match.start() - start
        nearest = min(
            uuids,
            key=lambda u: abs(window.find(u) - offset_in_window),
        )

        name = None
        for key in ("full_name", "display_name", "fullName", "displayName"):
            hit = re.search(rf'"{key}"\s*[:"]\s*"?([^"\x00]{{1,64}})"', window)
            if hit:
                name = hit.group(1).strip()
                break

        found.setdefault(
            nearest,
            Identity(uuid=nearest, email=email, full_name=name, source="app-store"),
        )
    return found


def _from_electron_stores(app_support: Path) -> dict[str, Identity]:
    found: dict[str, Identity] = {}
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    for store in ELECTRON_STORES:
        base = app_support / store
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if time.monotonic() > deadline:
                return found
            if not path.is_file():
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            try:
                if path.stat().st_size > MAX_SCAN_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            for uuid, identity in _scan_blob(data).items():
                found.setdefault(uuid, identity)
    return found


def resolve_identities(app_support: Path, cli_home: Path) -> dict[str, Identity]:
    """Map account UUID -> Identity, from the most trustworthy source available."""
    identities: dict[str, Identity] = {}

    def offer(identity: Identity | None) -> None:
        if identity and identity.uuid not in identities:
            identities[identity.uuid] = identity

    offer(_from_claude_json(cli_home.parent / ".claude.json", "signed-in"))
    for found in _from_backups(cli_home / "backups"):
        offer(found)
    for found in _from_electron_stores(app_support).values():
        offer(found)

    return identities


def describe(uuid: str, identities: dict[str, Identity]) -> Identity:
    """Always returns something displayable, even for an unknown account."""
    return identities.get(uuid) or Identity(uuid=uuid, source="unresolved")
