#!/bin/bash
# bulk-read-guard-bash — pega `cat/less/more arquivo` que despeja arquivo
# inteiro no contexto. head/tail/sed -n/grep são cirúrgicos e passam.
# Comandos com pipe ou redirecionamento também passam.

MIN_LINES="${BULK_READ_MIN_LINES:-500}"
case "$MIN_LINES" in ''|*[!0-9]*) MIN_LINES=500 ;; esac

input=$(cat)
[ -n "$BULK_READ_DEBUG_LOG" ] && printf '%s\n' "$input" >> "$BULK_READ_DEBUG_LOG"

# o hook roda dentro de subagentes também; o bulk-reader precisa ler inteiro
agent_type=$(printf '%s' "$input" | jq -r '.agent_type // empty')
[ "$agent_type" = "bulk-reader" ] && exit 0

cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0
printf '%s' "$cmd" | grep -qE '[|>]' && exit 0
printf '%s' "$cmd" | grep -qE '^(cat|less|more) ' || exit 0

args=$(printf '%s' "$cmd" | sed -E 's/^(cat|less|more) +//')
file_path=""
for arg in $args; do
  case "$arg" in
    -*) continue ;;
    *)  file_path=$(printf '%s' "$arg" | tr -d '"'"'"); break ;;
  esac
done
file_path="${file_path/#\~/$HOME}"
[ -z "$file_path" ] || [ ! -f "$file_path" ] && exit 0

lines=$(wc -l < "$file_path" 2>/dev/null | tr -d ' ')
[ -z "$lines" ] && exit 0
[ "$lines" -le "$MIN_LINES" ] && exit 0

reason="bulk-read-guard: $file_path has $lines lines (limit $MIN_LINES). Don't cat it whole. Either delegate to the bulk-reader subagent (Agent tool, subagent_type \"bulk-reader\", pass path + precise question) or read a targeted range with sed -n 'A,Bp' / grep -n."
jq -n --arg r "$reason" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
