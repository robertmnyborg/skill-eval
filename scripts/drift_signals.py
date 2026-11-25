#!/usr/bin/env python3
"""Reprompt and path-churn detector over Claude Code transcripts. Predicates only.

Two signals the false-completion detector does not see:

  reprompt  A user prompt that restates an earlier prompt in the same session, with no done-claim
            needed in between: keyword Jaccard >= REPROMPT_JACCARD with one of the previous
            LOOKBACK prompts, or a restate phrase ("I asked", "simpler", "what I meant") that also
            shares task vocabulary with the previous prompt, or a confusion phrase ("still not
            understanding", "explain it more simply", "wait, so") on its own.
  churn     The session took a path and came back: an Edit that restores text an earlier Edit
            removed from the same file, a file Written REWRITE_MIN+ times, a git undo command, or
            a user redirect ("go back to", "scrap that", "revert"). A session is flagged at
            CHURN_MIN events, or at one redirect plus one mechanical event.

`--history` runs the reprompt detector over ~/.claude/history.jsonl instead: prompts only, but it
survives transcript cleanup and reaches back to the first session on this machine.

Label reviewed rows in the golden file (`{"id": ..., "label": "tp"|"fp", "why": ...}` per line)
to print precision, same as false_completion.py.

Usage:
  drift_signals.py [--days 7 | --since ISO] [--history] [--out FILE] [--json FILE] [--golden FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from false_completion import _cell, golden_precision  # noqa: E402
from oak_transcripts import Session, iter_session_files, jaccard, keywords, load_session, parse_ts, window_start  # noqa: E402

LOOKBACK = 6
REPROMPT_JACCARD = 0.45
RESEND_JACCARD = 0.85
PHRASE_MIN_OVERLAP = 0.10
MIN_KEYWORDS = 5  # "yes", "go", "continue" and other short steers never count
REWRITE_MIN = 3
CHURN_MIN = 3

RESTATE_RE = re.compile(
    r"^.{0,120}?\b(i (?:already |just )?(?:asked|said|told you)|as i said|like i said|i meant|what i (?:meant|mean|want(?:ed)?)|"
    r"no,? i mean|let me rephrase|you (?:didn'?t|did not) (?:answer|address|do)|that'?s not what i|"
    r"try again|one more time|re-?read (?:my|what))\b",
    re.I | re.S,
)
# Comprehension failures: the answer did not land. Strong enough alone, so no overlap or length gate.
# (2026-09-23 Black Book vendor method: four re-asks in two sessions shared almost no keywords.)
CONFUSION_RE = re.compile(
    r"^.{0,160}?\b(i(?:'m| am)? (?:still )?(?:don'?t|do not|not) (?:understand|follow|get it|getting it|understanding|following)|"
    r"(?:isn'?t|not|doesn'?t) mak(?:e|ing) sense|hard time (?:understanding|reconciling|following)|i(?:'?m| am) (?:confused|lost)|"
    r"(?:explain|say) (?:it|that|this)? ?(?:more )?simpl[ey]|simpler|"
    r"explain (?:it |that |this )?again|how (?:do )?i reconcile|wait,? so\b|so (?:wait|then),)",
    re.I | re.S,
)
REDIRECT_RE = re.compile(
    r"\b(go back to|back to the (?:original|first|previous|old)|revert (?:that|this|it|to|the)|undo (?:that|this|it|the)|"
    r"scrap (?:that|this|it)|start over|wrong (?:approach|direction|path|track)|that was the wrong|"
    r"we already (?:tried|had|did)|you already (?:tried|did)|why did you change)\b",
    re.I,
)
# Harness text that lands in the user role: hook feedback, empty-turn nudges, scheduled ticks.
HARNESS_PREFIXES = ("Stop hook feedback", "[Your previous response", "<<autonomous-loop", "Read " + str(Path.home() / ".overworld" / "handoffs") + "/")
LOOP_REPEATS = 3  # one prompt sent 3+ times verbatim in a session is a /loop or cron tick
GIT_UNDO_RE = re.compile(r"\bgit (?:checkout -- |checkout \S+ -- |restore |reset --hard|revert |stash drop)")


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def _row(sid, cwd, ts, earlier, text, gap, signal) -> dict:
    return {
        "id": _id(sid, "reprompt", ts.isoformat()),
        "kind": "reprompt",
        "ts": ts.isoformat(),
        "session": sid,
        "cwd": cwd,
        "earlier": earlier[:160].replace("\n", " "),
        "prompt": text[:160].replace("\n", " "),
        "turns_apart": gap,
        "signal": signal,
    }


def reprompts(sid: str, cwd: str, prompts: list[tuple[datetime, str]], since: datetime) -> list[dict]:
    out = []
    seen = Counter(p[:80] for _, p in prompts)
    # `!` lines are shell commands the user ran, not asks; "❯ " is a pasted copy of an earlier prompt.
    prompts = [(ts, p.removeprefix("❯ ")) for ts, p in prompts if not p.startswith(HARNESS_PREFIXES + ("!",)) and seen[p[:80]] < LOOP_REPEATS]
    kws = [keywords(p[:1500]) for _, p in prompts]
    for i, (ts, text) in enumerate(prompts):
        if ts < since or text.startswith("/"):
            continue
        confused = CONFUSION_RE.match(own_words(text))
        if confused:
            j = max(i - 1, 0)
            out.append(_row(sid, cwd, ts, prompts[j][1], text, i - j, f"confusion '{confused.group(1)}'"))
            continue
        if len(kws[i]) < MIN_KEYWORDS:
            continue
        best_j, best = -1, 0.0
        for j in range(max(0, i - LOOKBACK), i):
            s = jaccard(kws[i], kws[j])
            if s > best:
                best_j, best = j, s
        phrase = RESTATE_RE.match(own_words(text))
        prev_overlap = jaccard(kws[i], kws[i - 1]) if i else 0.0
        if best >= REPROMPT_JACCARD:
            # The same ask sent again right away: the first attempt hung, errored or was interrupted.
            kind = "resend" if best >= RESEND_JACCARD and best_j == i - 1 else "jaccard"
            signal, j = f"{kind} {best:.2f}", best_j
        elif phrase and prev_overlap >= PHRASE_MIN_OVERLAP:
            signal, j = f"phrase '{phrase.group(1)}'", i - 1
        else:
            continue
        out.append(_row(sid, cwd, ts, prompts[j][1], text, i - j, signal))
    return out


def churn(s: Session, since: datetime) -> dict | None:
    events: list[tuple[str, str]] = []
    removed: dict[str, set[str]] = defaultdict(set)
    writes: Counter = Counter()
    for t in s.turns:
        if t.sidechain or t.ts < since:
            continue
        if t.role == "user":
            m = not t.text.startswith(HARNESS_PREFIXES + ("# ",)) and REDIRECT_RE.search(own_words(t.text))
            if m:
                events.append(("redirect", t.text[:120].replace("\n", " ")))
            continue
        for tu in t.tool_uses:
            fp = tu.input.get("file_path", "")
            if tu.name == "Edit":
                old, new = tu.input.get("old_string", ""), tu.input.get("new_string", "")
                if len(new) > 20 and new in removed[fp]:
                    events.append(("revert-edit", Path(fp).name))
                if len(old) > 20:
                    removed[fp].add(old)
            elif tu.name == "Write":
                writes[fp] += 1
                if writes[fp] == REWRITE_MIN:
                    events.append(("rewrite", Path(fp).name))
            elif tu.name == "Bash" and GIT_UNDO_RE.search(tu.input.get("command", "")):
                events.append(("git-undo", tu.input["command"][:80]))
    kinds = Counter(k for k, _ in events)
    if len(events) < CHURN_MIN and not (kinds["redirect"] and len(events) > kinds["redirect"]):
        return None
    return {
        "id": _id(s.session_id, "churn", since.date()),
        "kind": "churn",
        "ts": s.turns[0].ts.isoformat(),
        "session": s.session_id,
        "cwd": s.cwd,
        "counts": dict(kinds),
        "examples": [f"{k}: {v}" for k, v in events[:4]],
    }


def own_words(text: str) -> str:
    """The user's typed words: pasted blocks and long tails quote other people and old output."""
    return text.split("<pasted_content", 1)[0][:600]


