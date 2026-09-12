# Claude Migrator

Restores Claude desktop conversation history that disappears after signing in
to a different account, and moves it between Macs.

## Why history vanishes

Both session stores are keyed by account UUID:

```
~/Library/Application Support/Claude/
  config.json                                        -> lastKnownAccountUuid
  claude-code-sessions/<account>/<org>/local_*.json
  local-agent-mode-sessions/<account>/<org>/local_*.json
```

Signing in to a new account makes the app read a different `<account>`
directory. Nothing is deleted — the old sessions are simply no longer looked
at. This tool copies them into the signed-in account's directory.

CLI transcripts in `~/.claude/projects` are *not* account-scoped, so they
survive an account switch untouched. They are, however, where the actual
conversation text lives, which is why the transfer bundle carries them too.

## Restore on this Mac

Double-click **Claude Migrator.app**, then:

1. **Quit Claude.** Every write is blocked while it is running — it holds these
   files open and rewrites its own index.
2. **Back Up** clones everything to `~/Downloads/claude-backup-<timestamp>/`
   and verifies the copy file-by-file. Restore stays locked until it passes.
3. **Restore Sessions** merges the other account's sessions into the signed-in
   one.

Accounts are shown by e-mail, resolved from `~/.claude.json`, from the CLI's
own config backups, and — for accounts no config still mentions — from the
desktop app's local stores.

## Move to another Mac

On the Mac that has the history:

1. Select the account, press **Export Bundle…**
2. A zip lands in `~/Downloads` containing the sessions, their CLI transcripts,
   and a SHA-256 for every file.

On the other Mac: open Claude Migrator, press **Import Bundle…**, pick the zip.

Sessions whose working directory does not exist on the second Mac are reported
after the import — they open, but Claude will not find the project files.

## Safety

An imported bundle is untrusted input and is validated before anything is
extracted. It is rejected outright for: absolute paths, `..` traversal,
symlink members, unexpected top-level directories, non-UUID workspace
segments, file names that are not sessions or transcripts, a missing or
unreadable manifest, an unknown bundle version, implausible compression
ratios, or more than 200,000 members / 20 GB uncompressed. Every member is
checksummed against the manifest after extraction and discarded on mismatch.

For local writes:

- **Nothing is ever overwritten or deleted.** Files are created with `O_EXCL`,
  so the check and the write are one atomic step and a file the desktop app
  writes concurrently is left exactly as the app wrote it.
- **Directories land whole.** Cowork session folders are built in a staging
  directory and moved into place, so a reader never sees a half-populated one.
- **The archive index is merged, never replaced** — a set union written
  atomically via a temporary file and a rename.
- **Backups are verified** before the restore step unlocks, comparing file
  counts and byte totals per item.
- **Free space is checked** before any copy begins.
- **Paths are contained** — a symlinked or oddly named entry that resolves
  outside the directory being migrated is refused.
- **One instance at a time**, enforced by a PID lock that reclaims itself after
  a crash.
- Re-running any operation is a no-op.

Account state that belongs to the machine rather than the history
(`scheduled-tasks.json`, `rpm/`, `cowork-*-cache.json`, `debug/`) is
deliberately left behind.

## Development

The `.app` is a standalone PyInstaller bundle — native arm64, ~64 MB, with its
own Python and a trimmed Qt (Core/Gui/Widgets only). It does not depend on
system Python.

```bash
python3 -m venv .venv-slim
.venv-slim/bin/pip install pyside6-essentials pyinstaller pytest

QT_QPA_PLATFORM=offscreen .venv-slim/bin/python -m pytest -q   # 105 tests
./build-app.sh                                                 # test, build, install
```

| Module | Responsibility |
|---|---|
| `storage.py` | Finds accounts, workspaces and the signed-in account |
| `identity.py` | Resolves an account UUID to an e-mail and name |
| `safety.py` | Every write; containment, space, verification, locking |
| `backup.py` | APFS-cloned snapshot into `~/Downloads`, then verifies it |
| `sync.py` | Plans and executes a same-machine merge |
| `portable.py` | Export to a zip; validate and import one |
| `app.py` | The window |

Tests never touch real Claude data — they build a synthetic storage tree in
`tmp_path`, including the hostile archives the importer must refuse.
