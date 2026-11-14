#!/bin/bash
# Stop: run only the Vitest tests affected by files changed since the last green run in this session.
in=$(cat)
# Already continuing because of this hook: let Claude stop instead of looping.
[ "$(jq -r '.stop_hook_active' <<<"$in")" = true ] && exit 0
cwd=$(jq -r '.cwd' <<<"$in")
sid=$(jq -r '.session_id' <<<"$in")
cd "$cwd" 2>/dev/null || exit 0
[ -x node_modules/.bin/vitest ] || exit 0

changed=$(git ls-files -m -o --exclude-standard -- '*.ts' '*.tsx' '*.js' '*.jsx' 2>/dev/null)
[ -n "$changed" ] || exit 0
stamp="${XDG_RUNTIME_DIR:-/tmp}/claude-vitest-$sid"
if [ -f "$stamp" ]; then
	changed=$(while IFS= read -r f; do [ "$f" -nt "$stamp" ] && echo "$f"; done <<<"$changed")
	[ -n "$changed" ] || exit 0
fi

mapfile -t files <<<"$changed"
if out=$(node_modules/.bin/vitest related --run --passWithNoTests "${files[@]}" 2>&1); then
	touch "$stamp"
	exit 0
fi
printf 'vitest related failed, fix before finishing:\n%s\n' "$(tail -60 <<<"$out")" >&2
exit 2
