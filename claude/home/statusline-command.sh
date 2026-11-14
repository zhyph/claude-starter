#!/usr/bin/env bash
# Claude Code statusline: directory, git branch/status, model, context usage, rate limits, cost, lines changed
# Glyphs are all single-width (no East Asian "ambiguous" chars) so columns line up in Windows Terminal.
input=$(cat)

IFS=$'\t' read -r model dir ctx_pct cost added removed five week < <(
  echo "$input" | jq -r '[
    (.model.display_name // "Claude"),
    (.workspace.current_dir // .cwd // ""),
    (.context_window.used_percentage // "-"),
    (.cost.total_cost_usd // 0),
    (.cost.total_lines_added // 0),
    (.cost.total_lines_removed // 0),
    (.rate_limits.five_hour.used_percentage // "-"),
    (.rate_limits.seven_day.used_percentage // "-")
  ] | @tsv' | tr -d '\r'
)

R=$'\033[0m'; B=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RED=$'\033[31m'; MAG=$'\033[35m'; BLUE=$'\033[94m'
SEP="  ${DIM}|${R}  "

color_for() {
  if [ "$1" -ge 80 ]; then printf '%s' "$RED"
  elif [ "$1" -ge 50 ]; then printf '%s' "$YELLOW"
  else printf '%s' "$GREEN"; fi
}

pct() { printf '%s%%' "$1"; }

dir_name=$(basename "$dir")

branch=""; dirty=""
if git -C "$dir" --no-optional-locks rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git -C "$dir" --no-optional-locks branch --show-current 2>/dev/null)
  [ -z "$branch" ] && branch=$(git -C "$dir" --no-optional-locks rev-parse --short HEAD 2>/dev/null)
  [ -n "$(git -C "$dir" --no-optional-locks status --porcelain 2>/dev/null)" ] && dirty="${YELLOW}✱${R}"
fi

out="${B}${CYAN}${dir_name}${R}"
[ -n "$branch" ] && out+=" ${MAG}⎇ ${branch}${R}${dirty:+ $dirty}"
out+="${SEP}${BLUE}${model}${R}"

if [ "$ctx_pct" != "-" ]; then
  ctx=${ctx_pct%.*}; ctx=${ctx:-0}
  c=$(color_for "$ctx")
  filled=$(( (ctx + 5) / 10 )); [ "$filled" -gt 10 ] && filled=10
  # Bar is background-colored spaces: exactly one cell each in every font, no glyph fallback
  bg=$(( ${c:2:2} + 10 ))
  printf -v on  '%*s' "$filled" ''
  printf -v off '%*s' "$(( 10 - filled ))" ''
  bar=$'\033['"${bg}m${on}"$'\033[48;5;237m'"${off}${R}"
  out+="${SEP}${bar} ${c}$(pct "$ctx")${R}"
fi

limits=""
if [ "$five" != "-" ]; then
  p=$(printf '%.0f' "$five"); limits+="${DIM}5h${R} $(color_for "$p")$(pct "$p")${R}"
fi
if [ "$week" != "-" ]; then
  p=$(printf '%.0f' "$week"); [ -n "$limits" ] && limits+="  "
  limits+="${DIM}7d${R} $(color_for "$p")$(pct "$p")${R}"
fi
[ -n "$limits" ] && out+="${SEP}${limits}"

if [ "$cost" != "0" ] && [ -n "$cost" ]; then
  out+="${SEP}$(printf '$%.2f' "$cost")"
fi

if [ "$added" != "0" ] || [ "$removed" != "0" ]; then
  out+="${SEP}${GREEN}+${added}${R} ${RED}-${removed}${R}"
fi

printf '%s\n' "$out"
