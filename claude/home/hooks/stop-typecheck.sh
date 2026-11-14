#!/bin/bash
# Stop: run tsc --noEmit when .ts/.tsx files changed since the last clean check in this session.
in=$(cat)
# Already continuing because of this hook: let Claude stop instead of looping.
[ "$(jq -r '.stop_hook_active' <<<"$in")" = true ] && exit 0
cwd=$(jq -r '.cwd' <<<"$in")
sid=$(jq -r '.session_id' <<<"$in")
cd "$cwd" 2>/dev/null || exit 0
[ -f tsconfig.json ] && [ -x node_modules/.bin/tsc ] || exit 0

changed=$(git ls-files -m -o --exclude-standard -- '*.ts' '*.tsx' 2>/dev/null)
[ -n "$changed" ] || exit 0
stamp="${XDG_RUNTIME_DIR:-/tmp}/claude-tsc-$sid"
if [ -f "$stamp" ]; then
	newer=$(while IFS= read -r f; do [ "$f" -nt "$stamp" ] && echo "$f"; done <<<"$changed")
	[ -n "$newer" ] || exit 0
fi

if out=$(node_modules/.bin/tsc --noEmit -p tsconfig.json 2>&1); then
	touch "$stamp"
	exit 0
fi
printf 'tsc --noEmit failed, fix before finishing:\n%s\n' "$(head -40 <<<"$out")" >&2
exit 2
