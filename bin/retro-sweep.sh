#!/bin/zsh
# Weekly retro sweep. Clusters failures across all Claude Code transcripts since the
# last sweep and applies structural fixes (hooks/skills/CLAUDE.md) or memory gotchas.
# See skills/retro/SKILL.md (sweep mode).
#
# Run by hand, or schedule it (optional): launchd/com.example.retro-sweep.plist on
# macOS, or a cron line such as `0 9 * * 6 $HOME/bin/retro-sweep.sh` elsewhere.
#
# Config (env vars, all optional):
#   CLAUDE_BIN         claude binary (default: `command -v claude`, then ~/.local/bin/claude)
#   RETRO_LOG_DIR      where logs and pre-step tables go (default ~/Library/Logs/retro-sweep
#                      on macOS, ~/.local/state/retro-sweep elsewhere)
#   RETRO_SIGNALS_DIR  path to claude-oak-agents/scripts for the false-completion and
#                      drift pre-steps (https://github.com/robertmnyborg/claude-oak-agents)
#   RETRO_NPX          npx binary for the ccusage cost pre-step (default: `command -v npx`)

set -e
# launchd and cron start with a minimal PATH; add the usual install locations.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if [[ "$(uname)" == Darwin ]]; then DEFAULT_LOG="$HOME/Library/Logs/retro-sweep"; else DEFAULT_LOG="$HOME/.local/state/retro-sweep"; fi
LOG_DIR="${RETRO_LOG_DIR:-$DEFAULT_LOG}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/sweep-$(date +%Y-%m-%d).log"
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"

notify() {
  echo "$1 $(date)" >> "$LOG"
  command -v osascript >/dev/null && osascript -e "display notification \"$1\" with title \"retro-sweep\"" 2>/dev/null || true
}

# Once-per-day guard: a manual run and the scheduled fire must not both execute.
STAMP="$LOG_DIR/sweep-$(date +%Y-%m-%d).started"
if [[ -f "$STAMP" ]]; then
  echo "sweep already started today ($(date)); skipping" >> "$LOG"
  exit 0
fi
touch "$STAMP"

# Window comes from the WATERMARK, not a hardcoded 7 days. Provenance: one weekly run
# died mid-response, the watermark stalled, and the next sweep had a 14-day window but a
# 7-day cost file. Every session in the missed week was invisible to the cost check.
# Deriving the window from the watermark makes a missed sweep self-heal.
WATERMARK="$HOME/.claude/.retro-sweep-last"
WM=$(cat "$WATERMARK" 2>/dev/null || true)
to_ymd() {  # ISO-8601 UTC -> YYYYMMDD, BSD or GNU date
  date -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%Y%m%d 2>/dev/null || date -d "$1" +%Y%m%d 2>/dev/null
}
SINCE=$( [[ -n "$WM" ]] && to_ymd "$WM" || date -v-7d +%Y%m%d 2>/dev/null || date -d '7 days ago' +%Y%m%d )
# `--until` is EXCLUSIVE of the named day: passing today drops today's spend entirely
# (measured: three sessions, 11% of the window, vanished). Always ask for tomorrow.
UNTIL=$(date -v+1d +%Y%m%d 2>/dev/null || date -d tomorrow +%Y%m%d)
echo "window: $SINCE -> $UNTIL (from watermark)" >> "$LOG"

# Cost pre-step: per-session token spend (free, local; parses ~/.claude/projects JSONLs).
# Failure must not kill the sweep (set -e), hence || true; stderr goes to the log so the
# next failure is visible. Provenance: the step once wrote 0 bytes for weeks because
# `npx` could not find `node` on launchd's PATH and the error was discarded.
NPX="${RETRO_NPX:-$(command -v npx || true)}"
COST_JSON="$LOG_DIR/ccusage-week.json"
if [[ -n "$NPX" ]]; then
  "$NPX" -y ccusage@latest session --since "$SINCE" --until "$UNTIL" --json > "$COST_JSON" 2>>"$LOG" || true
