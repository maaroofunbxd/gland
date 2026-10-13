"""Console scripts for the bash files in gland/git: `git-sync` (so `git sync` works once the package is on PATH) and
`gland-git-dir` (prints the folder, for linking the pre-push hook)."""

from __future__ import annotations

import os
import shutil
import sys

from gland import GIT_DIR


def git_sync() -> None:
    bash = shutil.which("bash")
    if not bash:
        sys.exit("git sync: needs bash")
    script = str(GIT_DIR / "git-sync")
    os.execv(bash, [bash, script, *sys.argv[1:]])


def git_dir() -> None:
    print(GIT_DIR)
