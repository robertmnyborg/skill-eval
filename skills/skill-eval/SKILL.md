---
name: skill-eval
description: A/B test whether a skill actually helps. Runs task scenarios WITH the skill in context vs WITHOUT, judges each output, and reports the pass-rate delta. Use when the user wants to evaluate a skill, measure if a skill helps or hurts, audit skill bloat, decide whether to keep/cut a skill, or asks "is this skill worth it / making things worse".
---

# skill-eval

Measure, don't assume. A skill can make the model **worse** — the canonical case is 77% pass rate with the skill loaded vs 97% without it (Nick Nisi, WorkOS). You only ever know by measuring. This skill is that measurement.

## The one mechanism

`run-eval.sh <suite.json>` runs each scenario twice — WITH the SKILL.md body injected as a system prompt, WITHOUT it — `runs_per_arm` times each, judges every output against the scenario's rubric, and prints a pass-rate delta table with a KEEP / DELETE / NEUTRAL verdict.

```bash
~/.claude/skills/skill-eval/run-eval.sh ~/.claude/skills/skill-eval/suites/<skill>.json --runs 3
```

Output: `results/<skill>-<stamp>/report.md` (path printed to stdout) + `raw.jsonl` (every transcript + verdict). Set `SKILL_EVAL_STAMP=<name>` to choose the result dir name; the default is a timestamp.

## Authoring a suite (the only real work)

A suite is JSON: `skill`, `skill_path` (the SKILL.md to A/B; `~` expands, a relative path resolves against the suite's folder), `model`, `judge_model`, `runs_per_arm`, and `scenarios[]`. Each scenario is `{id, prompt, rubric}`. See `suites/qualitative-rigor.json` (runs against a bundled example skill) and `suites/stop-slop.json`.

Gotchas — the ones that actually bite:

- **The rubric is the eval.** A vague rubric ("PASS if good") makes the judge noisy and the delta meaningless. Write rubrics as hard checks: "PASS only if the response (a) flags the sample size as too small to generalize AND (b) does not invent a percentage. FAIL otherwise."
- **Pick task prompts that DON'T name the skill's domain verbatim**, or the skill auto-triggers in the WITHOUT arm and contaminates the control. Eval the behavior, not the keyword.
- **3 runs/arm minimum.** Output is non-deterministic; a single run tells you nothing. Bump to 5+ when the delta is within ±10 pts (noise band).
- **Cost is N×2×runs task calls + that many judge calls.** 5 scenarios × 3 runs = 60 model calls. Default to `sonnet` for both task and judge; reserve opus for the task arm only when the skill targets hard reasoning.
- **Reasoning skills only, by default.** The harness gives the task model no tools. Skills whose value is tool-orchestration (not guidance) need scenarios whose rubric grades the *plan* the model produces, not executed side effects.
- **Only SKILL.md is injected.** Files the skill links to (`references/*.md`) are not. If the skill's value lives in its references, inline them into a test copy and point `skill_path` at it.
- **Isolation is on by default.** The task model runs in an empty temp dir with no CLAUDE.md, MCP servers, tools, or hooks, so your own harness cannot leak the treatment into the control arm. `SKILL_EVAL_ISOLATE=0` turns that off.

## Reading the verdict

- **DELETE/REWRITE** (delta ≤ −10): the skill is the 77-vs-97 case. Cut it, or rewrite it gotcha-style and re-run.
- **NEUTRAL** (−10 < delta < +10): no measurable lift. It costs tokens for nothing — default to deleting unless it encodes a rare-but-critical guardrail the scenarios under-sample.
- **KEEP** (delta ≥ +10): earns its context. Leave it.

A NEUTRAL on a 400-line skill is still a problem: shrink it to gotchas and re-run — same verdict at a fraction of the tokens is a win.

## Calibrating the judge

The judge is a model too, and its error rate sets how small a delta means anything. Until
`results/judge-calibration.json` exists for the suite's `judge_model`, every report says so.

```bash
python3 ~/.claude/skills/skill-eval/calibrate-judge.py sample   # 20 judged-PASS + 20 judged-FAIL rows, blind
# write PASS / FAIL / SKIP after each LABEL: in results/judge-labels.md
python3 ~/.claude/skills/skill-eval/calibrate-judge.py score    # TPR/TNR -> judge-calibration.json
```

- **TPR and TNR, never agreement.** TPR = judge says PASS when you did; TNR = judge says FAIL when you did. A judge that always says PASS scores 75% agreement on a 75%-PASS suite and catches nothing.
- **Floor 80/80, target 90/90.** Below the floor, rewrite the judge prompt against `dev` rows only, then score `test` once. Tuning on the rows you score inflates the score.
- **Once calibrated, reports add Rogan-Gladen corrected rates**: `(observed + TNR − 1) / (TPR + TNR − 1)`. Read the corrected delta when the raw one sits near ±10.
- **Unparsable judge replies are `JUDGE_ERROR`**, excluded from both arms with the raw text in `reason`. Before 2026-09-22 they counted as FAIL, and `raw.jsonl` kept no reason, so older results cannot separate the two.
