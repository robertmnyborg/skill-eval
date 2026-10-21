#!/usr/bin/env bash
# skill-eval — A/B test a skill: run task scenarios WITH vs WITHOUT the skill in
# context, judge each output, report the pass-rate delta.
#
# Implements the WorkOS/Nick Nisi "measure, don't assume" test: a skill that
# scores 77% loaded vs 97% unloaded is making the model WORSE. You only know by
# measuring. This is that measurement.
#
# Usage:
#   run-eval.sh <suite.json> [--runs N] [--model M] [--judge-model M] [--keep]
#
# Suite JSON schema:
#   {
#     "skill": "stop-slop",                             # label only
#     "skill_path": "~/.claude/skills/stop-slop/SKILL.md", # injected in WITH arm;
#                                                       # ~ expands, relative = suite dir
#     "model": "sonnet",                                # task model (overridable)
#     "judge_model": "sonnet",                          # grader model
#     "runs_per_arm": 3,                                # repeats per arm (noise)
#     "scenarios": [
#       { "id": "s1", "prompt": "the task", "rubric": "PASS if ... FAIL if ..." }
#     ]
#   }
set -uo pipefail

SUITE="${1:?usage: run-eval.sh <suite.json> [--runs N] [--model M] [--judge-model M] [--keep]}"
shift || true
RUNS_OVERRIDE=""; MODEL_OVERRIDE=""; JUDGE_OVERRIDE=""; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --runs) RUNS_OVERRIDE="$2"; shift 2 ;;
    --model) MODEL_OVERRIDE="$2"; shift 2 ;;
    --judge-model) JUDGE_OVERRIDE="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
[ -x "$CLAUDE" ] || { echo "claude CLI required (set CLAUDE_BIN)" >&2; exit 1; }
[ -f "$SUITE" ] || { echo "suite not found: $SUITE" >&2; exit 1; }

