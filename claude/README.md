# Claude Code setup sync

Portable Claude Code configuration. `sync.py` is stdlib-only Python 3 and
works on Linux, macOS and Windows.

## What is synced

| Source | Contents |
| --- | --- |
| `home/` | Mirror of the safe parts of `~/.claude`: `CLAUDE.md`, `settings.json` (with the `autoMode` key dropped, see below), `settings.local.json` (permission allowlist), `statusline-command.sh`, `keybindings.json`, and the `rules/`, `skills/`, `agents/`, `commands/`, `hooks/`, `output-styles/` dirs (when they exist) |
| `mcp-servers.json` | User-level MCP servers from `~/.claude.json`, with every `env`/`headers` value replaced by `<REDACTED:KEY>` placeholders |

Never synced: `.credentials.json`, `history.jsonl`, `sessions/`, `projects/`,
logs, caches, plugin binaries, `skills/.trash/` (deleted skills) and
`skills/synced/` (the claude.ai skill cache, re-synced on login). Plugins reinstall themselves on a new machine
because `settings.json` carries `enabledPlugins` and `extraKnownMarketplaces`.

## Update the repo (this machine -> repo)

```sh
python3 claude/sync.py export
```

Secret-scans the source (`~/.claude` plus the redacted MCP export built in
memory) *before* anything is written, then copies the allowlisted files into
`claude/home/` and redacts MCP secrets, then scans the merged repo payload
again (it can also pick up servers merged in from another machine). Exits
non-zero and tells you not to commit if anything credential-shaped is found
at either stage; a failed pre-write scan leaves `claude/home/` untouched.
`settings.json` is also rewritten on export to drop the top-level `autoMode`
key (it can carry sensitive prose about private repos/orgs); the live
`~/.claude/settings.json` is never modified.

Machine paths are normalized on the way out. Claude Code rewrites the JSON
config with absolute paths as you use it, e.g. a hook command becomes
`'/home/you/.claude/hooks/x.sh'` and a deny rule becomes
`Read(//home/you/.ssh/**)`. Exported verbatim those pin the repo to one
machine: on another install the hook path does not exist and the deny rules
stop matching, so `~/.ssh` and friends become readable again. Export rewrites
the home prefix back to `~` in `settings.json`, `settings.local.json` and
`keybindings.json` (Claude Code expands `~` in permission rules, the shell
expands it in hook commands). Any other exported file that still mentions the
home directory is reported as a warning rather than rewritten.

Export never deletes from the repo on its own, since a file only in the
repo may have come from another machine's export. It lists repo files that
no longer exist in `~/.claude`; when this machine is the source of truth,
`python3 claude/sync.py export --prune` deletes them (git keeps history).

Symlinked skill dirs (what `npx skills add` creates, pointing into
`~/.agents/skills`) are followed, so their contents land in the repo as
real files.

## Install (repo -> machine, new or existing)

```sh
python3 claude/sync.py diff      # preview first
python3 claude/sync.py install
```

- New or changed repo files are copied in. Everything replaced or removed
  is backed up to `~/.claude/backups/dotfiles-sync-<timestamp>/` first.
- `settings.json` and `settings.local.json` are deep-merged into the live
  files, not overwritten: the repo wins on keys both define, keys only the
  machine has (a work proxy's `env`, extra plugins, `autoMode`) survive,
  and `permissions.allow/deny/ask` plus each `hooks.<Event>` list
  accumulate (local-only entries kept, no duplicates). A key deleted from the repo
  is therefore not deleted locally; remove it by hand.
- Files the repo has dropped are removed from `~/.claude`, but only when
  their bytes match a version the checked-out branch's git history shows
  at that path (the repo put them there; other branches and stashes don't
  count). Files that never came from the repo, or were edited after
  install, are kept and listed under "Local-only files". Needs git; without
  it nothing is removed.
- Symlinks inside `~/.claude/<dir>` are never written or deleted through.
  A linked skill that needs updating becomes a real copy (its target is
  left alone), a dangling link is replaced, and a linked skill the repo
  dropped is removed as a link only. A whole synced dir that is itself a
  link (stow-style) is written through as intended. Link changes are logged
  to `symlinks.txt` in the backup dir.
- MCP servers are merged into `~/.claude.json`; servers already configured
  locally are left untouched.
- The install ends with a `POST-INSTALL REMINDERS` block: secrets still
  holding a `<REDACTED:KEY>` placeholder, local-only files, the
  `claude plugin marketplace add` / `claude plugin install` commands for
  enabled plugins that are not installed yet (printed, not run), and the
  notes kept in [reminders.md](reminders.md).

## Tests

```sh
uvx pytest claude/test_sync.py
```

## Standalone secret scan

```sh
python3 claude/sync.py scan
```
