"""Turning account UUIDs into names the user recognises."""

from __future__ import annotations

import json
from pathlib import Path

from claude_migrator import identity
from tests.conftest import NEW, OLD


def write_claude_json(path: Path, uuid: str, email: str, name: str = "Test User") -> None:
    path.write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "accountUuid": uuid,
                    "emailAddress": email,
                    "displayName": name,
                    "organizationName": f"{email}'s Organization",
                }
            }
        )
    )


def test_signed_in_account_comes_from_claude_json(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    cli.mkdir()
    write_claude_json(tmp_path / ".claude.json", NEW, "new@example.org")

    found = identity.resolve_identities(tmp_path / "Claude", cli)
    assert found[NEW].email == "new@example.org"
    assert found[NEW].source == "signed-in"


def test_previous_accounts_are_recovered_from_backups(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    (cli / "backups").mkdir(parents=True)
    write_claude_json(tmp_path / ".claude.json", NEW, "new@example.org")
    write_claude_json(cli / "backups" / ".claude.json.backup.1", OLD, "old@example.org")

    found = identity.resolve_identities(tmp_path / "Claude", cli)
    assert found[OLD].email == "old@example.org"
    assert found[OLD].source == "backup"


def test_a_structured_source_is_not_overridden_by_the_scraper(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    cli.mkdir()
    write_claude_json(tmp_path / ".claude.json", NEW, "authoritative@example.org")

    store = tmp_path / "Claude" / "Local Storage"
    store.mkdir(parents=True)
    (store / "blob.ldb").write_bytes(
        f'"uuid":"{NEW}","email_address":"stale@example.org"'.encode()
    )

    found = identity.resolve_identities(tmp_path / "Claude", cli)
    assert found[NEW].email == "authoritative@example.org"


def test_the_scraper_recovers_an_account_no_config_mentions(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    cli.mkdir()
    store = tmp_path / "Claude" / "Local Storage"
    store.mkdir(parents=True)
    (store / "blob.ldb").write_bytes(
        f'"uuid":"{OLD}","email_address":"recovered@example.org","full_name":"Old User"'.encode()
    )

    found = identity.resolve_identities(tmp_path / "Claude", cli)
    assert found[OLD].email == "recovered@example.org"
    assert found[OLD].full_name == "Old User"
    assert found[OLD].source == "app-store"


def test_the_scraper_picks_the_uuid_nearest_the_email(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    cli.mkdir()
    store = tmp_path / "Claude" / "Local Storage"
    store.mkdir(parents=True)
    # Account UUID adjacent, organization UUID further away — as in the real payload.
    (store / "blob.ldb").write_bytes(
        f'"uuid":"{OLD}","email_address":"user@example.org"'.encode()
        + b"," * 200
        + f'"organization":{{"uuid":"{NEW}"}}'.encode()
    )
    found = identity.resolve_identities(tmp_path / "Claude", cli)
    assert OLD in found and found[OLD].email == "user@example.org"


def test_tooling_addresses_are_ignored(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    cli.mkdir()
    store = tmp_path / "Claude" / "Local Storage"
    store.mkdir(parents=True)
    (store / "blob.ldb").write_bytes(
        f'"uuid":"{OLD}","contact":"noreply@sentry.io"'.encode()
    )
    assert identity.resolve_identities(tmp_path / "Claude", cli) == {}


def test_an_unresolved_account_still_displays(tmp_path: Path) -> None:
    described = identity.describe(OLD, {})
    assert described.label == OLD.split("-")[0]
    assert described.source == "unresolved"


def test_label_prefers_email_then_name(tmp_path: Path) -> None:
    assert identity.Identity(OLD, email="a@b.c", full_name="N").label == "a@b.c"
    assert identity.Identity(OLD, full_name="Only Name").label == "Only Name"
    assert identity.Identity(OLD).label == OLD.split("-")[0]


def test_sublabel_only_appears_when_it_adds_something() -> None:
    assert identity.Identity(OLD, email="a@b.c", full_name="N").sublabel == "N"
    assert identity.Identity(OLD, full_name="N").sublabel is None


def test_unreadable_sources_are_skipped_without_raising(tmp_path: Path) -> None:
    cli = tmp_path / ".claude"
    (cli / "backups").mkdir(parents=True)
    (tmp_path / ".claude.json").write_text("{ not json")
    (cli / "backups" / ".claude.json.backup.1").write_bytes(b"\x00\x01\x02")
    assert identity.resolve_identities(tmp_path / "Claude", cli) == {}
