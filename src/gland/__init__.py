"""gland: land a git worktree's branch onto the main checkout (`gland`, gland/land.py), and two bash scripts that keep
the branches you push on top of the upstream primary branch: a pre-push hook that only checks, and `git sync` that
does the rebase (gland/git/). land.py is one stdlib-only file that imports nothing else from the package, so it can
be copied anywhere and run as a script."""

from pathlib import Path

__version__ = "0.1.0"

GIT_DIR = Path(__file__).resolve().parent / "git"
"""Where pre-push and git-sync are installed: link the global hook and ~/.local/bin/git-sync here (README.md)."""
