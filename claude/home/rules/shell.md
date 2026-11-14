---
paths:
  - "**/*.sh"
  - "**/*.bash"
  - "**/*.zsh"
---

- Shell/Infra: POSIX-compatible when possible. Use shellcheck conventions.
- After shell edits: `shfmt -d` and `shellcheck`.
