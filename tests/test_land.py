"""gland (gland.land) against real git repos in tmp dirs."""
import subprocess

import pytest

from gland import land as gland


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(cwd, name, text, message):
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A main checkout on main, and a linked worktree on branch feat with one commit; cwd is the worktree."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GLAND_TEST", raising=False)
    main, wt = tmp_path / "repo", tmp_path / "wt"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    commit(main, "a.txt", "a\n", "first")
    git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
    commit(wt, "b.txt", "b\n", "feature")
    commit(main, "c.txt", "c\n", "main moved on")
    monkeypatch.chdir(wt)
    return main, wt


def branches(main):
    return git(main, "branch", "--format=%(refname:short)").split()


def test_happy_path_lands_with_no_ff_and_cleans_up(repo, tmp_path):
    main, wt = repo
    assert gland.main(["-y", "--test", "true"]) == gland.EXIT_OK
    assert git(main, "log", "-1", "--format=%s") == "Merge feat"
    assert len(git(main, "log", "-1", "--format=%P").split()) == 2  # a real merge commit
    assert (main / "b.txt").is_file() and (main / "c.txt").is_file()
    assert not wt.exists() and branches(main) == ["main"]
    # the branch got "Merge main into feat" first
    assert "Merge main into feat" in git(main, "log", "--format=%s")


def test_trailers_and_message_in_the_merge_commit(repo):
    main, _ = repo
    assert gland.main(["feat", "-y", "--test", "", "-m", "Land feat",
                       "--trailer", "Co-Authored-By: A <a@example.com>", "--trailer", "Refs: #1"]) == 0
    body = git(main, "log", "-1", "--format=%B")
    assert body.startswith("Land feat\n\n")
    assert git(main, "log", "-1", "--format=%(trailers:key=Co-Authored-By,valueonly)") == "A <a@example.com>"
    assert "Refs: #1" in body


def test_dirty_worktree_is_refused(repo):
    main, wt = repo
    (wt / "untracked.txt").write_text("x")
    before = git(main, "rev-parse", "HEAD")
    assert gland.main(["-y", "--test", "true"]) == gland.EXIT_DIRTY
    assert git(main, "rev-parse", "HEAD") == before and wt.exists()


def test_gitignored_files_do_not_count(repo):
    main, wt = repo
    commit(wt, ".gitignore", "*.log\n", "ignore logs")
    (wt / "x.log").write_text("x")
    assert gland.main(["-y", "--test", "true", "--keep"]) == gland.EXIT_OK


def test_failing_tests_stop_before_landing(repo):
    main, wt = repo
    before = git(main, "rev-parse", "HEAD")
    assert gland.main(["-y", "--test", "false"]) == gland.EXIT_TESTS
    assert git(main, "rev-parse", "HEAD") == before
    assert wt.exists() and "feat" in branches(main)


def test_conflict_stops(repo):
    main, wt = repo
    commit(wt, "c.txt", "branch version\n", "conflicting")
    before = git(main, "rev-parse", "HEAD")
    assert gland.main(["-y", "--test", "true"]) == gland.EXIT_CONFLICT
    assert git(main, "rev-parse", "HEAD") == before
    assert "c.txt" in git(wt, "diff", "--name-only", "--diff-filter=U")


def test_keep_keeps_worktree_and_branch(repo):
    main, wt = repo
    assert gland.main(["-y", "--test", "true", "--keep"]) == gland.EXIT_OK
    assert git(main, "log", "-1", "--format=%s") == "Merge feat"
    assert wt.exists() and "feat" in branches(main)


def test_dry_run_changes_nothing(repo, capsys):
    main, wt = repo
    before = (git(main, "rev-parse", "HEAD"), git(wt, "rev-parse", "HEAD"))
    assert gland.main([str(wt), "--dry-run", "--test", "true"]) == gland.EXIT_OK
    assert (git(main, "rev-parse", "HEAD"), git(wt, "rev-parse", "HEAD")) == before
    assert wt.exists() and "feat" in branches(main)
    assert "merge --no-ff feat" in capsys.readouterr().out


def test_failing_tests_after_landing_keep_everything(repo, capsys):
    main, wt = repo
    # passes in the worktree, fails in the main checkout
    assert gland.main(["-y", "--test", f"test \"$PWD\" != {main.resolve()}"]) == gland.EXIT_TESTS_AFTER
    assert git(main, "log", "-1", "--format=%s") == "Merge feat"
    assert wt.exists() and "reset --hard ORIG_HEAD" in capsys.readouterr().out


def test_test_command_from_env_and_pyproject(repo, monkeypatch):
    _, wt = repo
    (wt / "pyproject.toml").write_text('[project]\nname = "x"\n\n[tool.gland]\ntest = "uv run pytest -q"\n')
    assert gland.test_command(None, wt) == "uv run pytest -q"
    monkeypatch.setenv("GLAND_TEST", "make test")
    assert gland.test_command(None, wt) == "make test"
    assert gland.test_command("", wt) is None


def test_main_checkout_and_no_tty(repo, monkeypatch):
    main, _ = repo
    monkeypatch.chdir(main)
    assert gland.main(["-y"]) == gland.EXIT_ERROR  # not in a linked worktree
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert gland.main(["feat", "--test", "true"]) == gland.EXIT_ABORTED  # no -y, no TTY
    assert gland.main(["nope", "-y"]) == gland.EXIT_ERROR
