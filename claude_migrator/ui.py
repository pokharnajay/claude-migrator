"""The window.

Two jobs, one screen:

* Restore on this Mac — back up, then merge a previous account's sessions into
  the signed-in one. Step by step with the primary button, or in one go with
  Merge.
* Move to another Mac — export a zip here, import it there.

The primary button walks the user through the steps in order and refuses to
skip one; anything that writes is gated behind a verified backup and a quit
Claude.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from functools import partial
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import backup, identity, portable, safety, storage, sync
from .identity import Identity
from .safety import SafetyError
from .storage import Account, Org

STEP_BLOCKED, STEP_BACKUP, STEP_SYNC, STEP_DONE = range(4)

# How long the scan spinner stays up even when the work finishes sooner. Below
# roughly this, a rescan reads as a button that did nothing at all.
SCAN_MINIMUM_SECONDS = 2.0

ACCENT = "#c96442"


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def human_date(stamp: float | None) -> str:
    return datetime.fromtimestamp(stamp).strftime("%d %b %Y") if stamp else "—"


CLAUDE_BINARY_SUFFIX = "Claude.app/Contents/MacOS/Claude"


def claude_is_running() -> bool:
    """True when the desktop app currently has the session files open.

    Matched on the executable path reported by `ps`: `pgrep -x Claude` never
    matches it, and `pgrep -f` on the same path silently fails to. Requiring the
    path to *end* with the binary name excludes `Claude Helper` and the CLI.
    """
    result = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    return any(
        line.strip().endswith(CLAUDE_BINARY_SUFFIX) for line in result.stdout.splitlines()
    )


# --------------------------------------------------------------------------- work


class Worker(QObject):
    progressed = Signal(int, int, str)
    logged = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, job) -> None:
        super().__init__()
        self._job = job

    def log(self, line: str = "") -> None:
        """Jobs call `log()` with no argument for a blank line.

        `Signal(str).emit` cannot be handed to them directly: it rejects a
        zero-argument call, and the resulting TypeError surfaced as the whole
        job having failed.
        """
        self.logged.emit(line)

    def run(self) -> None:
        try:
            self.finished.emit(True, self._job(self.progressed.emit, self.log))
        except Exception as exc:  # reported in the log rather than crashing the app
            self.finished.emit(False, str(exc))


# --------------------------------------------------------------------------- widgets


class Spinner(QWidget):
    """A small indeterminate progress indicator.

    Qt ships no spinner widget, and a progress bar is the wrong shape for a
    table row, so this paints a rotating arc.
    """

    def __init__(self, diameter: int = 14, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._diameter = diameter
        self._angle = 0
        self.setFixedSize(diameter + 2, diameter + 2)
        self._timer = QTimer(self)
        self._timer.setInterval(1000 // 30)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.show()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _advance(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(Qt.NoBrush)

        track = QColor(ACCENT)
        track.setAlpha(55)
        inset = 1.0
        box = QRectF(inset, inset, self._diameter, self._diameter)

        painter.setPen(QPen(track, 2.0, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(box, 0, 360 * 16)

        painter.setPen(QPen(QColor(ACCENT), 2.0, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(box, -self._angle * 16, 100 * 16)


class Card(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 15, 18, 16)
        outer.setSpacing(10)

        heading = QLabel(title.upper())
        heading.setObjectName("cardTitle")
        outer.addWidget(heading)

        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body)


def describe_workspace(org: Org) -> str:
    """The same one-line summary for every account, signed-in or not."""
    if org.total_sessions == 0:
        return "no sessions"
    return (
        f"{org.code_sessions} code · {org.agent_sessions} cowork"
        f"   {human_date(org.first_activity)} – {human_date(org.last_activity)}"
    )


class AccountRow(QWidget):
    """One account workspace, named by whoever owns it.

    The signed-in account is shown with exactly the same detail as the ones it
    can pull from, so the scan says what you already have as well as what you
    stand to gain.
    """

    def __init__(
        self, account: Account, org: Org, who: Identity, selectable: bool = True
    ) -> None:
        super().__init__()
        self.account = account
        self.org = org
        self.who = who

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(10)

        self.checkbox = QCheckBox() if selectable else None
        if self.checkbox is not None:
            self.checkbox.setChecked(True)
            row.addWidget(self.checkbox)
        else:
            spacer = QLabel()
            spacer.setFixedWidth(18)
            row.addWidget(spacer)

        name = QLabel(f"<b>{who.label}</b>")
        name.setTextFormat(Qt.RichText)
        name.setToolTip(f"Account {account.uuid}\nWorkspace {org.uuid}")
        row.addWidget(name)

        if len(account.orgs) > 1:
            workspace = QLabel(f"workspace {org.uuid.split('-')[0]}")
            workspace.setObjectName("muted")
            row.addWidget(workspace)

        row.addStretch(1)

        self.spinner = Spinner()
        self.spinner.hide()
        row.addWidget(self.spinner)

        self.detail = QLabel(describe_workspace(org))
        self.detail.setObjectName("muted")
        row.addWidget(self.detail)

    def set_loading(self, loading: bool) -> None:
        """Swap the session counts for a spinner while they are being recounted."""
        if loading:
            self.detail.setText("scanning…")
            self.spinner.start()
        else:
            self.detail.setText(describe_workspace(self.org))
            self.spinner.stop()

    @property
    def selected(self) -> bool:
        return self.checkbox is not None and self.checkbox.isChecked()


# --------------------------------------------------------------------------- window


@dataclass
class Scan:
    target: Account
    target_org: Org
    who: Identity
    sources: list[tuple[Account, Org, Identity]]


class MigratorWindow(QWidget):
    def __init__(self, app_support: Path, cli_home: Path) -> None:
        super().__init__()
        self.app_support = app_support
        self.cli_home = cli_home
        self.scan: Scan | None = None
        self.source_rows: list[AccountRow] = []
        self.target_rows: list[AccountRow] = []
        self.backup_path: Path | None = None
        self.backup_verified = False
        self.step = STEP_BLOCKED
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._next_step = STEP_BACKUP
        self._on_done: object | None = None
        self._result = None
        self._post_scan_note: str | None = None
        # Runs once the current job's thread has been released, so it is free to
        # start another job. Dropped if the job fails.
        self._then: object | None = None

        self.setWindowTitle("Claude Migrator")
        self.setMinimumSize(800, 720)
        self._build()
        self._apply_style()
        self.do_scan()

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        root.setSpacing(14)

        title = QLabel("Claude Migrator")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Restores Claude conversation history that disappears after signing in to a "
            "different account, and moves it between Macs."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.banner_box = QWidget()
        self.banner_box.setObjectName("banner")
        banner_row = QHBoxLayout(self.banner_box)
        banner_row.setContentsMargins(14, 10, 12, 10)
        banner_row.setSpacing(12)
        self.banner = QLabel()
        self.banner.setObjectName("bannerText")
        self.banner.setWordWrap(True)
        banner_row.addWidget(self.banner, 1)
        self.quit_claude_button = QPushButton("Quit Claude")
        self.quit_claude_button.clicked.connect(self._quit_claude)
        banner_row.addWidget(self.quit_claude_button)
        self.banner_box.hide()
        root.addWidget(self.banner_box)

        accounts = Card("Accounts")
        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        accounts.body.addWidget(self.target_label)

        self.target_box = QVBoxLayout()
        self.target_box.setSpacing(0)
        accounts.body.addLayout(self.target_box)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        accounts.body.addWidget(divider)

        self.sources_label = QLabel("Restore history from")
        self.sources_label.setObjectName("muted")
        accounts.body.addWidget(self.sources_label)

        self.sources_box = QVBoxLayout()
        self.sources_box.setSpacing(0)
        accounts.body.addLayout(self.sources_box)

        self.scanning_row = QWidget()
        scanning_layout = QHBoxLayout(self.scanning_row)
        scanning_layout.setContentsMargins(0, 3, 0, 3)
        scanning_layout.setSpacing(10)
        self.scanning_spinner = Spinner()
        scanning_layout.addWidget(self.scanning_spinner)
        scanning_label = QLabel("Scanning this Mac…")
        scanning_label.setObjectName("muted")
        scanning_layout.addWidget(scanning_label)
        scanning_layout.addStretch(1)
        self.scanning_row.hide()
        accounts.body.addWidget(self.scanning_row)
        root.addWidget(accounts)

        backup_card = Card("Backup")
        self.transcripts = QCheckBox("Include CLI transcripts (~/.claude/projects)")
        self.transcripts.setChecked(True)
        self.transcripts.stateChanged.connect(self._refresh_backup_label)
        backup_card.body.addWidget(self.transcripts)
        self.backup_label = QLabel()
        self.backup_label.setObjectName("muted")
        self.backup_label.setWordWrap(True)
        backup_card.body.addWidget(self.backup_label)
        root.addWidget(backup_card)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("log")
        self.log.setFont(QFont("Menlo", 11))
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.log, 1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        root.addWidget(self.progress)

        transfer = QHBoxLayout()
        transfer.setSpacing(10)
        transfer_label = QLabel("Another Mac")
        transfer_label.setObjectName("muted")
        transfer.addWidget(transfer_label)
        self.export_button = QPushButton("Export Bundle…")
        self.export_button.setToolTip(
            "Write the selected history to a zip you can carry to another Mac."
        )
        self.export_button.clicked.connect(self._run_export)
        transfer.addWidget(self.export_button)
        self.import_button = QPushButton("Import Bundle…")
        self.import_button.setToolTip(
            "Read a zip exported from another Mac into this account."
        )
        self.import_button.clicked.connect(self._run_import)
        transfer.addWidget(self.import_button)
        transfer.addStretch(1)
        root.addLayout(transfer)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.status = QLabel()
        self.status.setObjectName("muted")
        buttons.addWidget(self.status)
        buttons.addStretch(1)

        self.merge_button = QPushButton("Merge")
        self.merge_button.setToolTip(
            "Rescan, back up, then merge the ticked accounts into the signed-in one"
        )
        self.merge_button.clicked.connect(self._run_merge)
        buttons.addWidget(self.merge_button)

        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.setToolTip("Recount sessions for every account on this Mac")
        self.rescan_button.clicked.connect(self.do_scan)
        buttons.addWidget(self.rescan_button)

        self.reveal_button = QPushButton("Show in Finder")
        self.reveal_button.clicked.connect(self._reveal)
        self.reveal_button.hide()
        buttons.addWidget(self.reveal_button)

        self.action_button = QPushButton("Back Up")
        self.action_button.setObjectName("primary")
        self.action_button.setDefault(True)
        self.action_button.clicked.connect(self._advance)
        buttons.addWidget(self.action_button)
        root.addLayout(buttons)

    def _apply_style(self) -> None:
        dark = self.palette().color(QPalette.Window).lightness() < 128
        surface = "#1c1c1e" if dark else "#ffffff"
        border = "#323235" if dark else "#e3e3e6"
        muted = "#8e8e93" if dark else "#6e6e73"
        log_bg = "#141416" if dark else "#fafafa"
        # Buttons need more contrast than the cards do, or a disabled one reads
        # as a line of plain text rather than a control that is switched off.
        button_bg = "#2c2c2f" if dark else "#ffffff"
        button_border = "#48484d" if dark else "#c8c6c2"
        # A disabled button has to be unmistakably recessed. Made too close to
        # the enabled fill, it invites a click that does nothing at all.
        button_off_bg = "#191919" if dark else "#f4f3f1"
        button_off_border = "#2a2a2d" if dark else "#e4e2de"
        button_off_text = "#5c5a58" if dark else "#b0ada9"

        self.setStyleSheet(f"""
            QWidget {{
                font-family: '.AppleSystemUIFont', 'Helvetica Neue', Helvetica;
                font-size: 13px;
            }}
            #title {{ font-size: 21px; font-weight: 600; }}
            #muted {{ color: {muted}; }}
            #cardTitle {{
                font-size: 10px; font-weight: 700; color: {muted}; letter-spacing: 1.1px;
            }}
            #card {{
                background: {surface}; border: 1px solid {border}; border-radius: 10px;
            }}
            #divider {{ background: {border}; border: none; }}
            #banner {{
                background: rgba(201,100,66,0.12);
                border: 1px solid rgba(201,100,66,0.35);
                border-radius: 8px;
            }}
            #bannerText {{ color: {ACCENT}; }}
            #log {{
                background: {log_bg}; border: 1px solid {border};
                border-radius: 10px; padding: 10px; color: {muted};
            }}
            QPushButton {{
                background: {button_bg};
                border: 1px solid {button_border};
                border-radius: 7px;
                padding: 7px 16px;
                font-weight: 500;
            }}
            QPushButton:hover {{ border-color: {ACCENT}; }}
            QPushButton:pressed {{ background: {button_off_bg}; }}
            QPushButton#primary {{
                background: {ACCENT}; border: 1px solid {ACCENT};
                color: white; font-weight: 600; padding: 7px 22px;
            }}
            QPushButton#primary:hover {{ background: #b5573a; border-color: #b5573a; }}
            QPushButton:disabled, QPushButton#primary:disabled {{
                color: {button_off_text};
                background: {button_off_bg};
                border: 1px solid {button_off_border};
                font-weight: 400;
            }}
            QPushButton:disabled:hover {{ border-color: {button_off_border}; }}
            QProgressBar {{ background: {border}; border: none; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
        """)

    # -- logging -----------------------------------------------------------

    def say(self, line: str = "") -> None:
        self.log.append(line)
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- scan --------------------------------------------------------------

    def do_scan(self) -> None:
        """Recount everything, off the GUI thread so the spinner can turn."""
        if self._busy():
            return

        for row in self.source_rows + self.target_rows:
            row.set_loading(True)
        if not (self.source_rows or self.target_rows):
            self.scanning_row.show()
            self.scanning_spinner.start()

        app_support, cli_home = self.app_support, self.cli_home
        started = time.monotonic()

        def job(progress, log):
            data = (
                storage.current_account_uuid(app_support),
                storage.discover_accounts(app_support),
                identity.resolve_identities(app_support, cli_home),
            )
            # A scan that finishes instantly looks like a button that did
            # nothing, so let the spinner be seen.
            remaining = SCAN_MINIMUM_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            self._result = data
            return "Scan complete"

        self._start(job, "Scanning…", self.step, on_done=self._apply_scan)

    def _apply_scan(self, data) -> None:
        current, accounts, people = data

        # Ticks survive a rescan. Re-ticking an account the user had unticked
        # would quietly put it back into the next merge.
        unticked = {
            (row.account.uuid, row.org.uuid) for row in self.source_rows if not row.selected
        }

        self.scanning_spinner.stop()
        self.scanning_row.hide()
        self.log.clear()
        self.scan = None
        self.backup_path = None
        self.backup_verified = False
        self.reveal_button.hide()
        for row in self.source_rows + self.target_rows:
            row.setParent(None)
        self.source_rows.clear()
        self.target_rows.clear()
        self.sources_label.setText("Restore history from")

        if not accounts:
            self._halt("No Claude storage found on this Mac.")
            return
        if current is None:
            self._halt("Could not read the signed-in account from Claude's config.json.")
            return

        target = next((a for a in accounts if a.uuid == current), None)
        target_org = storage.active_org(target) if target else None
        if target is None or target_org is None:
            self._halt("The signed-in account has no workspace yet. Open Claude once, then rescan.")
            return

        who = identity.describe(current, people)
        sources = [
            (account, org, identity.describe(account.uuid, people))
            for account in accounts
            if account.uuid != current
            for org in account.orgs
            if org.total_sessions > 0  # empty workspaces are a sign-in artefact
        ]
        self.scan = Scan(target=target, target_org=target_org, who=who, sources=sources)

        self.target_label.setText("Signed in as")
        self.target_label.setObjectName("muted")
        for org in target.orgs:
            row = AccountRow(target, org, who, selectable=False)
            self.target_rows.append(row)
            self.target_box.addWidget(row)

        for account, org, person in sources:
            row = AccountRow(account, org, person)
            if (account.uuid, org.uuid) in unticked:
                row.checkbox.setChecked(False)
            self.source_rows.append(row)
            self.sources_box.addWidget(row)

        self.say(f"Scanned at     : {datetime.now().strftime('%H:%M:%S')}")
        self.say(f"Signed in as   : {who.label}")
        if who.org_name:
            self.say(f"Organization   : {who.org_name}")
        self.say()
        self.say("This account already holds")
        for org in target.orgs:
            marker = "→" if org.uuid == target_org.uuid else " "
            self.say(f"  {marker} {org.uuid[:8]}  {describe_workspace(org)}")
        latest = sync.read_sessions(target_org, trees=(storage.CODE_TREE,))
        if latest:
            newest = max(
                (p for p in (target_org.code_dir.glob("local_*.json") if target_org.code_dir else [])),
                key=lambda p: p.stat().st_mtime,
                default=None,
            )
            if newest is not None:
                title = next(
                    (r.title for r in latest if r.session_file == newest.name), "Untitled"
                )
                when = datetime.fromtimestamp(newest.stat().st_mtime).strftime("%d %b %Y %H:%M")
                self.say(f"    most recent: {title}  ({when})")
        self.say()
        self.say(f"Other accounts : {len({a.uuid for a, _, _ in sources})}")
        self.say()

        if not sources:
            self.sources_label.setText("No other accounts on this Mac.")
            self.say("Nothing to restore locally. You can still import a bundle from another Mac.")
        else:
            total = sum(org.total_sessions for _, org, _ in sources)
            self.say(f"{total} session(s) available to restore.")

        note, self._post_scan_note = self._post_scan_note, None
        if note:
            self.say()
            self.say(note)

        self._refresh_backup_label()
        self._check_claude_running()

    def _check_claude_running(self) -> bool:
        """Writing is gated on Claude being closed. Returns True when blocked."""
        running = claude_is_running()
        if running:
            self.banner.setText(
                "<b>Quit Claude to continue.</b> Every button below is disabled while it "
                "is running — it holds these session files open and rewrites its own "
                "index as it goes."
            )
            self.banner_box.show()
            self._set_step(STEP_BLOCKED)
        else:
            self.banner_box.hide()
            self._set_step(STEP_BACKUP if self.scan else STEP_BLOCKED)
        return running

    def _halt(self, message: str) -> None:
        self.say(message)
        self.status.setText(message)
        self._set_step(STEP_BLOCKED)

    # -- shared helpers ----------------------------------------------------

    def _selected_orgs(self) -> list[Org]:
        return [row.org for row in self.source_rows if row.selected]

    def _refresh_backup_label(self) -> None:
        items = backup.backup_items(self.app_support, self.cli_home, self.transcripts.isChecked())
        self.backup_label.setText(
            f"{len(items)} item(s), {human_bytes(backup.total_size(items))} "
            "→ ~/Downloads/claude-backup-<timestamp>\n"
            "Cloned on APFS: near-instant, and almost no extra disk until something changes."
        )

    def _busy(self) -> bool:
        return self._thread is not None

    def _guard(self, needs_sources: bool = True) -> bool:
        """Common preconditions. Returns True when it is safe to proceed."""
        if self._busy():
            return False
        if self.scan is None:
            self.say("Nothing scanned yet — press Rescan.")
            return False
        if claude_is_running():
            self._check_claude_running()
            self.say("Claude is still running. Quit it first.")
            return False
        if needs_sources and not self._selected_orgs():
            self.say("Select at least one account first.")
            return False
        return True

    # -- back up & restore -------------------------------------------------

    def _advance(self) -> None:
        if self.step == STEP_BACKUP:
            self._run_backup()
        elif self.step == STEP_SYNC:
            self._run_sync()

    def _run_backup(self) -> None:
        if not self._guard():
            return
        items = backup.backup_items(self.app_support, self.cli_home, self.transcripts.isChecked())
        account = self.scan.target.uuid

        def job(progress, log):
            self._back_up(items, account, progress, log)
            return "Backup verified"

        self._start(job, "Backing up…", STEP_SYNC)

    def _run_sync(self) -> None:
        if not self._guard():
            return
        if not self.backup_verified:
            self.say("Refusing to restore without a verified backup. Run Back Up first.")
            return

        try:
            plan = sync.plan_sync(
                self.app_support, self.scan.target, self.scan.target_org, self._selected_orgs()
            )
        except (ValueError, SafetyError) as exc:
            self.say(str(exc))
            return

        if not plan.to_copy and not plan.archived_additions:
            self.say("Nothing to restore — every session is already in this account.")
            self._set_step(STEP_DONE)
            return

        target_uuid = self.scan.target.uuid

        def job(progress, log):
            return f"Restored {self._restore(plan, target_uuid, progress, log)} item(s)"

        self._start(job, "Restoring…", STEP_DONE)

    def _back_up(self, items, account: str, progress, log) -> None:
        """Take a backup and verify it, or raise. Runs in the worker thread."""
        log("Backing up…")
        outcome = backup.run_backup_verified(
            backup.DOWNLOADS, items, progress=progress, current_account=account
        )
        self.backup_path = outcome.path
        self.backup_verified = outcome.verified
        log(f"Written to {outcome.path}")
        log()
        for line in outcome.findings:
            log("  " + line)
        log()
        if not outcome.verified:
            raise SafetyError(
                "Backup could not be verified — refusing to go further. "
                "Nothing has been changed."
            )
        log("Backup verified.")

    def _restore(self, plan: sync.SyncPlan, target_uuid: str, progress, log) -> int:
        """Carry out a sync plan and report on it. Runs in the worker thread.

        Returns how many items were copied; raises if any of them failed.
        """
        log()
        log(f"Restoring {plan.session_count} session(s), {human_bytes(plan.total_bytes)}…")
        result = sync.execute(plan, progress=progress)
        log(f"Copied           : {result.copied}")
        log(f"Already present  : {len(plan.already_present) + result.skipped_existing}")
        if result.archived_merged:
            log(f"Archive flags    : {result.archived_merged} merged")
        for path, error in result.failed:
            log(f"FAILED {path.name}: {error}")

        org = storage.active_org(storage.find_account(self.app_support, target_uuid))
        missing = sync.missing_transcripts(org, self.cli_home / "projects") if org else []
        if missing:
            log()
            log(f"{len(missing)} session(s) will open empty — transcript no longer on disk:")
            for ref in missing[:12]:
                log(f"  · {ref.title}")
            if len(missing) > 12:
                log(f"  … and {len(missing) - 12} more")
        log()
        log("Open Claude to see the restored sessions.")
        if result.failed:
            raise SafetyError(f"{len(result.failed)} item(s) failed — see above.")
        return result.copied

    # -- merge in one go ---------------------------------------------------

    def _run_merge(self) -> None:
        """Rescan, back up, then merge the ticked accounts: all three steps at once.

        The rescan comes first so the merge works from what is on disk now, not
        from counts taken whenever the window last looked.
        """
        if not self._guard():
            return
        chosen = {(row.account.uuid, row.org.uuid) for row in self.source_rows if row.selected}
        self._then = partial(self._merge_scanned, chosen)
        self.do_scan()

    def _merge_scanned(self, chosen: set[tuple[str, str]]) -> None:
        """Second half of Merge, once the fresh scan has landed."""
        if self.scan is None:  # the scan halted, and has already said why
            return
        if not self._guard(needs_sources=False):
            return

        rows = [
            row
            for row in self.source_rows
            if row.selected and (row.account.uuid, row.org.uuid) in chosen
        ]
        if not rows:
            self.say("None of the ticked accounts can be merged from any more. Nothing was merged.")
            return

        try:
            plan = sync.plan_sync(
                self.app_support, self.scan.target, self.scan.target_org, [row.org for row in rows]
            )
        except (ValueError, SafetyError) as exc:
            self.say(str(exc))
            return

        if not plan.to_copy and not plan.archived_additions:
            self.say("Nothing to merge — every session is already in this account.")
            self.status.setText("Nothing to merge")
            return

        sources = ", ".join(dict.fromkeys(row.who.label for row in rows))
        confirm = QMessageBox.question(
            self,
            "Merge these sessions?",
            f"From: {sources}\n"
            f"Into: {self.scan.who.label}\n"
            f"Sessions: {plan.session_count}   Size: {human_bytes(plan.total_bytes)}\n\n"
            "A verified backup is taken first. Nothing already in this account is "
            "overwritten.",
        )
        if confirm != QMessageBox.Yes:
            self.say("Merge cancelled. Nothing was changed.")
            return

        items = backup.backup_items(self.app_support, self.cli_home, self.transcripts.isChecked())
        target_uuid = self.scan.target.uuid

        def job(progress, log):
            self._back_up(items, target_uuid, progress, log)
            return f"Merged {self._restore(plan, target_uuid, progress, log)} item(s)"

        self._start(job, "Merging…", STEP_DONE)

    # -- transfer between Macs ---------------------------------------------

    def _run_export(self) -> None:
        if not self._guard():
            return
        orgs = self._selected_orgs()
        label = next(
            (row.who.label for row in self.source_rows if row.selected),
            self.scan.who.label,
        )
        projects = self.cli_home / "projects"

        def job(progress, log):
            log()
            log("Building bundle…")
            summary = portable.export_bundle(
                backup.DOWNLOADS, orgs, projects, label, progress=progress
            )
            self.backup_path = summary.path
            log(f"Bundle    : {summary.path}")
            log(f"Sessions  : {summary.sessions}")
            log(f"Transcripts: {summary.transcripts}")
            if summary.missing_transcripts:
                log()
                log(
                    f"{len(summary.missing_transcripts)} session(s) have no transcript on "
                    "this Mac and will open empty on the other one:"
                )
                for title in summary.missing_transcripts[:10]:
                    log(f"  · {title}")
            log()
            log("Copy this zip to the other Mac, open Claude Migrator there,")
            log("and press Import Bundle.")
            return "Bundle exported"

        self._start(job, "Exporting…", self.step)

    def _run_import(self) -> None:
        if not self._guard(needs_sources=False):
            return

        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a bundle exported from another Mac", str(backup.DOWNLOADS), "Zip archives (*.zip)"
        )
        if not chosen:
            return
        bundle = Path(chosen)

        try:
            info = portable.inspect_bundle(bundle)
        except SafetyError as exc:
            self.say(str(exc))
            QMessageBox.critical(self, "Bundle rejected", str(exc))
            return

        confirm = QMessageBox.question(
            self,
            "Import this bundle?",
            f"{bundle.name}\n\n"
            f"From: {info.label}\n"
            f"Exported: {info.exported_at[:19].replace('T', ' ')}\n"
            f"Sessions: {info.sessions}   Transcripts: {info.transcripts}\n\n"
            f"They will be merged into {self.scan.who.label}. "
            "Nothing already on this Mac is overwritten.",
        )
        if confirm != QMessageBox.Yes:
            return

        app_support = self.app_support
        cli_home = self.cli_home
        account = self.scan.target.uuid
        org = self.scan.target_org.uuid

        def job(progress, log):
            log()
            log(f"Importing {bundle.name}…")
            result = portable.import_bundle(
                bundle, app_support, cli_home, account, org, progress=progress
            )
            log(f"Sessions copied    : {result.sessions_copied}")
            log(f"Transcripts copied : {result.transcripts_copied}")
            log(f"Already present    : {result.skipped_existing}")
            for name, error in result.failed:
                log(f"FAILED {name}: {error}")
            if result.unknown_cwds:
                log()
                log("These sessions point at folders that do not exist on this Mac:")
                for cwd in result.unknown_cwds[:10]:
                    log(f"  · {cwd}")
                log("They will open, but Claude will not find the project files.")
            log()
            log("Open Claude to see the imported sessions.")
            if result.failed:
                raise SafetyError(f"{len(result.failed)} item(s) failed — see above.")
            return f"Imported {result.sessions_copied} session(s)"

        self._start(job, "Importing…", self.step)

    # -- threading ---------------------------------------------------------

    def _start(self, job, status: str, next_step: int, on_done=None) -> None:
        self._on_done = on_done
        self._set_controls_enabled(False)
        self.status.setText(status)
        self.progress.setRange(0, 0)
        self.progress.show()
        self._next_step = next_step

        self._thread = QThread()
        self._worker = Worker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        # Every receiver below must be a bound method of a QObject living in the
        # GUI thread. Connecting to a plain callable (a lambda) gives Qt no
        # receiver to attribute, so it runs the slot directly in the worker
        # thread — which crashed the app: the slot called wait() on the very
        # thread it was running in, and the QThread was then destroyed while
        # still running.
        self._worker.logged.connect(self.say)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)

        # Shutdown belongs to the thread itself, never to a slot that might be
        # executing inside it.
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_thread_finished(self) -> None:
        """Runs in the GUI thread once the worker thread has actually stopped."""
        thread, self._thread, self._worker = self._thread, None, None
        if thread is not None:
            thread.deleteLater()
        self._set_step(self.step)

        then, self._then = self._then, None
        if then is not None:
            then()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.action_button,
            self.merge_button,
            self.rescan_button,
            self.export_button,
            self.import_button,
            self.transcripts,
        ):
            widget.setEnabled(enabled)

    def _on_progress(self, index: int, total: int, label: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self.status.setText(f"{index}/{total} · {label}")

    def _on_finished(self, ok: bool, message: str) -> None:
        next_step = self._next_step
        handler, self._on_done = self._on_done, None
        if ok and handler is not None:
            # Widgets can only be built here, on the GUI thread.
            handler(self._result)
            self.status.setText(message)
            self._set_controls_enabled(True)
            self.progress.hide()
            return

        self.progress.hide()
        self._set_controls_enabled(True)

        if self.backup_path is not None:
            self.reveal_button.show()

        if not ok:
            self._then = None
            self.say(f"Stopped: {message}")
            self.status.setText("Stopped")
            self._set_step(self.step)  # stay where we were; nothing advanced
            return

        self.status.setText(message)
        self._set_step(next_step)

    def _set_step(self, step: int) -> None:
        self.step = step
        labels = {
            STEP_BLOCKED: ("Back Up", False),
            STEP_BACKUP: ("Back Up", True),
            STEP_SYNC: ("Restore Sessions", True),
            STEP_DONE: ("Done", False),
        }
        text, enabled = labels[step]
        self.action_button.setText(text)

        has_sources = bool(self.source_rows)
        if step == STEP_SYNC and not has_sources:
            enabled = False
        self.action_button.setEnabled(enabled and not self._busy())

        blocked = step == STEP_BLOCKED
        self.export_button.setEnabled(has_sources and not blocked and not self._busy())
        self.merge_button.setEnabled(has_sources and not blocked and not self._busy())
        self.import_button.setEnabled(self.scan is not None and not blocked and not self._busy())

    def _quit_claude(self) -> None:
        """Ask Claude to quit, the same as Cmd-Q, then rescan.

        A graceful quit request, never a kill: anything Claude has in flight
        gets to finish and save.
        """
        self.say("Asking Claude to quit…")
        subprocess.run(
            ["osascript", "-e", 'tell application "Claude" to quit'],
            capture_output=True,
            text=True,
        )
        for _ in range(20):
            if not claude_is_running():
                # The scan clears the log when it lands, so the note has to be
                # handed to it rather than printed now.
                self._post_scan_note = "Claude has quit. Ready to go."
                self.do_scan()
                return
            time.sleep(0.25)
        self.say("Claude is still running — quit it from its own window, then press Rescan.")

    def closeEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        """Let a running job finish before the window and its thread go away.

        Closing mid-job would otherwise destroy a QThread that is still
        running, which aborts the process rather than quitting it.
        """
        thread = self._thread
        if thread is not None and thread.isRunning():
            self.status.setText("Finishing up…")
            thread.quit()
            thread.wait(10_000)
        super().closeEvent(event)

    def _reveal(self) -> None:
        if self.backup_path:
            subprocess.run(["open", "-R", str(self.backup_path)])


def main() -> int:
    app = QApplication([])
    app.setApplicationName("Claude Migrator")
    app.setApplicationDisplayName("Claude Migrator")

    lock = safety.SingleInstance(Path.home() / "Library" / "Caches" / "claude-migrator.lock")
    try:
        lock.__enter__()
    except SafetyError as exc:
        QMessageBox.warning(None, "Claude Migrator", str(exc))
        return 1

    try:
        window = MigratorWindow(storage.APP_SUPPORT, storage.CLI_HOME)
        window.show()
        return app.exec()
    finally:
        lock.__exit__()
