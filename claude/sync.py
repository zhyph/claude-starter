#!/usr/bin/env python3
"""Sync Claude Code configuration between ~/.claude and this repo.

Multiplatform (Linux/macOS/Windows), stdlib only. Uses an allowlist:
only known-safe config files are copied, never credentials, history,
sessions, logs or caches. MCP server definitions are exported with all
env/header values redacted, and every export is preceded by a secret
scan of the source files: nothing is written to the repo unless the
scan comes back clean. The merged repo payload is scanned again after
writing, since it can also pick up files merged in from another machine.

Install is a merge that also cleans up after the repo. Files new or
changed in the repo are copied in, settings JSON is deep-merged into the
live file (local-only keys survive), and a local file the repo has since
dropped is removed, but only if its bytes match a version git history
shows at that path; anything that only ever existed on the machine, or
was edited after install, is kept and listed. Symlinked dirs inside
~/.claude are never written or deleted through: they are replaced by real
copies (or removed as links). Everything replaced or removed is backed
up first. Export copies ~/.claude into the repo and, with --prune, also
deletes repo files that no longer exist locally. Run `diff` first to see
what a sync would touch.

Usage:
  python3 sync.py diff      # preview both directions, changes nothing
  python3 sync.py export    # ~/.claude -> repo (run before committing)
  python3 sync.py export --prune  # ...and delete repo files gone locally
  python3 sync.py install   # repo -> ~/.claude (run on a new machine)
  python3 sync.py scan      # secret-scan the repo payload only
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
PAYLOAD_DIR = REPO_DIR / "home"
MCP_FILE = REPO_DIR / "mcp-servers.json"
REMINDERS_FILE = REPO_DIR / "reminders.md"

HOME_DIR = Path.home()
CLAUDE_HOME = HOME_DIR / ".claude"
CLAUDE_JSON = HOME_DIR / ".claude.json"

# Only these ever leave ~/.claude. Everything else (credentials, history,
# sessions, projects, logs, caches, plugins) stays on the machine.
ALLOWED_FILES = [
    "CLAUDE.md",
    "settings.json",
    "settings.local.json",
    "statusline-command.sh",
    "keybindings.json",
]
ALLOWED_DIRS = [
    "rules",
    "skills",
    "agents",
    "commands",
    "hooks",
    "output-styles",
]

# JSON config that Claude Code rewrites with absolute paths as you use it
# (hook commands, `Read(//home/you/...)` permission rules). Exported verbatim
# those pin the repo to one machine, so they are normalized back to ~ on the
# way out. Everything else is copied byte for byte and only warned about.
PORTABLE_FILES = {"settings.json", "settings.local.json", "keybindings.json"}

REDACTED_PREFIX = "<REDACTED:"
_REDACTED_RE = re.compile(re.escape(REDACTED_PREFIX) + r"[^>]*>")

# English placeholder text ("your-api-key-here", "changeme") that the generic
# credential patterns would otherwise flag. Checked against the *value* only.
PLACEHOLDER_WORDS_RE = re.compile(
    r"(?i)\b(your|xxx+|changeme|change_me|example|placeholder|insert|replace|"
    r"dummy|sample|redacted|fake|todo|fixme|here)\b"
)

SECRET_PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_-]{10,}", "Anthropic API key"),
    (r"ctx7sk-[A-Za-z0-9-]{10,}", "Context7 API key"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"AIza[0-9A-Za-z_-]{35}", "Google API key"),
    (r"npm_[A-Za-z0-9]{36}", "npm token"),
    (r"sk-proj-[A-Za-z0-9_-]{20,}", "OpenAI project API key"),
    (r"sk-(?!ant-|proj-)[A-Za-z0-9]{20,}", "OpenAI API key"),
    (r"(?i)bearer\s+[A-Za-z0-9._-]{16,}", "Bearer token in header"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "Private key block"),
    (r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}", "JWT"),
    (
        # Quoted (JSON/YAML "key": "value", 'key': 'value'). The key name
        # allows a prefix (OPENAI_API_KEY, MY_SECRET, ...) since `_` is a
        # word character and \b alone can't see a boundary inside it. No
        # digit requirement: real passphrases aren't always alphanumeric
        # ("correcthorsebatterystaple"). English placeholder values
        # ("your-api-key-here") are filtered separately, by word. The
        # lookbehind only lets a match start where a word run starts; without
        # it every position inside a run rescans the rest of it, which is
        # quadratic and took minutes on megabyte-long lines (minified JSON).
        (
            r"(?i)[\"']?(?<![A-Za-z0-9_])[A-Za-z0-9_]*(api[_-]?key|secret|password|access[_-]?token|token)\b[\"']?"
            r"\s*[:=]\s*([\"'])(?P<val>[A-Za-z0-9+/_.=-]{16,})\2"
        ),
        "Generic credential assignment (quoted)",
    ),
    (
        # Unquoted (export FOO=bar, YAML foo: bar). Same prefix handling.
        # Requires a digit in the value, or this matches every
        # `apiKey: process.env.FOO`-style code reference in documentation,
        # which has no secret in it at all.
        (
            r"(?i)[\"']?(?<![A-Za-z0-9_])[A-Za-z0-9_]*(api[_-]?key|secret|password|access[_-]?token|token)\b[\"']?"
            r"\s*[:=]\s*(?![\"'])(?=[A-Za-z0-9+/_.=-]*[0-9])(?P<val>[A-Za-z0-9+/_.=-]{16,})"
        ),
        "Generic credential assignment (unquoted)",
    ),
]

# (path-glob matched against the scan label, exact pattern-label) pairs that
# suppress specific findings, e.g. vendored plugin skill docs full of
# `apiKey: "abc123..."`-style examples. Only ever used for the low-signal
# generic-assignment labels - never for a concrete token shape like
# sk-ant-/AKIA/ghp_, where a hit is worth a human's attention regardless of
# where it was found.
SCAN_IGNORE: list[tuple[str, str]] = [
    ("*skills/*/references/*", "Generic credential assignment (quoted)"),
    ("*skills/*/references/*", "Generic credential assignment (unquoted)"),
    # security-review documents detection patterns with AWS's published
    # example key and a bare PEM header; scoped to those two files only.
    ("*skills/security-review/references/secret-patterns.md", "AWS access key"),
    ("*skills/security-review/references/vuln-categories.md", "Private key block"),
]


def is_scan_ignored(label: str, secret_label: str) -> bool:
    return any(
        secret_label == ignored_label and fnmatch.fnmatch(label, glob)
        for glob, ignored_label in SCAN_IGNORE
    )


def find_secrets_in_text(label: str, text: str) -> tuple[list[str], int]:
    """Scan text line-by-line for secret-shaped substrings.

    <REDACTED:...> placeholders are masked out first, not skipped whole, so a
    real secret sharing a line with a placeholder is still caught. A match
    whose value is an obvious English placeholder, or that SCAN_IGNORE
    covers, is dropped rather than reported, but still counted.

    Binary content (a NUL in the first 8 KiB, git's own heuristic) is not
    scanned: random bytes in a vendored executable or image turn up
    token-shaped runs that would block the export as false positives.

    Returns (findings, suppressed_count).
    """
    findings = []
    suppressed = 0
    if "\0" in text[:8192]:
        return findings, suppressed
    for lineno, line in enumerate(text.splitlines(), 1):
        masked = _REDACTED_RE.sub("", line)
        for pattern, secret_label in SECRET_PATTERNS:
            m = re.search(pattern, masked)
            if not m:
                continue
            if "val" in m.groupdict() and PLACEHOLDER_WORDS_RE.search(m.group("val")):
                continue
            if is_scan_ignored(label, secret_label):
                suppressed += 1
                continue
            findings.append(f"{label}:{lineno} looks like a {secret_label}")
    return findings, suppressed


def redact_secrets(mapping: dict) -> dict:
    return {key: f"{REDACTED_PREFIX}{key}>" for key in mapping}


def normalize_mcp_server(cfg: dict) -> dict:
    """Comparable view of an MCP server config that ignores secret values
    (compares env/header key names only, not the redacted-vs-real values)."""
    norm = {k: v for k, v in cfg.items() if k not in ("env", "headers")}
    for field in ("env", "headers"):
        value = cfg.get(field)
        norm[f"{field}_keys"] = sorted(value) if isinstance(value, dict) else []
    return norm


def load_mcp_servers(path: Path, *, quiet: bool = True) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        if not quiet:
            print(
                f"  warning: {path} is not valid JSON ({e}); skipping MCP server merge"
            )
        return {}


def compare_mcp_servers(src: dict, dst: dict) -> tuple[list[str], list[str], list[str]]:
    """Like compare_tree, but for MCP server maps: added/changed/preserved."""
    added, changed = [], []
    for name in sorted(src):
        if name not in dst:
            added.append(name)
        elif normalize_mcp_server(src[name]) != normalize_mcp_server(dst[name]):
            changed.append(name)
    preserved = sorted(set(dst) - set(src))
    return added, changed, preserved


def build_mcp_export() -> tuple[dict, list[str], int, str | None]:
    """Build the redacted MCP export in memory, without writing anything.

    env/headers values are redacted. args/command/url are left as-is (redacting
    them would break the command) but scanned: a credential shape there is a
    finding, same as anywhere else in the export.

    Returns (exported, findings, suppressed, error). error is a plain
    (non-secret) message if ~/.claude.json could not be parsed at all.
    """
    if not CLAUDE_JSON.is_file():
        return {}, [], 0, None
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return (
            {},
            [],
            0,
            f"{CLAUDE_JSON} is not valid JSON ({e}), cannot export MCP servers",
        )
    servers = data.get("mcpServers", {})
    exported = {}
    findings: list[str] = []
    suppressed = 0
    for name, cfg in servers.items():
        safe = dict(cfg)
        for field in ("env", "headers"):
            if isinstance(safe.get(field), dict) and safe[field]:
                safe[field] = redact_secrets(safe[field])
        for field in ("command", "url"):
            value = safe.get(field)
            if isinstance(value, str):
                f, s = find_secrets_in_text(f"mcpServers.{name}.{field}", value)
                findings += f
                suppressed += s
        for i, value in enumerate(safe.get("args") or []):
            if isinstance(value, str):
                f, s = find_secrets_in_text(f"mcpServers.{name}.args[{i}]", value)
                findings += f
                suppressed += s
        exported[name] = safe
    return exported, findings, suppressed, None


def merge_mcp_file(local_export: dict) -> dict:
    """Merge this machine's exported servers into mcp-servers.json.

    Overwrites/adds entries for servers present locally; keeps entries for
    servers not present locally (exported from another machine)."""
    existing = load_mcp_servers(MCP_FILE)
    merged = dict(existing)
    merged.update(local_export)
    MCP_FILE.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return merged


def is_excluded_dir(parent: str, name: str) -> bool:
    """Claude-managed dirs that are not portable config: `.trash` holds
    deleted skills, `skills/synced` is the per-account cache claude.ai
    re-syncs on login; `__pycache__` is bytecode with machine paths."""
    return name in (".trash", "__pycache__") or (
        name == "synced" and Path(parent).name == "skills"
    )


def walk_files(root: Path) -> list[Path]:
    """Files under root, sorted, following symlinked dirs.

    Path.rglob does not descend into symlinked dirs (3.13+ default), which
    silently dropped skills that `npx skills add` links in from
    ~/.agents/skills. Real paths already visited are skipped to break cycles.
    """
    files: list[Path] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames[:] = [d for d in dirnames if not is_excluded_dir(dirpath, d)]
        files.extend(Path(dirpath) / f for f in filenames)
    return sorted(p for p in files if p.is_file())


def tree_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in walk_files(root)} if root.is_dir() else set()


def compare_tree(src: Path, dst: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Compare two directory trees file-by-file.

    Returns (added, updated, preserved): files only in src (would be
    added to dst), files in both that differ (would be updated), and
    files only in dst (untouched by a merge in this direction).
    """
    src_files, dst_files = tree_files(src), tree_files(dst)
    added, updated = [], []
    for rel in sorted(src_files):
        if rel not in dst_files:
            added.append(rel)
        elif (src / rel).read_bytes() != (dst / rel).read_bytes():
            updated.append(rel)
    preserved = sorted(dst_files - src_files)
    return added, updated, preserved


def repo_history() -> tuple[dict[str, set[str]], str] | None:
    """Every blob each payload path has held in the checked-out branch's
    history (HEAD, not --all: another branch or a stash is not something this
    checkout dropped), keyed by the path relative to PAYLOAD_DIR (posix),
    plus the repo's object format.

    This is what lets install tell "a file this repo once installed and has
    since dropped" (safe to remove) from "a file that only ever existed on
    this machine" (never touched). None if git or the repo is unavailable,
    in which case install prunes nothing.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(PAYLOAD_DIR), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    try:
        prefix, fmt = git("rev-parse", "--show-prefix", "--show-object-format").split()
        raw = git(
            "log", "HEAD", "-m", "--no-renames", "--raw", "--no-abbrev", "-z",
            "--format=", "--", ".",
        )  # fmt: skip
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    history: dict[str, set[str]] = {}
    tokens = raw.split("\0")
    for meta, path in itertools.pairwise(tokens):
        meta = meta.strip()
        if not meta.startswith(":") or not path.startswith(prefix):
            continue
        fields = meta.split()
        blobs = {sha for sha in fields[2:4] if sha.strip("0")}
        history.setdefault(path[len(prefix) :], set()).update(blobs)
    return history, fmt


def blob_ids(path: Path, fmt: str) -> set[str]:
    """git blob ids path's bytes could correspond to. Includes the LF form of
    a CRLF file, since a Windows checkout (autocrlf) installs CRLF copies of
    LF blobs."""
    data = path.read_bytes()
    variants = {data, data.replace(b"\r\n", b"\n")}
    algo = hashlib.sha256 if fmt == "sha256" else hashlib.sha1
    return {algo(b"blob %d\0" % len(v) + v).hexdigest() for v in variants}


def linked_ancestor(path: Path, root: Path) -> Path | None:
    """Topmost symlink strictly below root on the way to path (path itself
    included), or None. root itself being a link is deliberately ignored:
    that is a stow-style setup where writing through is the point."""
    cur = root
    for part in path.relative_to(root).parts:
        cur = cur / part
        if cur.is_symlink():
            return cur
    return None


def record_link(backups: Path, link: Path, action: str) -> None:
    backups.mkdir(parents=True, exist_ok=True)
    with (backups / "symlinks.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"{link} -> {os.readlink(link)} ({action})\n")


def skip_dangling(directory: str, names: list[str]) -> list[str]:
    """copytree ignore hook: skip links whose target is missing. (copytree's
    own ignore_dangling_symlinks resolves relative targets against the cwd,
    so it also drops live relative links.)"""
    return [
        n
        for n in names
        if os.path.islink(os.path.join(directory, n))
        and not os.path.exists(os.path.join(directory, n))
    ]


def drop_dangling_link(path: Path, backups: Path) -> None:
    if path.is_symlink() and not path.exists():
        record_link(backups, path, "dangling, removed")
        path.unlink()


def materialize_links(dst: Path, root: Path, backups: Path) -> None:
    """Turn every symlinked dir between root and dst.parent into a real dir,
    so an install write lands in ~/.claude instead of silently editing
    whatever the link points at (e.g. ~/.agents/skills, shared with other
    tools). A live link becomes a real copy of its target, which is left
    untouched; a dangling link is dropped. Both are logged to the backup."""
    cur = root
    for part in dst.parent.relative_to(root).parts:
        cur = cur / part
        if not cur.is_symlink():
            continue
        if cur.is_dir():
            tmp = cur.with_name(f".{cur.name}.sync-tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            # Dereference: a relative link inside the target would dangle
            # once its copy lives under ~/.claude instead.
            shutil.copytree(cur, tmp, ignore=skip_dangling)
            record_link(backups, cur, "replaced with a real copy")
            cur.unlink()
            tmp.rename(cur)
        else:
            record_link(backups, cur, "dangling, removed")
            cur.unlink()


def plan_prune(
    name: str,
    history: dict[str, set[str]],
    fmt: str,
    materialized: frozenset[Path] = frozenset(),
) -> tuple[list[Path], list[Path], list[Path]]:
    """What install would remove from ~/.claude/<name>.

    A local file absent from the repo is removable only if its exact bytes
    were once this repo's content at the same path, i.e. the repo installed
    it and has since dropped it. Anything else (never in the repo, or edited
    after install) is kept. Removal never reaches through a symlink: a linked
    dir goes away as a link, and only if everything under it is removable.

    A repo path matching case-insensitively counts as present: on macOS and
    Windows `Foo/x` and `foo/x` are the same file, so a case-only rename in
    the repo must not delete it. materialized lists links install turns into
    real dirs before pruning (diff passes them so its preview matches).

    Returns (files, links, kept), all absolute paths. A removable file inside
    a link that must stay (it also holds repo files) is reported as kept.
    """
    dst_root = CLAUDE_HOME / name
    if not dst_root.is_dir():
        return [], [], []
    repo_files = {rel.as_posix().casefold() for rel in tree_files(PAYLOAD_DIR / name)}
    files: list[Path] = []
    kept: list[Path] = []
    link_ok: dict[Path, bool] = {}
    under_link: dict[Path, list[Path]] = {}
    for path in walk_files(dst_root):
        rel = path.relative_to(dst_root)
        link = linked_ancestor(path, dst_root)
        if link in materialized:
            link = None
        if rel.as_posix().casefold() in repo_files:
            if link is not None:
                link_ok[link] = False
            continue
        known = history.get(f"{name}/{rel.as_posix()}", set())
        removable = bool(known & blob_ids(path, fmt))
        if link is not None and link != path:
            link_ok[link] = link_ok.get(link, True) and removable
            under_link.setdefault(link, []).append(path)
        elif removable:
            files.append(path)
        else:
            kept.append(path)
    links = sorted(link for link, ok in link_ok.items() if ok)
    for link, paths in under_link.items():
        if not link_ok[link]:
            kept += paths
    return files, links, sorted(kept)


def remove_emptied_dirs(removed: list[Path], root: Path) -> None:
    """rmdir the parents of removed paths that are now empty, up to root.
    Empty dirs that were already empty (a user's, say) are left alone."""
    for path in removed:
        d = path.parent
        while d != root and d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent


def prune_install(
    name: str, history: dict[str, set[str]], fmt: str, backups: Path
) -> tuple[list[Path], list[Path]]:
    """Remove what plan_prune allows, backing it up first.

    Returns (removed, kept) as paths relative to ~/.claude/<name>."""
    files, links, kept = plan_prune(name, history, fmt)
    dst_root = CLAUDE_HOME / name
    for path in files:
        backup = backups / path.relative_to(CLAUDE_HOME)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.unlink()
    for link in links:
        record_link(backups, link, "dropped from repo, link removed")
        link.unlink()
    remove_emptied_dirs(files + links, dst_root)
    removed = [p.relative_to(dst_root) for p in files + links]
    return removed, [p.relative_to(dst_root) for p in kept]


def print_grouped(verb: str, name: str, rels: list[Path], why: str = "") -> None:
    """One line per top-level entry (a skill dir, say), not per file."""
    groups: dict[str, list[Path]] = {}
    for rel in rels:
        groups.setdefault(rel.parts[0], []).append(rel)
    for top, members in groups.items():
        if len(members) == 1:
            detail = f" ({why})" if why else ""
            print(f"  {verb} {name}/{members[0].as_posix()}{detail}")
        else:
            detail = f" ({len(members)} files{', ' + why if why else ''})"
            print(f"  {verb} {name}/{top}/{detail}")


def merge_copy_file(
    src: Path,
    dst: Path,
    backups: Path | None = None,
    backup_root: Path | None = None,
    stale_warnings: list[str] | None = None,
    label: str | None = None,
    link_root: Path | None = None,
) -> str:
    """Copy src over dst if new or changed. Returns 'added', 'updated', or 'unchanged'.

    With link_root (install), symlinks below link_root on the way to dst are
    replaced by real dirs/files first (see materialize_links), so the write
    never edits a link's target."""
    if dst.exists():
        if src.read_bytes() == dst.read_bytes():
            return "unchanged"
        if stale_warnings is not None and dst.stat().st_mtime > src.stat().st_mtime:
            stale_warnings.append(label if label is not None else str(dst))
        if backups is not None and backup_root is not None:
            rel = dst.relative_to(backup_root)
            backup_dest = backups / rel
            backup_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup_dest)
        status = "updated"
    else:
        status = "added"
    if link_root is not None and backups is not None:
        materialize_links(dst, link_root, backups)
        if dst.is_symlink():
            record_link(backups, dst, "replaced with a real file")
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return status


def merge_copy_text(
    content: str,
    dst: Path,
    src_stat_path: Path,
    backups: Path | None = None,
    backup_root: Path | None = None,
    stale_warnings: list[str] | None = None,
    label: str | None = None,
) -> str:
    """Like merge_copy_file, but the source is content already in memory
    (used for settings.json, which is rewritten to drop/reattach autoMode)."""
    data = content.encode("utf-8")
    if dst.exists():
        if dst.read_bytes() == data:
            return "unchanged"
        if (
            stale_warnings is not None
            and dst.stat().st_mtime > src_stat_path.stat().st_mtime
        ):
            stale_warnings.append(label if label is not None else str(dst))
        if backups is not None and backup_root is not None:
            rel = dst.relative_to(backup_root)
            backup_dest = backups / rel
            backup_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup_dest)
        status = "updated"
    else:
        status = "added"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    try:
        st = src_stat_path.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    return status