else
  echo "npx not found; skipping cost pre-step" >> "$LOG"
fi

# Optional deterministic pre-steps (stdlib python, no LLM): unverified done/fixed claims a
# user contradicted within 24h, and reprompt / path-churn signals.
if [[ -n "${RETRO_SIGNALS_DIR:-}" && -d "$RETRO_SIGNALS_DIR" ]]; then
  WIN=( --days 7 ); [[ -n "$WM" ]] && WIN=( --since "$WM" )
  python3 "$RETRO_SIGNALS_DIR/false_completion.py" "${WIN[@]}" --out "$LOG_DIR/false-completions-week.md" >> "$LOG" 2>&1 || true
  python3 "$RETRO_SIGNALS_DIR/drift_signals.py" "${WIN[@]}" --out "$LOG_DIR/drift-signals-week.md" >> "$LOG" 2>&1 || true
fi

PROMPT="Run the retro skill in SWEEP mode (/retro sweep): read ~/.claude/.retro-sweep-last for the window (default 7 days), scan all transcript JSONLs under ~/.claude/projects/*/ modified since then, cluster failure signals ACROSS sessions (doom loops, contradicted claims, repeat user corrections, dead ends), and disposition each cluster structural-fix-first (edit the hook/skill/CLAUDE.md so it cannot recur) with a memory gotcha only as fallback. If $LOG_DIR/false-completions-week.md or $LOG_DIR/drift-signals-week.md exist, START from them (retro SKILL.md sweep step 2): for drift rows, read the assistant turn just before each flagged prompt to name WHY the first answer missed, and cluster on that cause, not on topic. Run the sweep-mode COST CHECK using $COST_JSON (sweep step 4). Then write the new timestamp to ~/.claude/.retro-sweep-last and finish with a report stating for each cluster: new or repeat from last sweep, and the fix applied, plus the cost summary line."

# acceptEdits lets it write memory/skill files headlessly.
BEFORE="${WM:-none}"
set +e
"$CLAUDE" -p "$PROMPT" \
  --permission-mode acceptEdits \
  --allowedTools "Bash" "Read" "Grep" "Glob" "Write" "Edit" \
  >> "$LOG" 2>&1
RC=$?
set -e

# Silent-failure guard. Provenance: one run died on "API Error: Connection closed
# mid-response" after a single turn, and nothing noticed. The watermark never advanced
# and the loop silently did nothing for a full week. The watermark IS the closing
# signal, so verify it moved rather than trusting the exit code.
AFTER=$(cat "$WATERMARK" 2>/dev/null || echo "none")
if [[ "$RC" -ne 0 || "$AFTER" == "$BEFORE" ]]; then
  notify "retro sweep FAILED (rc=$RC, watermark $BEFORE -> $AFTER). Re-run: $0"
  rm -f "$STAMP"   # clear the once-per-day guard so a re-run today is allowed
  exit 1
fi

# Land whatever the sweep staged instead of applying. Provenance: three consecutive
# sweeps wrote their primary fix into proposed-<date>/ and none was ever applied. The
# agent can be blocked from Edit/Write under ~/.claude even with acceptEdits; this shell
# is not, so it is the applier of last resort. Failure here must not fail the sweep.
STAGED="$LOG_DIR/proposed-$(date +%Y-%m-%d)/APPLY.sh"
if [[ -f "$STAGED" ]]; then
  echo "applying staged fixes: $STAGED" >> "$LOG"
  if zsh "$STAGED" >> "$LOG" 2>&1; then
    mv "$STAGED" "${STAGED}.applied"
    echo "staged fixes applied $(date)" >> "$LOG"
  else
    notify "retro sweep: staged fixes FAILED to apply. See $LOG"
  fi
fi

echo "retro sweep finished $(date)" >> "$LOG"