def first_prompt(s: Session) -> str:
    return next((t.text for t in s.turns if t.role == "user" and not t.sidechain), "")


def detect(since: datetime) -> tuple[list[dict], dict]:
    sessions = [load_session(f) for f in iter_session_files(since)]
    sessions = [s for s in sessions if s.turns and s.interactive and not re.search(r"retro.*sweep", first_prompt(s)[:300], re.I)]
    # Probe and eval batches share one scripted first prompt; they are not conversations.
    shared = Counter(first_prompt(s)[:200] for s in sessions)
    sessions = [s for s in sessions if shared[first_prompt(s)[:200]] < 3]
    flags = []
    for s in sessions:
        prompts = [(t.ts, t.text) for t in s.turns if t.role == "user" and not t.sidechain]
        flags += reprompts(s.session_id, s.cwd, prompts, since)
        c = churn(s, since)
        if c:
            flags.append(c)
    return flags, {"since": since.isoformat(), "source": "transcripts", "sessions": len(sessions)}


def load_history(path: Path) -> tuple[dict[str, list[tuple[datetime, str]]], dict[str, str]]:
    by_session: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    cwd: dict[str, str] = {}
    for line in path.open(errors="ignore"):
        o = json.loads(line)
        sid = o.get("sessionId")
        if not sid or not o.get("timestamp"):
            continue
        by_session[sid].append((datetime.fromtimestamp(o["timestamp"] / 1000, tz=timezone.utc), o.get("display", "")))
        cwd[sid] = o.get("project", "")
    return {k: sorted(v) for k, v in by_session.items()}, cwd