def merge_copy_tree(
    src: Path,
    dst: Path,
    backups: Path | None = None,
    backup_root: Path | None = None,
    stale_warnings: list[str] | None = None,
    label_prefix: str = "",
    link_root: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    """Merge-copy every file under src into dst. Never deletes dst-only files
    (install removes those separately, see prune_install)."""
    added, updated = [], []
    for path in walk_files(src):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        label = f"{label_prefix}{rel}"
        status = merge_copy_file(
            path, dst / rel, backups, backup_root, stale_warnings, label, link_root
        )
        if status == "added":
            added.append(rel)
        elif status == "updated":
            updated.append(rel)
    return added, updated


# A home path only counts as a match if what follows it can't be part of a
# longer name: /home/zhyp must not swallow /home/zhyphen.
_HOME_TAIL = r"(?![A-Za-z0-9_.-])"


def home_prefixes() -> list[str]:
    """Every literal spelling of this machine's home dir that can turn up in
    a config value: native (C:\\Users\\you on Windows) and forward-slashed."""
    native = str(HOME_DIR).rstrip("/\\")
    posix = native.replace("\\", "/")
    return [x for x in dict.fromkeys((native, posix)) if x]


def home_path_in(text: str) -> bool:
    return any(
        re.search(re.escape(home) + _HOME_TAIL, text) for home in home_prefixes()
    )


def _unquote_home(match: re.Match[str]) -> str:
    """'/home/you/x' -> ~/x. A tilde does not expand inside single quotes, so
    the quotes have to come off with it; a path that needs quoting (it has
    whitespace) switches to "$HOME/..." instead, which expands and stays safe.
    """
    rest = match.group(1)
    return f'"$HOME{rest}"' if re.search(r"\s", rest) else f"~{rest}"


def portable_string(value: str) -> str:
    """Rewrite this machine's home dir to ~ inside one config string.

    Claude Code hands back three shapes: `//home/you/...` (the absolute form
    of a permission rule), `'/home/you/...'` (a quoted hook command) and a
    plain `/home/you/...`. All three break on another install - the hook path
    does not exist there and, worse, the deny rules stop matching, so ~/.ssh
    and friends become readable again.
    """
    for home in sorted(home_prefixes(), key=len, reverse=True):
        esc = re.escape(home)
        # The permission-rule form is one extra leading slash on top of a
        # home path that already starts with one: //home/you/.ssh/**.
        doubled = re.escape(("/" if home.startswith("/") else "//") + home)
        value = re.sub(rf"{doubled}{_HOME_TAIL}", "~", value)
        value = re.sub(rf"'{esc}((?:[/\\][^']*)?)'", _unquote_home, value)
        value = re.sub(rf"{esc}{_HOME_TAIL}", "~", value)
    return value


def portable_json(data):
    """portable_string applied to every string in a parsed JSON document."""
    if isinstance(data, dict):
        return {k: portable_json(v) for k, v in data.items()}
    if isinstance(data, list):
        return [portable_json(v) for v in data]
    if isinstance(data, str):
        return portable_string(data)
    return data


def portable_json_text(src: Path) -> tuple[str | None, str | None]:
    """src re-serialized with machine paths rewritten to ~. (text, error)."""
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"{src} is not valid JSON ({e}), skipping {src.name} export"
    return json.dumps(portable_json(data), indent=2) + "\n", None


def redacted_settings_text() -> tuple[str | None, Path, str | None]:
    """Live settings.json with the autoMode key and Orca hooks dropped.

    autoMode can carry sensitive prose about private repos/orgs; it never
    leaves the machine. The local file itself is left untouched.

    Returns (text, src_path, error). text is None (with error set) if the
    live file exists but isn't valid JSON.
    """
    src = CLAUDE_HOME / "settings.json"
    if not src.is_file():
        return None, src, None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, src, f"{src} is not valid JSON ({e})"
    if "autoMode" in data:
        del data["autoMode"]
    # Orca injects its own agent hooks; they only work where Orca is installed.
    for ev, groups in list(data.get("hooks", {}).items()):
        for g in groups:
            g["hooks"] = [
                h
                for h in g.get("hooks", [])
                if ".orca/agent-hooks" not in h.get("command", "")
            ]
        data["hooks"][ev] = [g for g in groups if g["hooks"]]
        if not data["hooks"][ev]:
            del data["hooks"][ev]
    return json.dumps(portable_json(data), indent=2) + "\n", src, None


# Settings files are merged into the local copy on install, not overwritten:
# a machine keeps what only it has (a work laptop's proxy env vars, extra
# plugins, autoMode) and takes the repo's value wherever both define a key.
MERGED_JSON_FILES = {"settings.json", "settings.local.json"}
# Lists that accumulate across machines instead of the repo's list winning.
UNION_LISTS = {
    ("permissions", "allow"),
    ("permissions", "deny"),
    ("permissions", "ask"),
    ("permissions", "additionalDirectories"),
}


def merge_settings(local, repo, path: tuple[str, ...] = ()):
    """Deep merge, repo wins on conflicts. Dicts merge key by key; the
    UNION_LISTS and each hooks.<Event> list keep local-only entries after the
    repo's; any other value is taken from the repo. A key the repo dropped stays local (there is no
    telling a deletion from a key the repo never had)."""
    if isinstance(local, dict) and isinstance(repo, dict):
        out = dict(local)
        for key, value in repo.items():
            out[key] = (
                merge_settings(local[key], value, (*path, key))
                if key in local
                else value
            )
        return out
    unions = path in UNION_LISTS or (len(path) == 2 and path[0] == "hooks")
    if unions and isinstance(local, list) and isinstance(repo, list):
        # Local entries are compared in their ~-normalized form: the repo
        # copy never holds absolute home paths, so a raw compare would keep
        # a duplicate of every hook Claude Code rewrote to an absolute path.
        return repo + [x for x in local if portable_json(x) not in repo]
    return repo


def settings_install_text(
    repo_src: Path, local_dst: Path
) -> tuple[str | None, str | None]:
    """Repo settings content merged into the live file (see merge_settings).

    Returns (content, error).
    """
    if not repo_src.is_file():
        return None, None
    try:
        data = json.loads(repo_src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return (
            None,
            f"{repo_src} is not valid JSON ({e}), skipping {repo_src.name} install",
        )
    if local_dst.is_file():
        try:
            local_data = json.loads(local_dst.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(
                f"  warning: {local_dst} is not valid JSON ({e}); "
                "replacing it with the repo copy (old file is in the backup)"
            )
            local_data = {}
        data = merge_settings(local_data, data)
    return json.dumps(data, indent=2) + "\n", None


def export_payload(prune: bool = False) -> tuple[list[str], int, list[str]]:
    """Scan source files first; only write to the repo if the scan is clean.

    With prune, repo files under the synced dirs that no longer exist in
    ~/.claude are deleted (git still has them). Without it they are listed.

    Returns (findings, suppressed, errors). findings/errors both empty means
    a clean, completed export. errors are plain (non-secret) problems, e.g.
    unparsable JSON, that also block the write."""
    if not CLAUDE_HOME.is_dir():
        sys.exit(f"error: {CLAUDE_HOME} does not exist")

    print("  scanning source files for secrets")
    findings: list[str] = []
    errors: list[str] = []
    suppressed = 0

    nonportable: list[str] = []

    settings_text, settings_src, settings_error = redacted_settings_text()
    if settings_error:
        errors.append(settings_error)
    elif settings_text is not None:
        if home_path_in(settings_text):
            nonportable.append("~/.claude/settings.json")
        f, s = find_secrets_in_text("~/.claude/settings.json", settings_text)
        findings += f
        suppressed += s

    file_sources: list[tuple[str, Path]] = []
    text_sources: list[tuple[str, Path, str]] = []
    for name in ALLOWED_FILES:
        if name == "settings.json":
            continue
        src = CLAUDE_HOME / name
        if not src.is_file():
            continue
        label = f"~/.claude/{name}"
        if name in PORTABLE_FILES:
            portable_text, portable_error = portable_json_text(src)
            if portable_error or portable_text is None:
                errors.append(portable_error or f"{src}: could not normalize paths")
                continue
            text = portable_text
            text_sources.append((name, src, text))
        else:
            text = src.read_text(encoding="utf-8", errors="ignore")
            file_sources.append((name, src))
        if home_path_in(text):
            nonportable.append(label)
        f, s = find_secrets_in_text(label, text)
        findings += f
        suppressed += s

    for name in ALLOWED_DIRS:
        src = CLAUDE_HOME / name
        if not src.is_dir():
            continue
        for path in walk_files(src):
            if not path.is_file():
                continue
            rel = path.relative_to(CLAUDE_HOME)
            text = path.read_text(encoding="utf-8", errors="ignore")
            if home_path_in(text):
                nonportable.append(f"~/.claude/{rel}")
            f, s = find_secrets_in_text(f"~/.claude/{rel}", text)
            findings += f
            suppressed += s

    if REMINDERS_FILE.is_file():
        text = REMINDERS_FILE.read_text(encoding="utf-8", errors="ignore")
        f, s = find_secrets_in_text(REMINDERS_FILE.name, text)
        findings += f
        suppressed += s

    mcp_export, mcp_findings, mcp_suppressed, mcp_error = build_mcp_export()
    findings += mcp_findings
    suppressed += mcp_suppressed
    if mcp_error:
        errors.append(mcp_error)
    elif mcp_export and MCP_FILE.is_file():
        # merge_mcp_file below merges into whatever's already in mcp-servers.json.
        # If that file is unparsable, load_mcp_servers would silently treat it as
        # empty and the write would replace it with only this machine's servers,
        # discarding anything merged in from other machines. Fail loudly instead.
        try:
            json.loads(MCP_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(
                f"{MCP_FILE} is not valid JSON ({e}), cannot merge exported MCP "
                "servers without losing servers from other machines"
            )

    if findings or errors:
        return findings, suppressed, errors

    PAYLOAD_DIR.mkdir(exist_ok=True)
    stale: list[str] = []

    if settings_text is not None:
        status = merge_copy_text(
            settings_text,
            PAYLOAD_DIR / "settings.json",
            settings_src,
            stale_warnings=stale,
            label="settings.json",
        )
        if status != "unchanged":
            print(f"  {status} settings.json")

    for name, src, text in text_sources:
        status = merge_copy_text(
            text, PAYLOAD_DIR / name, src, stale_warnings=stale, label=name
        )
        if status != "unchanged":
            print(f"  {status} {name} (paths normalized to ~)")

    for name, src in file_sources:
        status = merge_copy_file(
            src, PAYLOAD_DIR / name, stale_warnings=stale, label=name
        )
        if status != "unchanged":
            print(f"  {status} {name}")

    for name in ALLOWED_DIRS:
        src = CLAUDE_HOME / name
        dst = PAYLOAD_DIR / name
        if src.is_dir():
            added, updated = merge_copy_tree(
                src, dst, stale_warnings=stale, label_prefix=f"{name}/"
            )
            for rel in added:
                print(f"  added {name}/{rel}")
            for rel in updated:
                print(f"  updated {name}/{rel}")
            if not added and not updated:
                print(f"  {name}/: no changes")

    # A synced dir missing locally (or a dangling link to one) says nothing
    # about which repo files are gone; never treat it as "delete everything".
    # Names compare case-insensitively so macOS/Windows case-only renames
    # don't look like deletions.
    repo_only: list[tuple[str, Path]] = []
    for name in ALLOWED_DIRS:
        local_dir = CLAUDE_HOME / name
        if not local_dir.is_dir():
            if (PAYLOAD_DIR / name).is_dir():
                print(f"  note: {local_dir} missing, not pruning repo {name}/")
            continue
        local = {rel.as_posix().casefold() for rel in tree_files(local_dir)}
        repo_only += [
            (name, rel)
            for rel in sorted(tree_files(PAYLOAD_DIR / name))
            if rel.as_posix().casefold() not in local
        ]
    if prune:
        for name, rel in repo_only:
            (PAYLOAD_DIR / name / rel).unlink()
            print(f"  removed {name}/{rel} (no longer in ~/.claude)")
        remove_emptied_dirs(
            [PAYLOAD_DIR / name / rel for name, rel in repo_only], PAYLOAD_DIR
        )
    elif repo_only:
        print(f"\n  {len(repo_only)} repo file(s) no longer exist in ~/.claude:")
        for name, rel in repo_only:
            print(f"    - {name}/{rel}")
        print(
            "  kept (they may come from another machine's export); "
            "run `export --prune` to delete them."
        )

    if not CLAUDE_JSON.is_file():
        print("  skip: ~/.claude.json not found, no MCP servers exported")
    elif not mcp_export:
        print("  skip: no user-level MCP servers configured")
    else:
        merged = merge_mcp_file(mcp_export)
        print(
            f"  wrote {MCP_FILE.name} ({len(mcp_export)} server(s) from this machine, "
            f"{len(merged)} total, secrets redacted)"
        )

    if nonportable:
        print(f"\n  warning: still references {HOME_DIR} after normalization:")
        for label in nonportable:
            print(f"    - {label}")
        print("  another machine will not resolve those paths; use ~ or $HOME instead.")

    if stale:
        print("\n  warning: repo copy is newer than the local file for:")
        for label in stale:
            print(f"    - {label}")
        print(
            "  export overwrote it anyway; check before committing if that's unexpected."
        )

    return [], suppressed, []


def scan_repo_payload() -> tuple[list[str], int]:
    """Scan already-exported repo content for secrets.

    Used by `sync.py scan`, and as a post-write check after `export` (the
    pre-write scan only sees this machine's source files, not what a prior
    export from another machine already put in the repo)."""
    targets: list[tuple[str, Path]] = []
    if PAYLOAD_DIR.is_dir():
        for path in walk_files(PAYLOAD_DIR):
            if path.is_file():
                targets.append((str(path.relative_to(REPO_DIR)), path))
    if MCP_FILE.is_file():
        targets.append((MCP_FILE.name, MCP_FILE))
    if REMINDERS_FILE.is_file():
        targets.append((REMINDERS_FILE.name, REMINDERS_FILE))
    findings: list[str] = []
    suppressed = 0
    for label, path in targets:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        f, s = find_secrets_in_text(label, text)
        findings += f
        suppressed += s
    return findings, suppressed


def backup_dir() -> Path:
    """A backup dir no earlier run used (two installs can share a second)."""
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    base = CLAUDE_HOME / "backups" / f"dotfiles-sync-{stamp}"
    path, n = base, 1
    while path.exists():
        path, n = base.with_name(f"{base.name}-{n}"), n + 1
    return path


def install_mcp_servers(backups: Path) -> tuple[list[str], list[str], bool]:
    """Merge exported servers into ~/.claude.json.

    Never clobbers a server name that already exists locally (it may hold
    real secrets); such names are instead checked for config drift.

    Returns (needs_secrets, drifted, ok). needs_secrets lists secrets that
    still hold a redacted placeholder and must be filled in by hand. drifted
    lists server names that exist both locally and in the export but differ
    on non-secret fields. ok is False if ~/.claude.json could not be parsed,
    in which case the merge was skipped entirely.
    """
    if not MCP_FILE.is_file():
        return [], [], True
    exported = load_mcp_servers(MCP_FILE, quiet=False)
    data: dict = {}
    if CLAUDE_JSON.is_file():
        try:
            data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(
                f"  error: {CLAUDE_JSON} is not valid JSON ({e}); skipping MCP server merge"
            )
            return [], [], False
    servers = data.setdefault("mcpServers", {})
    added = []
    drifted = []
    for name, cfg in exported.items():
        if name in servers:
            if normalize_mcp_server(cfg) != normalize_mcp_server(servers[name]):
                drifted.append(name)
            continue  # never clobber a locally configured server (it has real secrets)
        servers[name] = cfg
        added.append(name)
    if added:
        if CLAUDE_JSON.is_file():
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CLAUDE_JSON, backups / CLAUDE_JSON.name)
        CLAUDE_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  added MCP servers: {', '.join(added)}")
    else:
        print("  MCP servers: all already present, nothing changed")
    # After the merge, check every server the export knows about: whether it
    # was just added or already existed, any secret still missing or holding
    # a placeholder needs a manual fix.
    needs_secrets = []
    for name, cfg in exported.items():
        local = servers.get(name, {})
        for field in ("env", "headers"):
            for key, value in (cfg.get(field) or {}).items():
                if not (isinstance(value, str) and value.startswith(REDACTED_PREFIX)):
                    continue
                current = (local.get(field) or {}).get(key)
                if not current or (
                    isinstance(current, str) and current.startswith(REDACTED_PREFIX)
                ):
                    needs_secrets.append(f"{name}.{field}.{key}")
    return needs_secrets, drifted, True


def missing_plugins() -> list[str]:
    """Commands that install the plugins settings.json enables but this
    machine doesn't have yet. Printed, never run: installing a plugin runs
    third-party code, which deserves a look first."""
    settings = load_mcp_servers(CLAUDE_HOME / "settings.json")  # any JSON file
    plugins_dir = CLAUDE_HOME / "plugins"
    installed = load_mcp_servers(plugins_dir / "installed_plugins.json").get(
        "plugins", {}
    )
    known = load_mcp_servers(plugins_dir / "known_marketplaces.json")
    extra = settings.get("extraKnownMarketplaces", {})
    missing = [
        p
        for p, on in settings.get("enabledPlugins", {}).items()
        if on and p not in installed
    ]
    commands = []
    for market in sorted({p.split("@", 1)[1] for p in missing if "@" in p}):
        if market in known:
            continue
        src = extra.get(market, {}).get("source", {})
        where = src.get("repo") or src.get("url") or src.get("path")
        if where:
            commands.append(f"claude plugin marketplace add {where}")
    commands += [f"claude plugin install {p}" for p in missing]
    return commands


def print_reminders(
    needs_secrets: list[str], drifted: list[str], kept: list[str]
) -> None:
    print("\n" + "=" * 24 + " POST-INSTALL REMINDERS " + "=" * 24)
    if needs_secrets:
        print("Secrets were redacted on export and MUST be filled in by hand,")
        print(f"in {CLAUDE_JSON} under mcpServers:")
        for entry in needs_secrets:
            print(f"  - {entry}")
    else:
        print("No missing MCP secrets detected.")
    if drifted:
        print("\nConfig drift (exported config differs from local on non-secret")
        print("fields; local was kept as-is, review manually):")
        for name in drifted:
            print(f"  - {name}")
    if kept:
        print("\nLocal-only files, not from this repo (kept, review if unexpected):")
        for label in kept:
            print(f"  - {label}")
    commands = missing_plugins()
    if commands:
        print("\nEnabled plugins not installed yet (review, then run):")
        for cmd in commands:
            print(f"  {cmd}")
    print("\nAlso remember:")
    print("  - Log in on first `claude` run (credentials are never synced).")
    if REMINDERS_FILE.is_file():
        print(f"\nNotes from {REMINDERS_FILE.name}:")
        print(REMINDERS_FILE.read_text(encoding="utf-8").rstrip())
    print("=" * 72)


def install_payload() -> None:
    if not PAYLOAD_DIR.is_dir():
        sys.exit(
            f"error: nothing to install, {PAYLOAD_DIR} is missing (run export first)"
        )
    CLAUDE_HOME.mkdir(exist_ok=True)
    backups = backup_dir()
    stale: list[str] = []

    # Settings are merged into the live file (merge_settings), so local-only
    # keys such as autoMode or a machine's own env vars survive. A local file
    # being newer is normal there, hence no stale warning.
    for name in sorted(MERGED_JSON_FILES):
        repo_src, local_dst = PAYLOAD_DIR / name, CLAUDE_HOME / name
        if repo_src.is_file():
            drop_dangling_link(local_dst, backups)
        content, error = settings_install_text(repo_src, local_dst)
        if error:
            print(f"  error: {error}")
        elif content is not None:
            status = merge_copy_text(
                content, local_dst, repo_src, backups=backups, backup_root=CLAUDE_HOME
            )
            if status != "unchanged":
                print(f"  {status} {name} (merged)")

    for name in ALLOWED_FILES:
        if name in MERGED_JSON_FILES:
            continue
        src = PAYLOAD_DIR / name
        if not src.is_file():
            continue
        dst = CLAUDE_HOME / name
        drop_dangling_link(dst, backups)
        # Top-level files are written through a live link (stow-style).
        status = merge_copy_file(src, dst, backups, CLAUDE_HOME, stale, name)
        if name.endswith(".sh") and sys.platform != "win32":
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR)
        if status != "unchanged":
            print(f"  {status} {name}")

    found = repo_history()
    if found is None:
        print(
            "  note: git history unavailable, nothing dropped from the repo is removed"
        )
    history, fmt = found if found is not None else ({}, "sha1")
    kept: list[str] = []
    for name in ALLOWED_DIRS:
        src = PAYLOAD_DIR / name
        dst = CLAUDE_HOME / name
        if dst.is_symlink() and not dst.exists():
            print(f"  error: {dst} is a broken symlink, skipping {name}/")
            continue
        added: list[Path] = []
        updated: list[Path] = []
        removed: list[Path] = []
        if src.is_dir():
            added, updated = merge_copy_tree(
                src, dst, backups, CLAUDE_HOME, stale, f"{name}/", link_root=dst
            )
        if found is not None:
            removed, local_only = prune_install(name, history, fmt, backups)
            kept += [f"{name}/{rel.as_posix()}" for rel in local_only]
        print_grouped("added", name, added)
        print_grouped("updated", name, updated)
        print_grouped("removed", name, removed, "dropped from repo")
        if src.is_dir() and not (added or updated or removed):
            print(f"  {name}/: no changes")
    needs_secrets, drifted, ok = install_mcp_servers(backups)
    if backups.exists():
        print(f"  replaced or removed files were backed up to {backups}")
    print_reminders(needs_secrets, drifted, kept)
    if stale:
        print("\n  warning: local file was newer than the repo copy for:")
        for label in stale:
            print(f"    - {label}")
        print(
            "  install overwrote it anyway; check the backup above if that's unexpected."
        )
    if not ok:
        sys.exit(1)


def mtime_note(local_path: Path, repo_path: Path) -> str:
    """Annotation like '; local newer by 2d' for a changed file, or '' if unknown."""
    try:
        local_m = local_path.stat().st_mtime
        repo_m = repo_path.stat().st_mtime
    except OSError:
        return ""
    delta = local_m - repo_m
    if abs(delta) < 1:
        return ""
    which = "local" if delta > 0 else "repo"
    seconds = abs(delta)
    if seconds >= 86400:
        return f"; {which} newer by {seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"; {which} newer by {seconds / 3600:.0f}h"
    return f"; {which} newer"


def print_diff() -> None:
    print(f"export direction ({CLAUDE_HOME} -> {PAYLOAD_DIR}):")
    _print_direction_diff(CLAUDE_HOME, PAYLOAD_DIR)
    _print_mcp_diff("export")
    print(f"\ninstall direction ({PAYLOAD_DIR} -> {CLAUDE_HOME}):")
    _print_direction_diff(PAYLOAD_DIR, CLAUDE_HOME)
    _print_mcp_diff("install")


def _print_direction_diff(src_root: Path, dst_root: Path) -> None:
    changed = False
    installing = src_root == PAYLOAD_DIR
    for name in ALLOWED_FILES:
        src, dst = src_root / name, dst_root / name
        if not src.is_file():
            continue
        if not dst.is_file():
            print(f"  + {name} (new)")
            changed = True
            continue
        if installing and name in MERGED_JSON_FILES:
            # Install merges instead of overwriting: compare the merge result.
            merged, error = settings_install_text(src, dst)
            if error:
                print(f"  ! {name}: {error}")
                changed = True
                continue
            try:
                same = json.loads(merged or "null") == json.loads(
                    dst.read_text("utf-8")
                )
            except json.JSONDecodeError:
                same = False
            if not same:
                print(f"  ~ {name} (repo keys would be merged in)")
                changed = True
            continue
        if name == "settings.json":
            # Compare the autoMode-stripped view of the live file against the
            # repo copy (autoMode never lands in the repo), not raw bytes.
            live_text, _, live_error = redacted_settings_text()
            if live_error:
                print(f"  ! settings.json: {live_error}, skipping comparison")
                changed = True
                continue
            repo_file = PAYLOAD_DIR / name
            if live_text is None or not repo_file.is_file():
                continue
            if live_text.encode("utf-8") != repo_file.read_bytes():
                note = mtime_note(CLAUDE_HOME / name, PAYLOAD_DIR / name)
                print(f"  ~ {name} (changed{note})")
                changed = True
            continue
        if name in PORTABLE_FILES:
            # Same idea: compare the ~-normalized view of the live file. The
            # repo copy never holds this machine's absolute paths, so raw
            # bytes would report a permanent, meaningless difference.
            live_text, live_error = portable_json_text(CLAUDE_HOME / name)
            if live_error:
                print(f"  ! {name}: {live_error}, skipping comparison")
                changed = True
                continue
            repo_file = PAYLOAD_DIR / name
            if live_text is None or not repo_file.is_file():
                continue
            if live_text.encode("utf-8") != repo_file.read_bytes():
                note = mtime_note(CLAUDE_HOME / name, PAYLOAD_DIR / name)
                print(f"  ~ {name} (changed{note})")
                changed = True
            continue
        if src.read_bytes() != dst.read_bytes():
            note = mtime_note(CLAUDE_HOME / name, PAYLOAD_DIR / name)
            print(f"  ~ {name} (changed{note})")
            changed = True
    found = repo_history() if installing else None
    for name in ALLOWED_DIRS:
        src, dst = src_root / name, dst_root / name
        # A dir the repo dropped entirely still gets pruned on install, so
        # it still needs a preview.
        if not src.is_dir() and not (installing and dst.is_dir()):
            continue
        added, updated, preserved = compare_tree(src, dst)
        for rel in added:
            print(f"  + {name}/{rel} (new)")
            changed = True
        for rel in updated:
            note = mtime_note(CLAUDE_HOME / name / rel, PAYLOAD_DIR / name / rel)
            print(f"  ~ {name}/{rel} (changed{note})")
            changed = True
        if found is not None:
            # Install turns a link into a real copy before pruning whenever
            # it writes under it; mirror that so the preview matches.
            written = {
                link
                for rel in added + updated
                if (link := linked_ancestor(dst / rel, dst)) is not None
            }
            files, links, kept = plan_prune(name, *found, frozenset(written))
            rels = [p.relative_to(dst) for p in files + links]
            print_grouped("-", name, rels, "dropped from repo, would be removed")
            changed = changed or bool(rels)
            preserved = kept
        if preserved:
            print(
                f"  = {name}/: {len(preserved)} file(s) only at destination, would be preserved"
            )
    if not changed:
        print("  nothing to change")


def _print_mcp_diff(direction: str) -> None:
    local = load_mcp_servers(CLAUDE_JSON).get("mcpServers", {})
    repo = load_mcp_servers(MCP_FILE)
    src, dst = (local, repo) if direction == "export" else (repo, local)
    added, changed, preserved = compare_mcp_servers(src, dst)
    if not (added or changed or preserved):
        return
    print("  MCP servers:")
    for name in added:
        print(f"    + {name} (new)")
    for name in changed:
        print(f"    ~ {name} (changed)")
    if preserved:
        print(
            f"    = {len(preserved)} server(s) only at destination, would be preserved"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["diff", "export", "install", "scan"])
    parser.add_argument(
        "--prune",
        action="store_true",
        help="export only: delete repo files that no longer exist in ~/.claude",
    )
    args = parser.parse_args()
    action = args.action
    if args.prune and action != "export":
        parser.error("--prune only applies to export")

    if action == "diff":
        print_diff()
    elif action == "export":
        print(f"exporting {CLAUDE_HOME} -> {PAYLOAD_DIR}")
        findings, suppressed, errors = export_payload(args.prune)
        if errors:
            for e in errors:
                print(f"  error: {e}")
        if findings:
            for f in findings:
                print(f"  SECRET? {f}")
        if findings or errors:
            sys.exit(
                f"\n{len(findings)} potential secret(s) and {len(errors)} error(s) found in "
                f"source files. Nothing was written to {PAYLOAD_DIR}. Resolve them (edit the "
                "source, or extend SECRET_PATTERNS/SCAN_IGNORE if a finding is a false "
                "positive) and re-run export."
            )
        print("  scanning merged repo payload for secrets")
        post_findings, post_suppressed = scan_repo_payload()
        suppressed += post_suppressed
        if post_findings:
            for f in post_findings:
                print(f"  SECRET? {f}")
            sys.exit(
                f"\n{len(post_findings)} potential secret(s) found in the merged repo payload "
                "(possibly merged in from another machine's export, or a preserved "
                "mcp-servers.json entry). DO NOT COMMIT until resolved."
            )
        if suppressed:
            print(f"  ({suppressed} finding(s) suppressed by SCAN_IGNORE)")
        print("  clean, safe to commit")
    elif action == "install":
        print(f"installing {PAYLOAD_DIR} -> {CLAUDE_HOME}")
        install_payload()
    else:
        print(f"scanning {PAYLOAD_DIR}")
        findings, suppressed = scan_repo_payload()
        if findings:
            for f in findings:
                print(f"  SECRET? {f}")
            sys.exit(f"\n{len(findings)} potential secret(s) found")
        if suppressed:
            print(f"  ({suppressed} finding(s) suppressed by SCAN_IGNORE)")
        print("  clean")


if __name__ == "__main__":
    main()
