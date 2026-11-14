# Claude Code starter config

A working Claude Code setup to read, try and modify. Nothing here is required;
delete what you do not want.

- `claude/home/` mirrors `~/.claude`: `CLAUDE.md` (global instructions),
  `settings.json`, `rules/`, `skills/`, `agents/`, `hooks/`, `statusline-command.sh`.
- `claude/mcp-servers.json` lists MCP servers, secrets replaced by placeholders.
- `claude/sync.py` copies this setup onto a machine. See `claude/README.md`.

Install: `python3 claude/sync.py install`, then read `claude/reminders.md`.

Things to review first, they reflect one person's habits:
- `claude/home/CLAUDE.md`: tooling list assumes tools like `ast-grep`, `uv`, `biome`,
  `gitleaks`. Remove what you do not use.
- `settings.json`: model, plugins, hooks (`biome-format`, `stop-typecheck`, `stop-vitest`).
- Many skills are stack-specific (Prisma, Supabase, React Native, Unreal). Delete the ones you will not use.
