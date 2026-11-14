#!/bin/sh
# PostToolUse: run Biome on an edited file, only inside projects that have a Biome config.
f=$(jq -r '.tool_input.file_path // empty')
case "$f" in
*.js | *.jsx | *.ts | *.tsx | *.mjs | *.cjs | *.json | *.jsonc | *.css) ;;
*) exit 0 ;;
esac
[ -f "$f" ] || exit 0
d=$(dirname "$f")
while [ "$d" != / ] && [ "$d" != . ]; do
	if [ -f "$d/biome.json" ] || [ -f "$d/biome.jsonc" ]; then
		bin="$d/node_modules/.bin/biome"
		[ -x "$bin" ] || bin=biome
		# exit 2 on PostToolUse does not undo the edit; it feeds the remaining diagnostics back to Claude
		out=$(cd "$d" && "$bin" check --write --no-errors-on-unmatched --files-ignore-unknown=true "$f" 2>&1) || {
			printf '%s\n' "$out" | head -40 >&2
			exit 2
		}
		exit 0
	fi
	d=$(dirname "$d")
done
exit 0
