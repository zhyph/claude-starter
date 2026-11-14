---
paths:
  - "**/*.py"
  - "**/pyproject.toml"
---

- Python: type hints always. pytest for testing.
- After Python edits: `ruff check --fix`, then `ruff format`. Never assert clean without running them.
