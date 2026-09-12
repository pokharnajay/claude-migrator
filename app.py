#!/usr/bin/env python3
"""Claude Migrator — start here.

    cd claude-migrator
    python3 app.py

The window needs PySide6, which is not part of the standard library. Rather
than making that your problem, the first run creates a `.venv` beside this file,
installs PySide6 into it, and restarts itself inside it. Every run after that
starts straight away.

Nothing is installed into your system Python.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENV_PYTHON = VENV / "bin" / "python3"
REQUIREMENT = "pyside6-essentials>=6.6"
MINIMUM_PYTHON = (3, 10)

# Set on the child process so a failure to import PySide6 there reports a real
# problem instead of bootstrapping in an endless loop.
RELAUNCH_FLAG = "CLAUDE_MIGRATOR_RELAUNCHED"


def fail(message: str) -> None:
    print(f"\n  {message}\n", file=sys.stderr)
    raise SystemExit(1)


def check_platform() -> None:
    if sys.platform != "darwin":
        fail("Claude Migrator only works on macOS — it reads macOS-specific paths.")
    if sys.version_info < MINIMUM_PYTHON:
        have = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in MINIMUM_PYTHON)
        fail(f"Python {want} or newer is required. This is Python {have}.")


def have_pyside() -> bool:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def create_venv() -> None:
    print("First run — setting up. This takes a minute and happens only once.", flush=True)
    print(f"  Creating {VENV.name}…", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV)], capture_output=True, text=True
    )
    if result.returncode != 0:
        fail(f"Could not create the virtual environment:\n  {result.stderr.strip()}")

    print("  Installing PySide6…", flush=True)
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", "pip", REQUIREMENT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            "Could not install PySide6:\n  "
            + (result.stderr.strip() or result.stdout.strip())
            + f"\n\n  You can install it yourself with:\n"
            f"    {VENV_PYTHON} -m pip install {REQUIREMENT}"
        )
    print("  Done.\n", flush=True)


def relaunch_in_venv() -> None:
    """Replace this process with the one inside the virtual environment.

    execve does not flush Python's buffers, so anything still sitting in them
    would be lost when the process image is replaced.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    environment = dict(os.environ, **{RELAUNCH_FLAG: "1"})
    os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve())], environment)


def main() -> int:
    check_platform()

    if not have_pyside():
        if os.environ.get(RELAUNCH_FLAG):
            fail(
                "PySide6 still is not importable after installing it.\n"
                f"  Try deleting {VENV} and running this again."
            )
        if not VENV_PYTHON.exists():
            create_venv()
        relaunch_in_venv()  # does not return

    from claude_migrator.ui import main as run_window

    return run_window()


if __name__ == "__main__":
    raise SystemExit(main())