SKILL=$(jq -r '.skill // "unknown"' "$SUITE")
SKILL_PATH=$(jq -r '.skill_path // ""' "$SUITE")
MODEL="${MODEL_OVERRIDE:-$(jq -r '.model // "sonnet"' "$SUITE")}"
JUDGE_MODEL="${JUDGE_OVERRIDE:-$(jq -r '.judge_model // .model // "sonnet"' "$SUITE")}"
RUNS="${RUNS_OVERRIDE:-$(jq -r '.runs_per_arm // 3' "$SUITE")}"
# skill_path: expand a leading ~, and resolve a relative path against the suite's directory.
SKILL_PATH="${SKILL_PATH/#\~/$HOME}"
case "$SKILL_PATH" in /*|"") ;; *) SKILL_PATH="$(cd "$(dirname "$SUITE")" && pwd)/$SKILL_PATH" ;; esac
[ -n "$SKILL_PATH" ] && [ -f "$SKILL_PATH" ] || { echo "skill_path missing/unreadable: $SKILL_PATH" >&2; exit 1; }
SKILL_BODY=$(cat "$SKILL_PATH")

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP_SRC="${SKILL_EVAL_STAMP:-$(date +%Y%m%d-%H%M%S)}"   # override to name the result dir
OUTDIR="$HERE/results/${SKILL}-${STAMP_SRC}"
mkdir -p "$OUTDIR"
RAW="$OUTDIR/raw.jsonl"; : > "$RAW"

echo "skill=$SKILL  model=$MODEL  judge=$JUDGE_MODEL  runs/arm=$RUNS" >&2
echo "skill_path=$SKILL_PATH ($(wc -l < "$SKILL_PATH") lines)" >&2

# --- isolation -----------------------------------------------------------------
# Measured 2026-08-30: without this, `claude -p` inherits the ambient CLAUDE.md,
# user memory, MCP servers, and file tools from wherever the script runs. A probe
# confirmed the task model answered YES/YES/YES to "are these two rules loaded"
# and "do you have file tools" while grading a suite that was A/B-ing exactly
# those rules. The control arm had the treatment. Set SKILL_EVAL_ISOLATE=0 to
# restore the old inheriting behavior for a suite that deliberately wants it.
ISOLATE="${SKILL_EVAL_ISOLATE:-1}"
ISO_ARGS=()
ISO_CWD="$PWD"
if [ "$ISOLATE" = "1" ]; then
  ISO_CWD=$(mktemp -d "${TMPDIR:-/tmp}/skill-eval-sandbox.XXXXXX")
  # NOTE: --disallowed-tools is variadic and swallows a positional prompt, so every
  # call below feeds the prompt on STDIN. Keep it that way.
  # disableAllHooks (2026-10-09): user settings still load for auth/model, and with
  # them the user's Stop hooks. One Stop hook replaced 2 of 3 control-arm drafts in
  # a writing-rule eval with hook chatter, inflating the delta. --bare would also
  # skip hooks but drops OAuth.
  ISO_ARGS=(--strict-mcp-config --setting-sources user --settings '{"disableAllHooks":true}'
            --disallowed-tools Read Write Edit Bash Grep Glob WebFetch WebSearch Task Skill)
  echo "isolation: ON (cwd=$ISO_CWD, no settings/CLAUDE.md, no MCP, no tools)" >&2
else
  echo "isolation: OFF — task model inherits ambient CLAUDE.md, memory, MCP and tools" >&2
fi

# Run one task arm. $1=with|without  $2=prompt  -> prints model's text result
run_arm() {
  local arm="$1" prompt="$2" out
  if [ "$arm" = "with" ]; then
    out=$(cd "$ISO_CWD" && printf '%s' "$prompt" | "$CLAUDE" -p --model "$MODEL" --output-format json \
          --append-system-prompt "The full text of a skill is reproduced below. Apply it to this task.

IMPORTANT: this skill is NOT registered with the Skill tool in this run. Do not call
Skill(...) for it and do not try to Read its file — both will fail. Everything it
contains is already inline below; just follow it.

$SKILL_BODY" "${ISO_ARGS[@]}" 2>/dev/null)
  else
    out=$(cd "$ISO_CWD" && printf '%s' "$prompt" | "$CLAUDE" -p --model "$MODEL" --output-format json "${ISO_ARGS[@]}" 2>/dev/null)
  fi
  jq -r '.result // .text // empty' <<<"$out" 2>/dev/null
}

# Judge one output. $1=rubric $2=response $3=task prompt -> prints "VERDICT<TAB>reason".
# An unparsable or empty judge reply is JUDGE_ERROR, never FAIL: counting it as FAIL
# biases both arms toward FAIL by an unmeasured amount.
judge() {
  local rubric="$1" resp="$2" prompt="$3" jp out
  jp="You are a strict grader. Apply this rubric and reply with ONLY a JSON object {\"verdict\":\"PASS\"|\"FAIL\",\"reason\":\"<one line>\"}.
Grade only against the rubric. The TASK GIVEN is provided so you can check the response against its source material; do not grade the task itself.

TASK GIVEN:
$prompt

RUBRIC:
$rubric

RESPONSE TO GRADE:
$resp"
  out=$(cd "$ISO_CWD" && printf '%s' "$jp" | "$CLAUDE" -p --model "$JUDGE_MODEL" --output-format json "${ISO_ARGS[@]}" 2>/dev/null | jq -r '.result // empty')
  local v reason
  v=$(grep -oiE '"verdict"\s*:\s*"(PASS|FAIL)"' <<<"$out" | grep -oiE 'PASS|FAIL' | head -1 | tr a-z A-Z)
  [ -z "$v" ] && v=$(grep -oiE '\b(PASS|FAIL)\b' <<<"$out" | head -1 | tr a-z A-Z)
  reason=$(grep -oE '"reason"\s*:\s*"[^"]*"' <<<"$out" | head -1 | sed -E 's/^"reason"\s*:\s*"//; s/"$//')
  [ -z "$v" ] && reason="unparsable judge output: ${out:0:200}"
  printf '%s\t%s' "${v:-JUDGE_ERROR}" "$reason" | tr '\n' ' '
}

N=$(jq '.scenarios | length' "$SUITE")
echo "scenarios=$N  -> $((N*2*RUNS)) task calls + judges" >&2

for i in $(seq 0 $((N-1))); do
  SID=$(jq -r ".scenarios[$i].id // \"s$i\"" "$SUITE")
  PROMPT=$(jq -r ".scenarios[$i].prompt" "$SUITE")
  RUBRIC=$(jq -r ".scenarios[$i].rubric" "$SUITE")
  for arm in with without; do
    for r in $(seq 1 "$RUNS"); do
      echo "  [$SID] $arm run $r/$RUNS..." >&2
      RESP=$(run_arm "$arm" "$PROMPT")
      REASON=""
      case "$RESP" in
        "") V="ERROR" ;;
        "API Error"*|"Execution error"*|"Credit balance"*) V="ERROR"; echo "    -> harness error, excluded: ${RESP:0:60}" >&2 ;;
        *) JR=$(judge "$RUBRIC" "$RESP" "$PROMPT"); V="${JR%%$'\t'*}"; REASON="${JR#*$'\t'}"
           [ "$V" = "JUDGE_ERROR" ] && echo "    -> judge error, excluded: ${REASON:0:60}" >&2 ;;
      esac
      jq -nc --arg sid "$SID" --arg arm "$arm" --argjson run "$r" \
             --arg verdict "$V" --arg reason "$REASON" --arg resp "${RESP:0:4000}" \
             '{sid:$sid,arm:$arm,run:$run,verdict:$verdict,reason:$reason,response:$resp}' >> "$RAW"
    done
  done
done

# Aggregate
REPORT="$OUTDIR/report.md"
{
  echo "# skill-eval: \`$SKILL\`"
  echo
  echo "- skill_path: \`$SKILL_PATH\` ($(wc -l < "$SKILL_PATH") lines)"
  echo "- model: \`$MODEL\` · judge: \`$JUDGE_MODEL\` · runs/arm: $RUNS"
  echo
  echo "| scenario | with-skill | without-skill | delta | verdict |"
  echo "|---|---|---|---|---|"
} > "$REPORT"

# bash evaluates both ternary branches, so $(( n>0 ? 100*p/n : 0 )) still divides by zero
pct() { if [ "$2" -gt 0 ]; then echo $(( 100*$1/$2 )); else echo 0; fi; }
TOT_W_P=0; TOT_W_N=0; TOT_O_P=0; TOT_O_N=0
for i in $(seq 0 $((N-1))); do
  SID=$(jq -r ".scenarios[$i].id // \"s$i\"" "$SUITE")
  wp=$(jq -r --arg s "$SID" 'select(.sid==$s and .arm=="with"  and .verdict=="PASS")|1' "$RAW" | wc -l | tr -d ' ')
  wn=$(jq -r --arg s "$SID" 'select(.sid==$s and .arm=="with"  and (.verdict=="PASS" or .verdict=="FAIL"))|1' "$RAW" | wc -l | tr -d ' ')
  op=$(jq -r --arg s "$SID" 'select(.sid==$s and .arm=="without" and .verdict=="PASS")|1' "$RAW" | wc -l | tr -d ' ')
  on=$(jq -r --arg s "$SID" 'select(.sid==$s and .arm=="without" and (.verdict=="PASS" or .verdict=="FAIL"))|1' "$RAW" | wc -l | tr -d ' ')
  wpct=$(pct "$wp" "$wn"); opct=$(pct "$op" "$on"); d=$(( wpct-opct ))
  TOT_W_P=$((TOT_W_P+wp)); TOT_W_N=$((TOT_W_N+wn)); TOT_O_P=$((TOT_O_P+op)); TOT_O_N=$((TOT_O_N+on))
  sign=""; [ "$d" -gt 0 ] && sign="+"
  vd="neutral"; [ "$d" -ge 10 ] && vd="helps ✅"; [ "$d" -le -10 ] && vd="HURTS ❌"; { [ "$wn" -eq 0 ] || [ "$on" -eq 0 ]; } && vd="no data (all runs excluded)"
  printf '| %s | %d%% (%d/%d) | %d%% (%d/%d) | %s%d pts | %s |\n' \
    "$SID" "$wpct" "$wp" "$wn" "$opct" "$op" "$on" "$sign" "$d" "$vd" >> "$REPORT"
done

TW=$(pct "$TOT_W_P" "$TOT_W_N")
TO=$(pct "$TOT_O_P" "$TOT_O_N")
TD=$(( TW-TO )); tsign=""; [ "$TD" -gt 0 ] && tsign="+"
TVERDICT="NEUTRAL — skill makes no measurable difference; consider deleting to save tokens."
[ "$TD" -ge 10 ] && TVERDICT="KEEP — skill measurably helps (+${TD} pts)."
[ "$TD" -le -10 ] && TVERDICT="DELETE/REWRITE — skill makes the model WORSE (${TD} pts). This is the 77-vs-97 case."
{ [ "$TOT_W_N" -eq 0 ] || [ "$TOT_O_N" -eq 0 ]; } && TVERDICT="NO DATA — every run in one arm was excluded; see the error counts below."
{
  echo
  printf '**Overall: with %d%% vs without %d%% → %s%d pts**\n\n' "$TW" "$TO" "$tsign" "$TD"
  echo "**Verdict: $TVERDICT**"
  echo
  ERRN=$(jq -r 'select(.verdict=="ERROR")|1' "$RAW" | wc -l | tr -d ' ')
  [ "$ERRN" -gt 0 ] && echo "_$ERRN run(s) failed in the harness (empty or API error) and are excluded from both numerator and denominator._" && echo
  JERRN=$(jq -r 'select(.verdict=="JUDGE_ERROR")|1' "$RAW" | wc -l | tr -d ' ')
  [ "$JERRN" -gt 0 ] && echo "_$JERRN judge reply(ies) were unparsable and are excluded from both numerator and denominator (see \`reason\` in raw.jsonl)._" && echo
  # Judge calibration: TPR/TNR from hand labels (calibrate-judge.py score). Without it the
  # verdict rests on a judge whose error rate nobody has measured.
  CAL="$HERE/results/judge-calibration.json"
  if [ -f "$CAL" ] && [ "$(jq -r '.judge_model' "$CAL")" = "$JUDGE_MODEL" ]; then
    python3 "$HERE/calibrate-judge.py" correct "$TW" "$TO"
  else
    echo "_Judge uncalibrated for \`$JUDGE_MODEL\`: TPR/TNR unknown, so a delta inside ±10 pts may be judge error. Run \`calibrate-judge.py sample\` then \`score\`._"
    echo
  fi
  echo "_Caveat: WITH-arm injects the SKILL.md body as a system prompt; the WITHOUT arm omits it. If the skill's description also auto-triggers on the task prompt, the without arm may be contaminated — pick task prompts that don't name the skill's domain verbatim._"
} >> "$REPORT"

[ "$KEEP" = 0 ] && echo "(raw transcripts kept at $RAW; pass --keep is default-on for raw)" >&2
echo "$REPORT"
