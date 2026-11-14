# Claude Code starter config

A working Claude Code setup to try, read and change. Nothing is sacred:
delete what you do not want. Everything here was copied from one person's
machine, so some choices reflect their habits (see "What to review").

## For the Claude reading this (setup instructions)

You are helping the user install this config onto their machine. Work through
these steps in order. Ask before installing system packages. Do not edit the
files under `claude/home/` before installing; customize afterwards.

1. **Check prerequisites.** Run `python3 --version`, `git --version`, `jq --version`,
   `gh --version`, `node --version`, `claude --version`. Python 3 and git are needed by
   the installer. `jq` is needed by the hooks and statusline. `gh` is needed by the
   GitHub MCP server. Node (for `npx`) is needed by most MCP servers. Install whatever
   is missing with the user's package manager (apt, brew, winget, etc.), after asking.
2. **Authenticate GitHub.** `gh auth status`; if not logged in, ask the user to run
   `gh auth login` themselves (it is interactive; in Claude Code they can type
   `! gh auth login`).
3. **Preview, then install.** From the repo root:
   ```sh
   python3 claude/sync.py diff
   python3 claude/sync.py install
   ```
   Install merges into the existing `~/.claude` and `~/.claude.json` instead of
   overwriting. Anything it replaces is backed up first to
   `~/.claude/backups/dotfiles-sync-<timestamp>/`. Existing local MCP servers are
   not touched. Details: [claude/README.md](claude/README.md).
4. **Read the `POST-INSTALL REMINDERS` block** printed at the end. It lists the exact
   `claude plugin marketplace add ...` and `claude plugin install ...` commands for
   the plugins that are not installed yet. They are printed, not run: run them
   (see the plugin table below for sources if you need them), or ask the user to
   run them in a normal terminal if they fail from inside a session.
5. **Context7 key (optional).** Get a free key at https://context7.com/dashboard and
   replace the `<REDACTED:CONTEXT7_API_KEY>` placeholder under
   `mcpServers.context7.headers` in `~/.claude.json`. Without it, context7 may be
   rate limited. Skip if the user does not want it, or remove the server.
6. **Optional tools.** The hooks only act when the tool exists, so none of these are
   required: `biome` (auto-format JS/TS), a project-local `tsc` (typecheck on stop),
   `ast-grep`, `uv`, `gitleaks`, `hyperfine`. `claude/home/CLAUDE.md` tells Claude to
   prefer them; if the user does not have them, remove the "Tooling" section.
7. **Restart Claude Code** so the settings, hooks and plugins load, then verify:
   run `/plugin` (plugins enabled), `/hooks`, `/mcp` (servers connect; a failing one
   can be removed from `~/.claude.json`), and confirm `~/.claude/CLAUDE.md` exists.
8. **Tell the user** what was installed, what was skipped, and that
   `~/.claude/backups/` holds the originals.

Troubleshooting: if a plugin or MCP server fails, disable it rather than debugging
it in the first session (see "Turning things off"). `python3 claude/sync.py install`
is safe to re-run.

## What is in here

- `claude/home/` mirrors `~/.claude`: `CLAUDE.md` (global instructions loaded in every
  session), `settings.json`, `rules/`, `skills/`, `agents/`, `hooks/`,
  `statusline-command.sh`.
- `claude/mcp-servers.json`: MCP servers, secrets replaced by placeholders:
  chrome-devtools, claude-design, claude-historian-mcp, context7, github, playwright.
- `claude/sync.py`: the installer (stdlib-only Python, Linux/macOS/Windows).

## Plugins that get installed

Enabled in `claude/home/settings.json` under `enabledPlugins`. Plugins can add
skills, agents, commands and hooks (scripts that run automatically, for example at
session start), so some behavior in a session comes from them, not from this repo.

| Plugin | Source | What it does |
| --- | --- | --- |
| superpowers | claude-plugins-official | Workflow skills (brainstorming, planning, debugging, TDD, code review). Injects a note at session start telling Claude to use them. |
| ponytail | github `DietrichGebert/ponytail` | "Lazy senior dev" mode: Claude prefers the smallest working change. Activated at session start. Say "stop ponytail" to turn off for a session. |
| ecc | git `https://github.com/affaan-m/ECC.git` | Large bundle of agents, skills and slash commands (reviewers, build fixers, planners). The biggest source of extra items in `/skills` and agent lists. |
| effortless | github `HeyCubit/effortless` | Adds a skill; options in `pluginConfigs`. |
| typesafe | github `typesafe-ai/skills` | Adds a skill. |
| pr-review-toolkit | claude-plugins-official | Agents for reviewing PRs (tests, comments, types, silent failures). |
| claude-security | claude-plugins-official | Security scan skill and agents. |
| pyright-lsp, typescript-lsp | claude-plugins-official | Code intelligence (go to definition, types) for Python and TypeScript. The TypeScript one needs `npm i -g typescript-language-server typescript`. |
| cc-plugin-you-should-know | builtin | Ships with Claude Code. |

## Hooks in settings.json

| Hook | When | Effect |
| --- | --- | --- |
| `bulk-read-guard.sh`, `bulk-read-guard-bash.sh` | before Read / Bash | Blocks reading very large files in one go (threshold `BULK_READ_MIN_LINES`, default 500 in `env`) and nudges toward ranges or the `bulk-reader` agent. |
| `biome-format.sh` | after Edit/Write | Runs biome on the edited file, only in projects that have a `biome.json`. |
| `stop-typecheck.sh` | when Claude stops | Runs `tsc` only in projects with a `tsconfig.json` and a local `node_modules/.bin/tsc`; feeds errors back to Claude. |

## Turning things off

- Plugin: set it to `false` in `enabledPlugins` (in `~/.claude/settings.json`), or `/plugin`.
- Hook: delete its block under `hooks` in `~/.claude/settings.json`.
- MCP server: `claude mcp remove <name>` or delete it from `~/.claude.json`.
- Skill or agent: delete its folder or file under `~/.claude/skills` or `~/.claude/agents`.
- Global instructions: edit `~/.claude/CLAUDE.md`.
- Note: re-running `install` re-adds keys that this repo defines. Edit the repo copy
  too if you want a removal to stick.

## What to review (one person's habits)

- `claude/home/CLAUDE.md`: terse-output rules, a "no emojis or em-dashes" rule, tooling
  and review-discipline preferences. Rewrite it to taste, this file shapes every session.
- `settings.json`: default model is `sonnet`, subagent model `sonnet`, effort `medium`,
  commit/PR attribution is blank (no "Co-authored-by Claude" line).
- Many skills are stack-specific (Prisma, Supabase, React Native, Unreal Engine,
  Ghidra). Delete the ones you will not use; they cost context in the skill list.
