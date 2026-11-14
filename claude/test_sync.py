"""Tests for sync.py install/export behavior on existing configs.

Run: uvx pytest claude/test_sync.py
Each test builds a throwaway git repo (the dotfiles side) and a fake home
(the ~/.claude side) under tmp_path; nothing touches the real config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sync

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


class Env:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch):
        self.repo = tmp / "dots"
        self.payload = self.repo / "claude" / "home"
        self.home = tmp / "home"
        self.claude = self.home / ".claude"
        self.agents = self.home / ".agents" / "skills"
        self.payload.mkdir(parents=True)
        self.claude.mkdir(parents=True)
        self.agents.mkdir(parents=True)
        self.git("init", "-q")
        (self.payload / "CLAUDE.md").write_text("baseline")
        for name, value in {
            "REPO_DIR": self.repo / "claude",
            "PAYLOAD_DIR": self.payload,
            "MCP_FILE": self.repo / "claude" / "mcp-servers.json",
            "REMINDERS_FILE": self.repo / "claude" / "reminders.md",
            "HOME_DIR": self.home,
            "CLAUDE_HOME": self.claude,
            "CLAUDE_JSON": self.home / ".claude.json",
        }.items():
            monkeypatch.setattr(sync, name, value)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=GIT_ENV)

    def repo_file(self, rel: str, text: str) -> None:
        path = self.payload / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", "c", "--allow-empty")

    def drop(self, rel: str) -> None:
        self.git("rm", "-rq", f"claude/home/{rel}")
        self.commit()

    def local(self, rel: str) -> Path:
        return self.claude / rel

    def backups(self) -> list[Path]:
        root = self.claude / "backups"
        return [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def test_install_removes_file_the_repo_dropped(env: Env) -> None:
    env.repo_file("skills/old/SKILL.md", "v1")
    env.repo_file("skills/keep/SKILL.md", "k")
    env.commit()
    sync.install_payload()
    env.drop("skills/old")

    sync.install_payload()

    assert not env.local("skills/old").exists()
    assert env.local("skills/keep/SKILL.md").read_text() == "k"
    assert [p.read_text() for p in env.backups() if p.name == "SKILL.md"] == ["v1"]


def test_install_keeps_local_only_file(env: Env) -> None:
    env.repo_file("skills/keep/SKILL.md", "k")
    env.commit()
    env.local("skills/mine").mkdir(parents=True)
    env.local("skills/mine/SKILL.md").write_text("mine")

    sync.install_payload()

    assert env.local("skills/mine/SKILL.md").read_text() == "mine"


def test_install_keeps_dropped_file_edited_after_install(env: Env) -> None:
    env.repo_file("rules/r.md", "v1")
    env.commit()
    sync.install_payload()
    env.local("rules/r.md").write_text("v1 plus my edit")
    env.drop("rules/r.md")

    sync.install_payload()

    assert env.local("rules/r.md").read_text() == "v1 plus my edit"


def test_install_does_not_write_through_linked_skill(env: Env) -> None:
    (env.agents / "sup").mkdir()
    (env.agents / "sup" / "SKILL.md").write_text("old")
    (env.agents / "sup" / "extra.md").write_text("extra")
    env.local("skills").mkdir()
    env.local("skills/sup").symlink_to(env.agents / "sup")
    env.repo_file("skills/sup/SKILL.md", "new")
    env.commit()

    sync.install_payload()

    assert (env.agents / "sup" / "SKILL.md").read_text() == "old"
    assert not env.local("skills/sup").is_symlink()
    assert env.local("skills/sup/SKILL.md").read_text() == "new"
    assert env.local("skills/sup/extra.md").read_text() == "extra"


def test_install_survives_dangling_linked_skill(env: Env) -> None:
    env.local("skills").mkdir()
    env.local("skills/gone").symlink_to(env.agents / "missing")
    env.repo_file("skills/gone/SKILL.md", "new")
    env.commit()

    sync.install_payload()

    assert env.local("skills/gone/SKILL.md").read_text() == "new"


def test_install_removes_dropped_link_but_not_its_target(env: Env) -> None:
    env.repo_file("skills/sup/SKILL.md", "x")
    env.commit()
    env.drop("skills/sup")
    (env.agents / "sup").mkdir()
    (env.agents / "sup" / "SKILL.md").write_text("x")
    env.local("skills").mkdir()
    env.local("skills/sup").symlink_to(env.agents / "sup")

    sync.install_payload()

    assert not env.local("skills/sup").is_symlink()
    assert not env.local("skills/sup").exists()
    assert (env.agents / "sup" / "SKILL.md").read_text() == "x"


def test_install_keeps_link_holding_local_only_content(env: Env) -> None:
    env.repo_file("skills/sup/SKILL.md", "x")
    env.commit()
    env.drop("skills/sup")
    (env.agents / "sup").mkdir()
    (env.agents / "sup" / "SKILL.md").write_text("x")
    (env.agents / "sup" / "notes.md").write_text("never in repo")
    env.local("skills").mkdir()
    env.local("skills/sup").symlink_to(env.agents / "sup")

    sync.install_payload()

    assert env.local("skills/sup").is_symlink()
    assert (env.agents / "sup" / "SKILL.md").read_text() == "x"


def test_install_writes_through_stow_style_root_link(env: Env, tmp_path: Path) -> None:
    stow = tmp_path / "stow" / "skills"
    stow.mkdir(parents=True)
    env.local("skills").symlink_to(stow)
    env.repo_file("skills/a/SKILL.md", "a")
    env.commit()

    sync.install_payload()

    assert env.local("skills").is_symlink()
    assert (stow / "a" / "SKILL.md").read_text() == "a"


def test_install_merges_settings_instead_of_overwriting(env: Env) -> None:
    env.local("settings.json").write_text(
        json.dumps(
            {
                "env": {"ANTHROPIC_BASE_URL": "http://proxy", "A": "local"},
                "autoMode": {"x": 1},
                "permissions": {"allow": ["L", "R"]},
                "model": "local-model",
            }
        )
    )
    env.repo_file(
        "settings.json",
        json.dumps(
            {"env": {"A": "repo"}, "permissions": {"allow": ["R"]}, "model": "m"}
        ),
    )
    env.commit()

    sync.install_payload()

    got = json.loads(env.local("settings.json").read_text())
    assert got["env"] == {"ANTHROPIC_BASE_URL": "http://proxy", "A": "repo"}
    assert got["permissions"]["allow"] == ["R", "L"]
    assert got["autoMode"] == {"x": 1}
    assert got["model"] == "m"


def test_second_install_changes_nothing(env: Env, capsys) -> None:
    env.repo_file("skills/a/SKILL.md", "a")
    env.repo_file("settings.json", json.dumps({"model": "m"}))
    env.commit()
    env.local("skills/mine").mkdir(parents=True)
    env.local("skills/mine/SKILL.md").write_text("mine")
    sync.install_payload()
    capsys.readouterr()

    sync.install_payload()

    out = capsys.readouterr().out
    assert "removed" not in out
    assert "updated" not in out
    assert "added" not in out.replace("added MCP", "")


def test_export_prune_is_opt_in(env: Env) -> None:
    env.repo_file("skills/gone/SKILL.md", "g")
    env.local("skills/here").mkdir(parents=True)
    env.local("skills/here/SKILL.md").write_text("h")

    assert sync.export_payload() == ([], 0, [])
    assert (env.payload / "skills/gone/SKILL.md").exists()

    assert sync.export_payload(prune=True) == ([], 0, [])
    assert not (env.payload / "skills/gone").exists()
    assert (env.payload / "skills/here/SKILL.md").read_text() == "h"


def test_export_skips_claude_managed_skill_dirs(env: Env) -> None:
    for rel in ("skills/.trash/1-x/old/SKILL.md", "skills/synced/acct/pdf/SKILL.md"):
        env.local(rel).parent.mkdir(parents=True)
        env.local(rel).write_text("managed")
    env.local("skills/mine/synced").mkdir(parents=True)
    env.local("skills/mine/synced/notes.md").write_text("n")

    sync.export_payload()
    assert not (env.payload / "skills/.trash").exists()
    assert not (env.payload / "skills/synced").exists()
    assert (env.payload / "skills/mine/synced/notes.md").read_text() == "n"


def test_missing_plugins_suggests_install_commands(env: Env) -> None:
    env.local("settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"p@m": True, "off@m": False},
                "extraKnownMarketplaces": {
                    "m": {"source": {"source": "github", "repo": "o/r"}}
                },
            }
        )
    )

    assert sync.missing_plugins() == [
        "claude plugin marketplace add o/r",
        "claude plugin install p@m",
    ]


def test_install_ignores_other_branches_and_stashes(env: Env) -> None:
    env.commit()
    env.git("branch", "-M", "main")
    env.git("checkout", "-qb", "work")
    env.repo_file("skills/work/SKILL.md", "work-only")
    env.commit()
    sync.install_payload()
    env.git("checkout", "-q", "main")
    env.local("skills/mine").mkdir(parents=True)
    env.local("skills/mine/SKILL.md").write_text("never committed")
    assert sync.export_payload() == ([], 0, [])
    env.git("stash", "-qu")

    sync.install_payload()

    assert env.local("skills/work/SKILL.md").read_text() == "work-only"
    assert env.local("skills/mine/SKILL.md").read_text() == "never committed"


def test_install_keeps_file_whose_name_differs_only_in_case(env: Env) -> None:
    # On macOS/Windows these are one file; a case-only repo rename must not
    # delete it. Checked here as "a case-insensitive repo match counts".
    env.repo_file("skills/Foo/SKILL.md", "x")
    env.commit()
    sync.install_payload()
    env.git("mv", "claude/home/skills/Foo", "claude/home/skills/tmp")
    env.git("mv", "claude/home/skills/tmp", "claude/home/skills/foo")
    env.commit()

    sync.install_payload()

    assert env.local("skills/Foo/SKILL.md").read_text() == "x"


def test_install_keeps_preexisting_empty_dir(env: Env) -> None:
    env.repo_file("hooks/a.sh", "a")
    env.commit()
    sync.install_payload()
    env.local("hooks/logs").mkdir()
    env.drop("hooks/a.sh")

    sync.install_payload()

    assert not env.local("hooks/a.sh").exists()
    assert env.local("hooks/logs").is_dir()


def test_diff_previews_prune_of_dir_the_repo_dropped(env: Env, capsys) -> None:
    env.repo_file("rules/r.md", "r")
    env.commit()
    sync.install_payload()
    env.drop("rules")
    capsys.readouterr()

    sync.print_diff()

    install_part = capsys.readouterr().out.split("install direction")[1]
    assert "- rules/r.md (dropped from repo, would be removed)" in install_part


def test_diff_matches_install_for_linked_skill_being_updated(env: Env, capsys) -> None:
    env.repo_file("skills/sup/SKILL.md", "v1")
    env.repo_file("skills/sup/old.md", "o")
    env.commit()
    env.git("rm", "-q", "claude/home/skills/sup/old.md")
    env.repo_file("skills/sup/SKILL.md", "v2")
    env.commit()
    (env.agents / "sup").mkdir()
    (env.agents / "sup/SKILL.md").write_text("v1")
    (env.agents / "sup/old.md").write_text("o")
    env.local("skills").mkdir()
    env.local("skills/sup").symlink_to(env.agents / "sup")
    capsys.readouterr()

    sync.print_diff()
    install_part = capsys.readouterr().out.split("install direction")[1]
    sync.install_payload()

    assert "- skills/sup/old.md (dropped from repo" in install_part
    assert not env.local("skills/sup/old.md").exists()
    assert (env.agents / "sup/old.md").read_text() == "o"


def test_export_prune_skips_missing_local_dir(env: Env) -> None:
    env.repo_file("skills/a/SKILL.md", "a")
    env.local("skills").symlink_to(env.home / "gone")

    assert sync.export_payload(prune=True) == ([], 0, [])

    assert (env.payload / "skills/a/SKILL.md").read_text() == "a"


def test_install_writes_through_top_level_stow_link(env: Env, tmp_path: Path) -> None:
    target = tmp_path / "stow" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text("mine")
    env.local("CLAUDE.md").symlink_to(target)
    env.commit()

    sync.install_payload()

    assert env.local("CLAUDE.md").is_symlink()
    assert target.read_text() == "baseline"


def test_settings_merge_keeps_local_hooks_without_duplicates(env: Env) -> None:
    repo_hook = {"matcher": "Edit", "hooks": [{"type": "command", "command": "~/r.sh"}]}
    local_same = {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": f"{env.home}/r.sh"}],
    }
    local_only = {"matcher": "Bash", "hooks": [{"type": "command", "command": "g"}]}
    env.local("settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [local_same, local_only]}})
    )
    env.repo_file("settings.json", json.dumps({"hooks": {"PreToolUse": [repo_hook]}}))
    env.commit()

    sync.install_payload()

    got = json.loads(env.local("settings.json").read_text())
    assert got["hooks"]["PreToolUse"] == [repo_hook, local_only]


def test_materialized_copy_keeps_relative_links_working(env: Env) -> None:
    (env.agents / "shared").mkdir()
    (env.agents / "shared/ref.md").write_text("ref")
    (env.agents / "sup").mkdir()
    (env.agents / "sup/SKILL.md").write_text("old")
    (env.agents / "sup/ref.md").symlink_to("../shared/ref.md")
    (env.agents / "sup/broken.md").symlink_to("../nowhere.md")
    env.local("skills").mkdir()
    env.local("skills/sup").symlink_to(env.agents / "sup")
    env.repo_file("skills/sup/SKILL.md", "new")
    env.commit()

    sync.install_payload()

    assert env.local("skills/sup/ref.md").read_text() == "ref"
    assert not env.local("skills/sup/broken.md").is_symlink()
