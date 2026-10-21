---
name: retro
description: Run a retrospective on the current/recent session — find where the agent looped, lied, repeated failed tool calls, or got corrected — and turn each into a structural harness fix (hook/skill/doctrine edit) or, as fallback, a memory gotcha. Use at the end of a hard task, when asked to "do a retro", "what went wrong", "capture lessons from this session", or after a session full of failures and corrections. Use "/retro sweep" (also fired weekly by bin/retro-sweep.sh) to cluster failures across ALL recent transcripts instead of one session.
---

# retro — every failure becomes harness memory

The WorkOS/Case lesson: **don't fix the mistake, fix the harness so it can't recur.** Case ends every run with a retrospective agent that reads its own transcript, finds doom loops and repeated failures, and writes them into markdown memory files. Next run skips the landmine. Claude Code's auto-memory dir (`~/.claude/projects/<project>/memory/`) loads into every session, so this closes the loop for real.

## What to do

1. **Find the failure signals** in this session (or a transcript JSONL the user points to). Look for:
   - The same tool call run 2+ times with no change between (a doom loop).
   - A claim of work done that was later contradicted ("you said tests passed but they didn't").
   - A user correction — any "no", "that's wrong", "actually", "don't do that", "I told you to".
   - A dead end: an approach abandoned after multiple attempts.
   - A surprising environment fact discovered the hard way (missing binary, wrong path, API quirk).

   Quick scan of recent transcripts:
   ```bash
   ls -t ~/.claude/projects/*/[0-9a-f]*.jsonl 2>/dev/null | head -3
   # repeated identical Bash commands (doom-loop smell):
   jq -r 'select(.message.content[]?.type=="tool_use" and .message.content[]?.name=="Bash") | .message.content[]?.input.command' <file> 2>/dev/null | sort | uniq -c | sort -rn | awk '$1>1'
   ```

2. **Distill each signal into ONE gotcha.** Not a narrative — a rule the next session can act on. "X fails because Y; do Z instead." Drop anything already obvious from the code, git history, or existing memory.