def detect_history(since: datetime, path: Path) -> tuple[list[dict], dict]:
    by_session, cwd = load_history(path)
    flags = []
    for sid, prompts in by_session.items():
        flags += reprompts(sid, cwd[sid], prompts, since)
    return flags, {"since": since.isoformat(), "source": "history.jsonl", "sessions": len(by_session)}


# Improvement opportunities: the output was accepted, but the user still had to steer it, and the
# same steering recurs across sessions. Each kind names the harness change that would remove it.
POLISH = {
    "shorter": r"make (?:it|this|that|them) (?:shorter|more concise|tighter|briefer)|more concise|shorten|too (?:long|wordy|verbose|dense)|less (?:verbose|wordy|text)|tighten|condense",
    "simpler": r"make (?:it|this|that) (?:simpler|clearer|easier)|less jargon|too (?:technical|complex|complicated)",
    "table": r"(?:as|into|in) a table|make (?:it|this) a table|tabular",
    "bullets": r"(?:as |into |use )?bullet(?:s| points)",
    "wording": r"reword|rephrase|change the (?:tone|wording|title|headline|copy)|rewrite (?:it|this|that|the)|sound(?:s)? (?:more|less) ",
    "visual": r"change the (?:color|colour|font|layout|spacing|style)|(?:bigger|smaller) font|align(?:ed)? (?:the|it)|more (?:padding|spacing|whitespace)",
    "add-element": r"add (?:a|another|an) (?:column|row|section|chart|line|tab|filter|link|note|caveat|source)",
}
POLISH_RE = {k: re.compile(r"^.{0,80}?\b(?:" + v + r")\b", re.I | re.S) for k, v in POLISH.items()}
RULE_RE = re.compile(r"\b(always|never|make sure|don'?t|do not|remember|every time|from now on|by default|instead of|should(?:n'?t)?|use )\b", re.I)
DISPOSITION = {
    "polish": "change the default in the skill or rule that produced it",
    "standing-instruction": "turn into a CLAUDE.md rule, or a hook if a script can check it",
    "workflow": "package as a skill",
    "shell": "wrap in a script, alias or hook",
}
OPP_MIN_SESSIONS = 3


