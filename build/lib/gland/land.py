#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""gland: land a branch from a linked git worktree onto the main checkout's branch, then remove the worktree.

The `gland` command of the gland package (README.md). Keep it one file, stdlib only (argcomplete optional), with
no imports even from the rest of the package, so it can be copied anywhere and run as a script.

    gland                         land the worktree you are in
    gland my-branch               land the branch checked out in some linked worktree
    gland ../repo-wt --dry-run    print the steps for that worktree, change nothing
    gland -y --trailer "Co-Authored-By: Someone <x@example.com>"

Steps (primary = the branch checked out in the main checkout, normally main):
  1. refuse if the worktree has uncommitted changes (tracked or untracked; gitignored files do not count)
  2. in the worktree: git merge <primary> -m "Merge <primary> into <branch>" (stop on conflict)
  3. run the test command in the worktree (stop if it fails)
  4. in the main checkout (refused if dirty in tracked files), after a confirmation (-y skips it; without a TTY
     -y is required): git merge --no-ff <branch> -m <message> plus the --trailer lines
  5. run the test command again in the main checkout (--no-test-after skips it); on failure say so, keep the
     worktree and the branch, and print the `git reset --hard ORIG_HEAD` hint (nothing is reverted)
  6. git worktree remove <path>; git branch -d <branch> (--keep keeps both)
Never pushes.

Test command: --test CMD, else $GLAND_TEST, else `test = "..."` under [tool.gland] in the worktree's
pyproject.toml, else none (--test "" for none). It runs through the shell, in the checkout's directory.

Exit codes: 0 landed (or --dry-run), 1 tests failed in the worktree (nothing landed), 2 error (bad args, not a
linked worktree, git error), 3 a checkout is dirty, 4 merge conflict (left in place to resolve or
`git merge --abort`), 5 landed but the tests failed on the primary branch, 6 not confirmed (declined, or no TTY
without -y), 7 landed but removing the worktree or the branch failed, 130 interrupted.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

(EXIT_OK, EXIT_TESTS, EXIT_ERROR, EXIT_DIRTY, EXIT_CONFLICT, EXIT_TESTS_AFTER, EXIT_ABORTED,
 EXIT_CLEANUP) = 0, 1, 2, 3, 4, 5, 6, 7
STEPS = 6


