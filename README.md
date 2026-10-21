# skill-eval

Two Claude Code skills for keeping a harness honest:

- **`skill-eval`** runs the same task prompts with a skill loaded and without it, has a judge model grade every output against a rubric, and prints the pass-rate delta with a KEEP / NEUTRAL / DELETE verdict.
- **`retro`** reads session transcripts for doom loops, contradicted "done" claims, and repeat user corrections, and turns each one into a hook, skill edit, or CLAUDE.md rule so the failure cannot recur.

## Why

Most people install a skill, feel that the output got better, and never check. A skill can make the model worse: the public case that started this repo is a skill that scored 77% with it loaded vs 97% without it (Nick Nisi, WorkOS). The only way to know is to run both arms and count.

Example run, `stop-slop` (Hardik Pandya's writing-style skill, published version at commit `8da1f03`, not bundled; `suites/stop-slop.json` expects it at `~/.claude/skills/stop-slop/`), 3 scenarios, 5 runs per arm, Sonnet as task model and judge. The rubrics were fixed before these runs, so no row here was used to tune them:

| scenario | with-skill | without-skill | delta | verdict |
|---|---|---|---|---|
| release-note | 100% (5/5) | 100% (5/5) | 0 pts | neutral |
| linkedin-post | 100% (5/5) | 0% (0/5) | +100 pts | helps |
| status-update | 100% (5/5) | 80% (4/5) | +20 pts | helps |

**Overall: with 100% vs without 60%, +40 pts. Verdict: KEEP.**

An earlier 3-run pass scored `release-note` at +34 and `status-update` at 0. At 3 runs per arm, one output flips a scenario by 33 points, so read per-scenario deltas under 5 runs as noise and trust the overall line.

The bundled `qualitative-rigor` example scored 100% vs 100% on the same day: current Sonnet already flags small samples and refuses to invent quotes, so that skill costs tokens and buys nothing. Both results came from the same command, pointed at a different suite.

## Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI (`claude`) on PATH, or set `CLAUDE_BIN`
- `jq`, `bash`, `python3` (stdlib only)
- `retro-sweep.sh` also uses `zsh` and, for the cost check, `npx` ([ccusage](https://github.com/ryoppippi/ccusage))

## Install

```bash
git clone https://github.com/robertmnyborg/skill-eval
cp -R skill-eval/skills/skill-eval skill-eval/skills/retro ~/.claude/skills/
# optional, for the weekly sweep:
mkdir -p ~/bin && cp skill-eval/bin/retro-sweep.sh ~/bin/
```

Claude Code picks up both skills on the next session. Ask "is the X skill worth it?" or "do a retro" and they trigger, or call them as `/skill-eval` and `/retro`.

## Usage: skill-eval

Run the bundled example (needs nothing else installed):

```bash
~/.claude/skills/skill-eval/run-eval.sh ~/.claude/skills/skill-eval/suites/qualitative-rigor.json --runs 3
```

The script prints the path to `results/<skill>-<timestamp>/report.md`. `raw.jsonl` next to it holds every response and the judge's one-line reason.

A suite is a JSON file:

```json
{
  "skill": "stop-slop",
  "skill_path": "~/.claude/skills/stop-slop/SKILL.md",
  "model": "sonnet",
  "judge_model": "sonnet",
  "runs_per_arm": 3,
  "scenarios": [
    { "id": "release-note", "prompt": "Write a 4-sentence release note for ...",
      "rubric": "PASS only if ALL hold: (a) no em dash; (b) ... FAIL if any condition is violated." }
  ]
}
```

Cost: scenarios x 2 arms x runs task calls, plus the same number of judge calls. 3 scenarios x 3 runs = 36 model calls.

What the runner does for you:

- **Isolation.** Each task call runs in an empty temp dir with no CLAUDE.md, no MCP servers, no tools, and hooks disabled. Without this, a probe showed the control arm reading the same rules the suite was A/B-ing, so the control had the treatment. `SKILL_EVAL_ISOLATE=0` turns it off.
- **Judge errors stay out of the count.** An unparsable judge reply is `JUDGE_ERROR` and drops from both arms. Counting it as FAIL biases both arms by an unmeasured amount.
- **Judge calibration.** `calibrate-judge.py sample` draws 20 judged-PASS and 20 judged-FAIL rows, blind. You label them, `calibrate-judge.py score` reports TPR and TNR, and every later report adds Rogan-Gladen corrected pass rates. Floor 80/80, target 90/90.

Rules that decide whether the number means anything are in [`skills/skill-eval/SKILL.md`](skills/skill-eval/SKILL.md). The short version: the rubric is the eval. The first draft of the `linkedin-post` rubric failed both arms 0 of 2 because it banned a closing question the skill never forbids. Grade what the skill claims to change, as hard checks.

## Usage: retro

- `/retro` at the end of a rough session: finds the loops and corrections in this transcript and lands a structural fix, or a memory note when no hook or rule can enforce it.
- `/retro sweep`: clusters failures across every transcript since the last sweep, flags sessions over $50 or over 30% of the window's spend, and writes `~/.claude/.retro-sweep-last` as its watermark.

`bin/retro-sweep.sh` runs the sweep headless. It fails loudly when the watermark does not advance, because one run died after a single turn and the loop did nothing for a week without anyone noticing.

Optional settings (env vars):

| var | default | purpose |
|---|---|---|
| `CLAUDE_BIN` | `command -v claude` | claude binary |
| `RETRO_LOG_DIR` | `~/Library/Logs/retro-sweep` (macOS), `~/.local/state/retro-sweep` | logs and pre-step tables |
| `RETRO_SIGNALS_DIR` | unset | `scripts/` of [claude-oak-agents](https://github.com/robertmnyborg/claude-oak-agents), adds deterministic false-completion and reprompt tables |
| `RETRO_NPX` | `command -v npx` | npx for the ccusage cost pre-step |

Scheduling is optional. On macOS, edit `YOUR_USER` in `launchd/com.example.retro-sweep.plist`, copy it to `~/Library/LaunchAgents/`, and run `launchctl load` on it. Elsewhere, a cron line works: `0 9 * * 6 $HOME/bin/retro-sweep.sh`.

## Layout

```
skills/skill-eval/   SKILL.md, run-eval.sh, calibrate-judge.py, suites/, examples/
skills/retro/        SKILL.md
bin/retro-sweep.sh   headless weekly sweep
launchd/             optional macOS schedule template
```

## License

MIT