def _norm(text: str) -> str:
    text = re.sub(r"https?://\S+", "URL", text)
    text = re.sub(r"[~/][\w./-]+", "PATH", text)
    text = re.sub(r"\d+", "N", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _cluster(items: list[tuple[str, frozenset, str]], threshold: float) -> list[list[tuple[str, frozenset, str]]]:
    """Greedy keyword clustering of (session, keywords, text) items."""
    clusters: list[list] = []
    for it in items:
        for c in clusters:
            if jaccard(it[1], c[0][1]) >= threshold:
                c.append(it)
                break
        else:
            clusters.append([it])
    return clusters


def _opp(kind: str, key: str, members: list[tuple[str, str]], cwd: dict[str, str]) -> dict:
    sessions = sorted({sid for sid, _ in members})
    return {
        "id": _id("opp", kind, key),
        "kind": "opportunity",
        "type": kind,
        "key": key,
        "sessions": len(sessions),
        "session_ids": [x[:8] for x in sessions[:8]],
        "cwds": sorted({cwd.get(x, "").replace(str(Path.home()), "~") for x in sessions})[:4],
        "examples": list(dict.fromkeys(t[:140].replace("\n", " ") for _, t in members))[:4],
        "disposition": DISPOSITION[kind],
    }


def opportunities(by_session: dict[str, list[tuple[datetime, str]]], cwd: dict[str, str], since: datetime) -> list[dict]:
    polish: dict[str, list] = defaultdict(list)
    rules: list[tuple[str, frozenset, str]] = []
    firsts: list[tuple[str, frozenset, str]] = []
    shells: dict[str, list] = defaultdict(list)
    for sid, prompts in by_session.items():
        seen = Counter(p[:80] for _, p in prompts)
        live = [(ts, p) for ts, p in prompts if ts >= since and not p.startswith(HARNESS_PREFIXES) and seen[p[:80]] < LOOP_REPEATS]
        for n, (ts, p) in enumerate(live):
            if p.startswith("!"):
                shells[_norm(p)[:80]].append((sid, p))
                continue
            if p.startswith("/"):
                continue
            words = own_words(p)
            if n == 0 and len(keywords(words)) >= MIN_KEYWORDS:
                firsts.append((sid, frozenset(keywords(words)), words))
            if n > 0:  # polish follows an answer the user kept
                for k, rx in POLISH_RE.items():
                    if rx.match(words):
                        polish[k].append((sid, words))
                        break
            for sent in re.split(r"(?<=[.!?])\s+|\n+", p.split("<pasted_content", 1)[0]):
                kw = keywords(sent)
                if 4 <= len(kw) <= 25 and RULE_RE.search(sent):
                    rules.append((sid, frozenset(kw), sent.strip()))
    out = [_opp("polish", k, v, cwd) for k, v in polish.items()]
    out += [_opp("shell", k, v, cwd) for k, v in shells.items()]
    for kind, items, thr in (("standing-instruction", rules, 0.5), ("workflow", firsts, 0.4)):
        for c in _cluster(items, thr):
            out.append(_opp(kind, _norm(c[0][2])[:60], [(sid, t) for sid, _, t in c], cwd))
    out = [o for o in out if o["sessions"] >= OPP_MIN_SESSIONS]
    return sorted(out, key=lambda o: (list(DISPOSITION).index(o["type"]), -o["sessions"]))


def render(flags: list[dict], meta: dict, precision: str | None, opps: list[dict] | None = None) -> str:
    rp = [f for f in flags if f["kind"] == "reprompt"]
    ch = [f for f in flags if f["kind"] == "churn"]
    home = str(Path.home())
    L = [f"# Drift signals ({meta['source']}), {datetime.now().date()} (since {meta['since'][:16]})", ""]
    L.append(f"Sessions {meta['sessions']} · reprompts {len(rp)} · churn sessions {len(ch)}" + (f" · golden precision {precision}" if precision else ""))
    L += ["", "## Reprompts", ""]
    if rp:
        L += ["| id | when | session | cwd | earlier prompt | restated prompt | gap | signal |", "|---|---|---|---|---|---|---|---|"]
        for f in rp:
            L.append(
                f"| `{f['id']}` | {f['ts'][:16]} | {f['session'][:8]} | `{f['cwd'].replace(home, '~')}` | {_cell(f['earlier'])} | "
                f"{_cell(f['prompt'])} | {f['turns_apart']} | {f['signal']} |"
            )
    else:
        L.append("None.")
    if meta["source"] == "transcripts":
        L += ["", "## Path churn", ""]
        if ch:
            L += ["| id | session | cwd | counts | examples |", "|---|---|---|---|---|"]
            for f in ch:
                counts = ", ".join(f"{k} {v}" for k, v in f["counts"].items())
                L.append(f"| `{f['id']}` | {f['session'][:8]} | `{f['cwd'].replace(home, '~')}` | {counts} | {_cell('; '.join(f['examples']), 200)} |")
        else:
            L.append("None.")
    if opps is not None:
        L += ["", f"## Improvement opportunities (accepted output you still had to steer, {OPP_MIN_SESSIONS}+ sessions)", ""]
        if opps:
            L += ["| id | type | pattern | sessions | where | examples | fix |", "|---|---|---|---|---|---|---|"]
            for o in opps:
                L.append(
                    f"| `{o['id']}` | {o['type']} | {_cell(o['key'], 50)} | {o['sessions']} | {_cell(', '.join(o['cwds']), 60)} | "
                    f"{_cell(' / '.join(o['examples']), 220)} | {o['disposition']} |"
                )
        else:
            L.append("None.")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=7)
    g.add_argument("--since", help="ISO timestamp (e.g. contents of ~/.claude/.retro-sweep-last)")
    ap.add_argument("--history", action="store_true", help="reprompts only, from ~/.claude/history.jsonl")
    ap.add_argument("--opp-days", type=int, default=30, help="window for improvement opportunities (0 = skip)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--golden", type=Path, default=Path.home() / ".claude" / "drift-golden.jsonl")
    a = ap.parse_args()

    since = parse_ts(a.since) if a.since else window_start(a.days)
    if since is None:
        ap.error("--since must be ISO 8601")
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if a.history:
        flags, meta = detect_history(since, Path.home() / ".claude" / "history.jsonl")
    else:
        flags, meta = detect(since)
    opps = None
    if a.opp_days:
        opps = opportunities(*load_history(Path.home() / ".claude" / "history.jsonl"), window_start(a.opp_days))
    md = render(flags, meta, golden_precision(flags, a.golden), opps)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(md)
        print(f"wrote {a.out}: {meta} flags={len(flags)} opportunities={len(opps or [])}")
    else:
        sys.stdout.write(md)
    if a.json:
        a.json.write_text(json.dumps({"meta": meta, "flags": flags, "opportunities": opps}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