class Stop(Exception):
    """A step failed: message for the user, and the exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def say(message: str) -> None:
    print(f"gland: {message}", flush=True)


def step(n: int, message: str) -> None:
    say(f"[{n}/{STEPS}] {message}")


def git(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise Stop(f"git {' '.join(args)} failed in {cwd}:\n{(result.stderr or result.stdout).strip()}", EXIT_ERROR)
    return result


def worktrees(cwd: Path | str) -> list[dict[str, Any]]:
    """`git worktree list --porcelain` as dicts (path, branch or None, bare/detached flags); the first is the main one."""
    out = git(cwd, "worktree", "list", "--porcelain").stdout
    entries: list[dict[str, Any]] = []
    for block in out.strip().split("\n\n"):
        entry: dict[str, Any] = {"branch": None}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entry["path"] = Path(value)
            elif key == "branch":
                entry["branch"] = value.removeprefix("refs/heads/")
            else:
                entry[key] = value or True
        if "path" in entry:
            entries.append(entry)
    return entries


def same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def resolve(target: str | None, cwd: Path | str = ".") -> tuple[Path, Path, str, str]:
    """(main checkout, worktree path, branch, primary branch) for a branch name, a worktree path, or the cwd."""
    probe = target if target and Path(target).is_dir() else cwd
    if git(probe, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise Stop(f"not in a git repository: {Path(probe).resolve()}", EXIT_ERROR)
    entries = worktrees(probe)
    main = entries[0]
    if main.get("bare"):
        raise Stop("the main checkout is a bare repository: nothing to land onto", EXIT_ERROR)
    linked = entries[1:]
    if target and Path(target).is_dir():
        top = git(target, "rev-parse", "--show-toplevel").stdout.strip()
        found = [e for e in linked if same_path(e["path"], top)]
        what = f"worktree {top}"
    elif target:
        found = [e for e in linked if e["branch"] == target]
        what = f"branch {target!r}"
    else:
        top = git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
        found = [e for e in linked if same_path(e["path"], top)]
        what = f"the current directory ({top})"
    if not found:
        names = ", ".join(f"{e['branch'] or 'detached'} ({e['path']})" for e in linked) or "none"
        raise Stop(f"{what} is not a linked worktree with a branch; linked worktrees: {names}", EXIT_ERROR)
    wt = found[0]
    if not wt["branch"]:
        raise Stop(f"worktree {wt['path']} has a detached HEAD: check out a branch first", EXIT_ERROR)
    primary = main["branch"]
    if not primary:
        raise Stop(f"the main checkout {main['path']} has a detached HEAD: check out the branch to land onto", EXIT_ERROR)
    if primary == wt["branch"]:
        raise Stop(f"{wt['branch']} is the main checkout's own branch", EXIT_ERROR)
    return main["path"], wt["path"], wt["branch"], primary


def pyproject_test(checkout: Path) -> str | None:
    """`test` under [tool.gland] in checkout/pyproject.toml (tomllib on 3.11+, else a plain line scan)."""
    path = checkout / "pyproject.toml"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # type: ignore[import-not-found]

        value = tomllib.loads(text).get("tool", {}).get("gland", {}).get("test")
        return value if isinstance(value, str) else None
    except ImportError:
        section = re.search(r"^\[tool\.gland\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        match = section and re.search(r"""^test\s*=\s*(["'])(.*?)\1\s*$""", section.group(1), re.M)
        return match.group(2) if match else None
    except ValueError:  # tomllib.TOMLDecodeError
        return None


def test_command(arg: str | None, checkout: Path) -> str | None:
    if arg is not None:
        return arg.strip() or None
    env = os.environ.get("GLAND_TEST")
    if env is not None:
        return env.strip() or None
    return pyproject_test(checkout)


def dirty(checkout: Path, untracked: bool) -> list[str]:
    args = ["status", "--porcelain"] + ([] if untracked else ["--untracked-files=no"])
    return git(checkout, *args).stdout.splitlines()


def run_tests(command: str, checkout: Path) -> bool:
    say(f"running tests in {checkout}: {command}")
    return subprocess.run(command, shell=True, cwd=checkout, check=False).returncode == 0


def merge(checkout: Path, *args: str) -> None:
    result = git(checkout, "merge", *args, check=False)
    if result.returncode == 0:
        return
    conflicts = git(checkout, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    if conflicts:
        raise Stop(f"merge conflict in {checkout}: {', '.join(conflicts)}\n"
                   f"  resolve and commit there, then re-run gland; or: git -C {shlex.quote(str(checkout))} merge --abort",
                   EXIT_CONFLICT)
    raise Stop(f"git merge failed in {checkout}:\n{(result.stderr or result.stdout).strip()}", EXIT_ERROR)


def confirm(question: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise Stop("no TTY to confirm on: pass -y", EXIT_ABORTED)
    try:
        answer = input(f"gland: {question} [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise Stop("not confirmed: nothing merged into the primary branch", EXIT_ABORTED)


def plan(main: Path, wt: Path, branch: str, primary: str, args: argparse.Namespace, test: str | None) -> None:
    message = args.message or f"Merge {branch}"
    say(f"dry run: land {branch} ({wt}) onto {primary} ({main})")
    step(1, f"check {wt} has no uncommitted changes" + ("  [dirty now]" if dirty(wt, True) else ""))
    step(2, f"git -C {wt} merge {primary} -m 'Merge {primary} into {branch}'")
    step(3, f"tests in {wt}: {test}" if test else "no test command: skip")
    trailers = "".join(f" + trailer {t!r}" for t in args.trailer)
    step(4, f"{'' if args.yes else 'confirm, then '}git -C {main} merge --no-ff {branch} -m {message!r}{trailers}"
         + ("  [main checkout dirty now]" if dirty(main, False) else ""))
    step(5, f"tests in {main}: {test}" if test and not args.no_test_after else "no tests after the merge: skip")
    step(6, "keep the worktree and the branch (--keep)" if args.keep
         else f"git worktree remove {wt}; git branch -d {branch}")


def land(args: argparse.Namespace) -> int:
    main, wt, branch, primary = resolve(args.target)
    test = test_command(args.test, wt)
    if args.dry_run:
        plan(main, wt, branch, primary, args, test)
        return EXIT_OK
    if not args.yes and not sys.stdin.isatty():
        raise Stop("no TTY to confirm on: pass -y (or --dry-run)", EXIT_ABORTED)
    say(f"landing {branch} ({wt}) onto {primary} ({main})")

    changes = dirty(wt, True)
    if changes:
        raise Stop(f"worktree {wt} has uncommitted changes, commit or remove them first:\n  " + "\n  ".join(changes),
                   EXIT_DIRTY)
    if dirty(main, False):
        raise Stop(f"main checkout {main} has uncommitted changes to tracked files", EXIT_DIRTY)
    step(1, f"worktree {wt} is clean")

    merge(wt, primary, "-m", f"Merge {primary} into {branch}")
    step(2, f"merged {primary} into {branch} ({git(wt, 'rev-parse', '--short', 'HEAD').stdout.strip()})")

    if test:
        if not run_tests(test, wt):
            raise Stop(f"tests failed in {wt}: nothing landed", EXIT_TESTS)
        step(3, "tests passed in the worktree")
    else:
        step(3, "no test command (--test, $GLAND_TEST or [tool.gland] test): skipped")

    changes = dirty(main, False)
    if changes:
        raise Stop(f"main checkout {main} has uncommitted changes to tracked files:\n  " + "\n  ".join(changes), EXIT_DIRTY)
    confirm(f"merge {branch} into {primary} in {main}?", args.yes)
    message = args.message or f"Merge {branch}"
    trailer_args = ["-m", "\n".join(args.trailer)] if args.trailer else []
    merge(main, "--no-ff", branch, "-m", message, *trailer_args)
    step(4, f"merged {branch} into {primary} with --no-ff ({git(main, 'rev-parse', '--short', 'HEAD').stdout.strip()}); "
            "not pushed")

    if test and not args.no_test_after:
        if not run_tests(test, main):
            bar = "!" * 72
            print(f"{bar}\ngland: TESTS FAILED on {primary} after the merge. Nothing was reverted; the worktree and the "
                  f"branch are kept.\ngland: to undo the merge: git -C {shlex.quote(str(main))} reset --hard ORIG_HEAD\n{bar}",
                  flush=True)
            return EXIT_TESTS_AFTER
        step(5, f"tests passed on {primary}")
    else:
        step(5, "tests after the merge: skipped")

    if args.keep:
        step(6, f"kept worktree {wt} and branch {branch} (--keep)")
    else:
        removed = git(main, "worktree", "remove", str(wt), check=False)
        deleted = git(main, "branch", "-d", branch, check=False) if removed.returncode == 0 else removed
        if deleted.returncode != 0:
            raise Stop(f"landed, but cleanup failed: {(deleted.stderr or deleted.stdout).strip()}", EXIT_CLEANUP)
        step(6, f"removed worktree {wt} and branch {branch}")
    say(f"landed {branch} on {primary}"
        + (f" (your shell was in the removed worktree: cd {shlex.quote(str(main))})"
           if not args.keep and (os.path.realpath(os.environ.get("PWD", "")) + os.sep).startswith(os.path.realpath(wt) + os.sep)
           else ""))
    return EXIT_OK


def complete_branches(prefix: str, **_: Any) -> list[str]:
    try:
        return [e["branch"] for e in worktrees(".")[1:] if e["branch"] and e["branch"].startswith(prefix)]
    except Stop:
        return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gland",
        description="Land a linked worktree's branch: merge the primary branch into it, test, merge it --no-ff into the "
                    "main checkout, test again, remove the worktree and the branch. Never pushes. Exit 0 landed, "
                    "1 tests failed (nothing landed), 2 error, 3 dirty checkout, 4 merge conflict, 5 tests failed "
                    "after landing, 6 not confirmed, 7 cleanup failed.",
    )
    target = parser.add_argument("target", nargs="?", metavar="BRANCH|WORKTREE_PATH",
                                 help="default: the linked worktree you are in")
    parser.add_argument("--test", "-t", metavar="CMD",
                        help='test command (default: $GLAND_TEST, else [tool.gland] test in pyproject.toml; "" for none)')
    parser.add_argument("--no-test-after", action="store_true", help="skip the tests on the primary branch after the merge")
    parser.add_argument("--message", "-m", help="merge commit message (default: 'Merge <branch>')")
    parser.add_argument("--trailer", action="append", default=[], metavar="LINE",
                        help="trailer line for the merge commit, e.g. 'Co-Authored-By: Name <email>' (repeatable)")
    parser.add_argument("--keep", action="store_true", help="keep the worktree and the branch afterwards")
    parser.add_argument("--dry-run", "-n", action="store_true", help="print the steps, change nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask before merging into the primary branch")
    target.completer = complete_branches  # type: ignore[attr-defined]
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        import argcomplete

        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args(argv)
    try:
        return land(args)
    except Stop as exc:
        print(f"gland: {exc}", file=sys.stderr, flush=True)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
