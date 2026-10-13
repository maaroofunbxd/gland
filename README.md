# gland

Small git tools for working in worktrees and forks, for any repo:

- **`gland`** lands a branch from a linked git worktree onto the main checkout's branch, then removes the worktree:
  merge the primary branch in, test, `merge --no-ff` into the main checkout, test again, clean up. It never pushes.
- **`pre-push`** is a global git hook that only checks: each branch you push must contain the upstream primary branch
  (`dev.local`, else `main`, else `master`, on the `upstream` remote, else `origin`). If one doesn't, the push stops.
- **`git sync`** does the changes the hook asks for: fetch, fast-forward the fork's and the local primary, rebase the
  branch you're on onto `upstream/<primary>`, and print the push to run.

`gland` is one stdlib-only Python file (`src/gland/land.py`) that imports nothing else from the package, so you can
also copy it anywhere and run it as a script. `pre-push` and `git-sync` are bash scripts in `src/gland/git/`.

## Install

```bash
uv tool install "gland[completion] @ git+https://github.com/maaroofunbxd/gland"   # or: pip install git+https://...
```

That puts `gland`, `git-sync` (so `git sync` works) and `gland-git-dir` on PATH. Python 3.9 or newer, git and bash.
The `completion` extra adds argcomplete (`eval "$(register-python-argcomplete gland)"`). As a dependency:
`gland @ git+https://github.com/maaroofunbxd/gland@v0.1.1`.

The hook is a link in your global hooks folder, so it runs in every clone that doesn't set its own
`core.hooksPath`:

```bash
git config --global core.hooksPath ~/.git-templates/hooks     # once, if you have no global hooks folder
ln -sf "$(gland-git-dir)/pre-push" "$(git config --global core.hooksPath | sed "s|^~|$HOME|")/pre-push"
```

Reinstalling or upgrading the package keeps the path, so the link keeps working.

## gland

```bash
gland                         # land the worktree you are in
gland my-branch               # land the branch checked out in some linked worktree
gland ../repo-wt --dry-run    # print the steps for that worktree, change nothing
gland -y --trailer "Co-Authored-By: Someone <x@example.com>"
```

Steps (primary = the branch checked out in the main checkout, normally `main`):

1. Refuse if the worktree has uncommitted changes (tracked or untracked; gitignored files don't count).
2. In the worktree: `git merge <primary>` (stop on conflict).
3. Run the test command in the worktree (stop if it fails).
4. In the main checkout (refused if dirty in tracked files), after a confirmation (`-y` skips it; without a TTY `-y`
   is required): `git merge --no-ff <branch>` with the `--trailer` lines.
5. Run the test command again in the main checkout (`--no-test-after` skips it). On failure it says so, keeps the
   worktree and the branch, and prints the `git reset --hard ORIG_HEAD` hint; nothing is reverted.
6. `git worktree remove <path>`; `git branch -d <branch>` (`--keep` keeps both).

Test command: `--test CMD`, else `$GLAND_TEST`, else `test = "..."` under `[tool.gland]` in the worktree's
`pyproject.toml`, else none (`--test ""` for none). It runs through the shell, in the checkout's directory.

Exit codes: 0 landed (or `--dry-run`), 1 tests failed in the worktree (nothing landed), 2 error, 3 a checkout is
dirty, 4 merge conflict (left in place to resolve or `git merge --abort`), 5 landed but the tests failed on the
primary branch, 6 not confirmed, 7 landed but removing the worktree or the branch failed, 130 interrupted.

## pre-push and git sync

### Using it

```bash
git push                      # refused: "feat is behind upstream/main (3 commit(s) missing). Nothing was pushed."
git sync                      # rebases feat onto upstream/main, then prints the push to run:
git push --force-with-lease origin feat   # (only when feat was pushed before; else plain git push)
```

What `git sync` does, step by step:

1. `git fetch upstream <primary>`.
2. The fork's `origin/<primary>` is moved to `upstream/<primary>` with a fast-forward push, never forced. If the
   fork has commits upstream doesn't, it's left alone and you're told.
3. The local primary is fast-forwarded too, without checking it out (so it works from any worktree). It's left
   alone when it has commits upstream doesn't, or when it's checked out in another worktree (you're told the
   command to run there).
4. The branch you're on is rebased onto `upstream/<primary>`, never onto the local primary, so unpushed commits on
   your local main don't leak into it. `--autostash` keeps uncommitted changes; `--rebase-merges` keeps merge
   commits. On a conflict it stops and says how to continue or abort.

Overrides, for both: `GIT_PRIMARY_BRANCH`, `GIT_PRIMARY_REMOTE`. `GIT_PUSH_HOOK_VERBOSE=1 git push` traces the hook.
Agents must not bypass the hook (`--no-verify`, `GIT_PUSH_HOOK_SKIP`); when it refuses, they run `git sync`.

### Why it's split this way

Until 2026-10-12 the hook did the work itself: it merged upstream into the local primary, force-pushed
`origin/<primary>` and rebased the branch being pushed onto the local primary. That went wrong in three ways:

- **The rebase didn't reach the remote.** Git decides what it sends before the pre-push hook runs, so the remote
  got the branch as it was before the rebase while the local branch was rebased. The two differed, the next push
  was rejected, and the advice was `--force-with-lease --no-verify`.
- **It failed in worktrees.** It ran `git checkout main`, which git refuses when `main` is checked out in another
  worktree; the push died with that error and no hint.
- **It rebased onto the local primary**, so commits on a local `main` that was ahead of upstream went into every
  branch pushed, and the force-push of `origin/<primary>` was a side effect of pushing something else.

A hook that only checks can't do any of that; the changes happen in `git sync`, which you run and can see.

Tests: `tests/test_git_sync.py` (throwaway repos: refused when behind, tags and deletes pass, sync from a worktree
with uncommitted changes and unpushed local commits, the conflict stop).
