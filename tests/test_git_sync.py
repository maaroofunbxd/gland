"""gland/git: the pre-push hook only checks, git-sync does the rebase (README.md). Throwaway repos: an upstream,
a fork (origin) and a clone with both remotes, the hook installed through core.hooksPath."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

KIT_GIT = Path(__file__).resolve().parents[1] / "src" / "gland" / "git"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").symlink_to(KIT_GIT / "pre-push")
    gc = tmp_path / "gitconfig"
    gc.write_text(f"[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_PRIMARY_BRANCH", "GIT_PRIMARY_REMOTE", "GIT_PUSH_HOOK_SKIP"):
        monkeypatch.delenv(var, raising=False)
    git(tmp_path, "init", "-q", "--bare", "up.git")
    git(tmp_path, "init", "-q", "--bare", "fork.git")
    git(tmp_path, "clone", "-q", "up.git", "seed")
    commit(tmp_path / "seed", "A")
    git(tmp_path / "seed", "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", "fork.git", "me")
    me = tmp_path / "me"
    git(me, "remote", "add", "upstream", "../up.git")
    git(me, "fetch", "-q", "upstream")
    git(me, "checkout", "-q", "-B", "main", "upstream/main")
    git(me, "push", "-q", "origin", "main")
    return tmp_path


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def commit(cwd, name, content=None):
    (Path(cwd) / name).write_text(content or name)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", name)


def upstream_moves(root, name):
    commit(root / "seed", name)
    git(root / "seed", "push", "-q", "origin", "main")


def sync(cwd):
    return subprocess.run(["bash", str(KIT_GIT / "git-sync")], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ})


def subjects(root, ref, git_dir="fork.git"):
    return git(root, f"--git-dir={git_dir}", "log", "--format=%s", ref).stdout.split()


def test_push_is_refused_when_behind_and_nothing_is_sent(repos):
    me = repos / "me"
    git(me, "checkout", "-q", "-b", "feat")
    commit(me, "F")
    assert git(me, "push", "-q", "origin", "feat", check=False).returncode == 0  # up to date: goes through
    upstream_moves(repos, "B")
    git(me, "tag", "t1")
    assert git(me, "push", "-q", "origin", "t1", check=False).returncode == 0  # tags aren't checked
    commit(me, "F2")
    pushed = git(me, "push", "origin", "feat", check=False)
    assert pushed.returncode != 0 and "feat is behind upstream/main" in pushed.stderr and "git sync" in pushed.stderr
    assert subjects(repos, "feat") == ["F", "A"]  # the remote didn't get F2
    assert git(me, "push", "-q", "origin", "--delete", "feat", check=False).returncode == 0  # deletes aren't checked


def test_sync_from_a_worktree_keeps_local_commits_out_and_the_dirty_tree(repos):
    me = repos / "me"
    git(me, "checkout", "-q", "-b", "feat")
    commit(me, "F")
    git(me, "push", "-q", "origin", "feat")
    git(me, "checkout", "-q", "main")
    commit(me, "M-local")  # unpushed, on a main checked out in this (other) worktree
    git(me, "worktree", "add", "-q", "../wt", "feat")
    wt = repos / "wt"
    upstream_moves(repos, "B")
    (wt / "F").write_text("uncommitted")
    out = sync(wt)
    assert out.returncode == 0, out.stderr
    assert git(wt, "log", "--format=%s").stdout.split() == ["F", "B", "A"]  # M-local isn't in it
    assert (wt / "F").read_text() == "uncommitted"
    assert "local main has 1 commit(s) not on upstream/main" in out.stderr
    assert "git push --force-with-lease origin feat" in out.stderr
    assert subjects(repos, "main") == ["B", "A"]  # the fork's main fast-forwarded
    git(wt, "stash", "-q")
    assert git(wt, "push", "-q", "--force-with-lease", "origin", "feat", check=False).returncode == 0
    assert subjects(repos, "feat") == ["F", "B", "A"]


def test_sync_fast_forwards_a_local_main_nobody_has_checked_out(repos):
    me = repos / "me"
    git(me, "checkout", "-q", "-b", "feat")
    upstream_moves(repos, "C")
    out = sync(me)
    assert out.returncode == 0, out.stderr
    assert git(me, "log", "--format=%s", "-1", "main").stdout.strip() == "C"


def test_sync_stops_on_a_conflict(repos):
    me = repos / "me"
    git(me, "checkout", "-q", "-b", "feat")
    commit(me, "f", "mine")
    commit(repos / "seed", "f", "theirs")
    git(repos / "seed", "push", "-q", "origin", "main")
    out = sync(me)
    assert out.returncode == 1 and "rebase stopped on a conflict" in out.stderr
    again = sync(me)
    assert again.returncode == 1 and "a rebase is in progress" in again.stderr