3. **Structural fix FIRST, memory gotcha as fallback.** For each gotcha, ask: can a hook, skill edit, CLAUDE.md line, allowlist entry, or wrapper script make this failure *impossible* rather than *remembered*? If yes, make that edit now (it's reviewable in git) and skip the memory file — a constraint the harness enforces beats one the model must recall. Write a memory gotcha only when no structural fix exists (tacit knowledge, judgment calls, external-world facts). A correction the user made twice and that COULD be a hook but was memorialized instead is a retro failure.

4. **When memory is the right disposition, write it** following the user's memory format (frontmatter `name`/`description`/`type`, then body with **Why:** / **How to apply:** for feedback/project types). Check for an existing file that already covers it and update that instead of duplicating. Type guide:
   - environment/API landmine → `reference`
   - "how Claude should work here" correction → `feedback`
   - project-specific constraint learned → `project`
5. **Add the one-line pointer to `MEMORY.md`.** No content there, just the index line.
6. **Report**: list each structural fix applied (file + what it prevents) and each gotcha written/updated. If nothing rose above the bar, say so — a clean session needs no memory.

## Sweep mode — `/retro sweep`

> **Headless runs may not be allowed to Edit/Write under `~/.claude/`.** Claude Code treats
> paths there as sensitive and can deny Edit/Write even with `--permission-mode acceptEdits`,
> and in some versions `cp`, `chmod` and shell redirects against those paths are denied too.
> A `python3` call that does the write itself has stayed open: stage the content in a temp
> dir with the normal Write tool, then `python3 -c "import shutil; shutil.copyfile(src, dst)"`.
> Provenance: three consecutive weekly sweeps staged their primary fix into a `proposed-<date>/`
> folder instead of landing it, and none of the three ever got applied. The guard they were
> fixing sat unpatched for a month. Apply your own fixes in-run; stage only what genuinely
> needs a human (`bin/retro-sweep.sh` runs a staged `APPLY.sh` as a last resort).

Per-session retros spot-check; the failures that matter cluster ACROSS sessions. Sweep mode is the trace-sweeping half of the loop (weekly via `bin/retro-sweep.sh`, or on demand).

1. **Window**: read `~/.claude/.retro-sweep-last` (ISO timestamp); default to 7 days back if absent. Collect all transcript JSONLs under `~/.claude/projects/*/` modified since then (skip files >50MB; sample the newest 30 if more).

   macOS `find -newermt` does NOT parse an ISO `...Z` string (it returns nothing, silently).
   Convert the watermark to local time first:
   ```bash
   WM_LOCAL=$(date -r "$(date -j -u -f %Y-%m-%dT%H:%M:%SZ "$(cat ~/.claude/.retro-sweep-last)" +%s)" +'%Y-%m-%d %H:%M:%S')
   find ~/.claude/projects -maxdepth 2 -name '*.jsonl' -newermt "$WM_LOCAL" -size -50M
   ```

   **Project dirs start with `-`** (`-Users-you`, and `-` itself for a session run from `/`,
   which is how a launchd sweep starts). `stat`, `grep` and `ls` read a path like
   `-/<session-id>.jsonl` as flags and fail. Do the corpus scan in one `python3` script with `glob`,
   or pass absolute paths.

   **File mtime selects the FILE, never the EVENT — filter every signal on the event's own
   `.timestamp` against the watermark.** A resumed session rewrites its whole transcript's
   mtime, so an mtime-selected corpus is full of pre-window events. Measured once: 17% of tool
   results in the selected corpus predated the window, and every one of the 22 guard-hook
   denials "found" that week came from before the fix being evaluated. Without the timestamp
   filter the sweep reports a landed fix as a failed one and re-fixes it.
2. **Extract signals per transcript** with the same detectors as above (doom loops, contradicted claims, user corrections, dead ends), using `jq` scans first — only Read a transcript deeply when its scan shows a signal.

   **Optional deterministic pre-steps.** If `RETRO_SIGNALS_DIR` points at a checkout of
   [claude-oak-agents](https://github.com/robertmnyborg/claude-oak-agents) `scripts/`,
   `bin/retro-sweep.sh` writes two tables to its log dir before the sweep starts (stdlib python,
   no LLM). Start from them when present:
   - `false-completions-week.md`: every assistant turn that claimed done/fixed/merged with no
     executing tool call behind it AND was contradicted or re-asked by the user within 24h in the
     same cwd. The detector is strict by design, so a short table is expected; a true positive is a
     harness defect (which verification step was skipped, and which hook or skill line should have
     forced it).
   - `drift-signals-week.md`: reprompts and path churn, strongest first:
     - `confusion`: "explain it more simply", "wait, so", "still not understanding". The answer did
       not land. Read the assistant turn before it and name the miss (led with results before
       method, undefined term, answered a different question). A cluster is a writing-rule or
       skill defect, not a memory. Baseline: one recurring "explain this method" thread across two
       sessions flagged 7 of 7 re-asks as `confusion`.
     - `phrase`: "I meant", "no, I mean", "that's not what I". The ask was misread.
     - `jaccard`: the ask restated within 6 prompts. Half are extensions of the same ask (false positive).
     - `resend`: the same prompt sent again right away. The first attempt hung, errored or got
       interrupted; look for the tool failure, not a comprehension failure.
     - churn rows: reverted edits, a file Written 3+ times, git undo, user redirects ("go back to",
       "scrap that"). Read the redirect turn and name the fork where the session took the wrong path.
   Cluster on the cause of the miss, never on the topic.

   **2c. Improvement opportunities: correct output you still had to steer.** `~/.claude/history.jsonl`
   keeps every typed prompt with `sessionId` and `project`, even after the transcript is gone. Scan
   the last 30 days for recurrence across 3+ sessions. Each type names its fix:
   - `polish`: a follow-up that reshapes an accepted answer ("more concise", "as bullets",
     "add a column"). Change the default in the skill or rule that produced the first draft.
     Baseline over 120 days: "make it more concise" in 16 sessions, mostly chat replies and doc sections.
   - `workflow`: the same opening ask in 3+ sessions. Package it as a skill.
   - `shell`: the same `!` command in 3+ sessions (an SSO login command, 7 sessions). Wrap it in a
     script, or a hook that runs it when the failure it fixes appears.
   - `standing-instruction`: a rule sentence ("always", "never", "make sure") repeated in 3+
     sessions. Turn it into a CLAUDE.md rule, or a hook if a script can check it.
   An opportunity that already has a skill is a routing miss instead: fix the skill's trigger
   description. Report opportunities as their own section, ranked by sessions, with the fix applied.

   **Exclude the sweep's own transcripts from the corpus.** A past `/retro sweep` session quotes
   its own findings, so words like "denied", "blocked" and "refused" appear hundreds of times as
   *content* and swamp the real detectors. Drop any transcript whose first real user prompt
   matches `retro.*sweep` before counting anything.

   **Exclude probe and eval sessions too.** skill-eval and hook-test runs spawn many headless
   sessions with an identical first prompt. Drop any first prompt shared by 3+ sessions before
   counting corrections. Measured: one week's 24 skill-eval runs were 24 of 53 correction-regex hits.

   **Verify a blocked-looking signal actually failed.** Match `tool_result` back to its
   `tool_use_id` and check `is_error`. A pattern can look like a harness block and be a 96%
   success rate: one sweep found 55 chained `sleep` calls and assumed the documented block was
   firing, when only 2 of 55 were denied and the doc claiming otherwise was the actual defect.
3. **Cluster across sessions.** The unit of output is the *recurring* failure: same command failing in 3 sessions, same correction given twice, same MCP tool erroring repeatedly. One-off stumbles that self-corrected don't clear the bar in sweep mode. **Severity overrides frequency:** a single failure clears the bar when its damage left the session: a wrong claim that reached a person or ticket (chat, email, issue tracker, PR, doc), money spent (prod runs, paid APIs, a session over $50), or a prod write. Rank these above any recurring cluster. (Provenance: one misdiagnosis happened once, cost two paid re-runs, a wrong ticket, and a wrong cause sent to an engineer; a count-only bar never surfaces it.)
4. **Cost check — flag runaway sessions.** Read the window's per-session spend from `ccusage-week.json` in the sweep log dir (pre-generated by `bin/retro-sweep.sh` via [ccusage](https://github.com/ryoppippi/ccusage)). Two schema traps: the session id lives in **`.period`**, not `.sessionId` (which is null), and the report only covers the window the script asked for — if the watermark is older than that (a missed sweep), regenerate it or the cost line is wrong:

   ```bash
   # --until is EXCLUSIVE of the day you name: passing today silently drops today's
   # spend. Always ask for TOMORROW. (Measured: passing today hid three sessions,
   # 11% of the window's spend.)
   npx -y ccusage@latest session --since <watermark YYYYMMDD> --until <tomorrow YYYYMMDD> --json
   ```

   Flag any session over **$50 equivalent**, and the top session if it exceeds **30% of the window's total spend**. **Roll `wf_*` rows into their parent first**: Workflow runs bill as separate `wf_<id>` sessions in ccusage, so a parent that fans out looks cheap. Find the parent with `grep -l wf_<id> ~/.claude/projects/*/*.jsonl` (exclude the sweep's own transcript) and add the children's cost before applying either threshold. Measured once: a parent was 11% of the window on its own and 31.5% with its three workflow children. For each flagged session, identify it (project dir, first user prompt, span) and treat it as a failure cluster: the structural fix is session hygiene — fresh context per task, `/compact` or restart at topic boundaries.

   **Measure span in real user prompts, not file mtime.** Transcript mtimes get bulk-touched and will tell you a 16-hour session ran for three days:

   ```bash
   jq -r 'select(.type=="user" and (.isMeta|not))
     | select((.message.content|tostring)|test("tool_result")|not) | .timestamp' <file> | sort -u
   ```

   **The driver is context volume, not calendar age.** Every turn re-reads the whole history, so what costs money is bytes-in-context — a dense 16-hour session outspends a sparse three-day one. Rank flagged sessions by **main transcript + `<sid>/subagents/*.jsonl` bytes**; a fan-out session can carry 17MB of subagent context behind a 2MB main transcript and look cheap. **Verify a hook fired before concluding it did not:** UserPromptSubmit stdout lands in the transcript, so `grep -c <hook-name> <transcript>` settles it in one command.

5. **Disposition per cluster, structural-fix-first** (step 3 above). Recurring clusters are precisely the ones that deserve a hook/skill/CLAUDE.md patch, not a memory note.
6. **Close out**: write the new timestamp to `~/.claude/.retro-sweep-last` (UTC, `%Y-%m-%dT%H:%M:%SZ`). If you keep a changelog for harness edits, append a dated entry. Report: clusters found → fix applied → expected effect, plus one cost line (window total, top session $ and %, sessions flagged). **Closing signal: repeat clusters trending to zero week-over-week AND no unflagged runaway sessions** — state whether this week's clusters are new or repeats from last sweep.

## Gotchas (the meta ones)

- **One fact per file.** Don't write a "session-lessons.md" dump; that's a journal, not memory. Atomic facts recall better.
- **Prefer updating over creating.** A near-duplicate memory is worse than none — it splits the signal.
- **Don't memorialize the transient.** "The build was red because of an uncommitted file" is not memory. "This repo's test cmd is `pnpm -w test`, not `npm test`" is.
- **A correction the user made twice is a `feedback` memory, full stop.** Repeat corrections are the highest-value retros — they're the harness bug.

## Optional auto-trigger
A `Stop` hook can cheaply grep the just-finished transcript for repeated tool calls and print "retro recommended" without spending tokens, leaving the LLM extraction to an explicit `/retro`. Offer to wire it if the user wants the loop automatic rather than on-demand.
