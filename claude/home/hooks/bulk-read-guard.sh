#!/bin/bash
# bulk-read-guard — bloqueia Read de arquivo INTEIRO acima de N linhas e manda
# delegar pro subagente bulk-reader (contexto descartável, modelo barato).
# Leituras com offset/limit passam direto: já são cirúrgicas.
# Ajuste o limite com BULK_READ_MIN_LINES no bloco "env" do settings.json.

MIN_LINES="${BULK_READ_MIN_LINES:-500}"
case "$MIN_LINES" in ''|*[!0-9]*) MIN_LINES=500 ;; esac

input=$(cat)
[ -n "$BULK_READ_DEBUG_LOG" ] && printf '%s\n' "$input" >> "$BULK_READ_DEBUG_LOG"

# o hook roda dentro de subagentes também; o bulk-reader precisa ler inteiro
agent_type=$(printf '%s' "$input" | jq -r '.agent_type // empty')
[ "$agent_type" = "bulk-reader" ] && exit 0

file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
offset=$(printf '%s' "$input" | jq -r '.tool_input.offset // empty')
limit=$(printf '%s' "$input" | jq -r '.tool_input.limit // empty')

# leitura cirúrgica: o modelo já sabe o que quer
if [ -n "$offset" ] || [ -n "$limit" ]; then exit 0; fi
# caminho vazio ou inexistente: deixa o Read falhar sozinho
if [ -z "$file_path" ] || [ ! -f "$file_path" ]; then exit 0; fi
# binários / notebooks: wc -l não significa nada
case "${file_path##*.}" in
  pdf|png|jpg|jpeg|gif|webp|svg|ipynb|PDF|PNG|JPG|JPEG) exit 0 ;;
esac

lines=$(wc -l < "$file_path" 2>/dev/null | tr -d ' ')
[ -z "$lines" ] && exit 0
[ "$lines" -le "$MIN_LINES" ] && exit 0

reason="bulk-read-guard: $file_path has $lines lines (limit $MIN_LINES). Don't load it whole into context. Either (a) delegate: Agent tool with subagent_type \"bulk-reader\", passing the absolute path and the precise question you need answered; or (b) if you need exact text to edit, Grep for the symbol first and Read with offset/limit around it."
jq -n --arg r "$reason" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
