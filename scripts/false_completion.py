#!/usr/bin/env python3
"""Cross-session false-completion detector over Claude Code transcripts. Predicates only.

A flag needs BOTH halves (strict AND, so the detector refuses false positives; the 2025
keyword-overlap version flagged 16 issues and all 16 were wrong):

  1. Unverified completion claim: an assistant turn whose text claims done/fixed/works AND
     whose recent tool calls include no verification (a test/build/run/curl command, a
     browser tool, or a script run) in the last VERIFY_LOOKBACK tool uses of that exchange.
  2. Contradiction within 24h in the same cwd, from any session: the next user prompt(s)
     either (a) open with a contradiction phrase ("still", "didn't", "same error", ...), or
     (b) re-ask the task (keyword Jaccard with the prompt that preceded the claim >= REASK).

Output is a review table plus JSON with stable ids. Label reviewed rows in a golden file
(`{"id": ..., "label": "tp"|"fp"}` per line) and pass --golden to print precision. That
labeling pass is what turns this from an estimate into a checked signal (harness doctrine §5).

Usage:
  false_completion.py [--days 7 | --since ISO] [--out FILE] [--json FILE] [--golden FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oak_transcripts import Session, iter_session_files, jaccard, keywords, load_session, parse_ts, window_start  # noqa: E402

# Report-style claims only: sentence/bullet/bold-initial verdict words, or explicit "now works"
# phrasing. Adjective uses ("fixed inventory", "a complete algorithm") do not match.
# The verdict word must stand alone or lead a clause ("Fixed.", "Merged and verified", "Done: x"),
# so "Fixed overhead" or "Live defect" as a noun phrase does not match.
CLAIM_RE = re.compile(
    r"(?:(?:^|\n|\*\*|✓|✅|\. |: )\s*(?:Done|Fixed|Deployed|Merged|Shipped|Resolved|Completed?|Verified|Working|Live|"
    r"All (?:tests|checks) pass(?:ing)?|Tests? pass(?:es|ing)?)(?=\s*[.:,!)*\n]|\s*$|\s+(?:the|it|in|and|via|by|on|at|now|to|—|-)\b))"
    r"|\b(?:now works?|works now|is working now|is now (?:live|deployed|fixed|working)|has been (?:fixed|deployed|merged|resolved)|"
    r"all (?:tests|checks) (?:pass|passing|green)|tests? (?:pass|passes|are green))\b",
    re.I,
)
# A claim that names its own evidence is not unverified even without a tool call.
HEDGE_RE = re.compile(r"\b(not (yet )?(done|fixed|verified|tested)|untested|unverified|could not verify|cannot verify|did not run)\b", re.I)
# Strong contradiction openers only (first 80 chars). Bare "again"/"error"/"no," proved too loose
# in coaching and analysis conversations.
CONTRADICTION_RE = re.compile(
    r"^(.{0,80}?)\b(still (?:not|doesn'?t|isn'?t|fails?|failing|broken|getting|see|shows?|the same|missing|wrong|no\b)|"
    r"didn'?t (?:work|fix|help|change|do)|did not (?:work|fix|help)|doesn'?t work|does not work|isn'?t working|not working|"
    r"not fixed|never (?:worked|ran|happened)|same (?:error|issue|problem|bug)|(?:it'?s|that'?s|this is) (?:still )?broken|"
    r"(?:that'?s|this is|you'?re) wrong|you said .{0,60}\bbut\b|nope\b|not (?:what|working|done|fixed))\b",
    re.I | re.S,
)
VERIFY_BASH_RE = re.compile(
    r"\b(pytest|npm (test|run (test|build|lint|typecheck))|yarn (test|build|lint)|pnpm (test|build)|jest|vitest|"
    r"cargo (test|build)|go (test|build)|make (test|check)|tsc\b|eslint|ruff|mypy|prove-tests|curl\b|wget\b|"
    r"python3? [^|;&]*\.py|node [^|;&]*\.(m?js|ts)|bash [^|;&]*\.sh|\./[^ ]+\.(sh|py)|docker (build|compose)|"
    r"launchctl (list|print)|aws \w+ (describe|get|list)|kubectl (get|logs)|psql\b|gh-axi (pr|run) (view|status|list)|"
    r"jq\b|plutil|validate|for \w+ in .*; do)",
)
# Reading a file back is not verification of a behavior claim; only executing something is.
VERIFY_LOOKBACK = 8
WINDOW = timedelta(hours=24)
REASK = 0.45
PHRASE_MIN_OVERLAP = 0.10  # a contradiction phrase must also share task vocabulary (golden fp e1fe03561b)


def is_verification(tu) -> bool:
    if tu.name == "Bash":
        return bool(VERIFY_BASH_RE.search(tu.input.get("command", "")))
    # Any MCP call observes an external system (mail sent, page fetched, row read); that counts.
    return tu.name.startswith("mcp__") or tu.name in ("TaskOutput", "Monitor", "WebFetch")


def claims_in(s: Session) -> list[dict]:
    """Assistant turns that end an exchange (next turn is a user prompt or end) with a claim.
    The exchange must have used at least one tool: a claim about work implies work happened."""
    out = []
    recent: list = []
    worked = False
    prev_user = ""
    for i, t in enumerate(s.turns):
        if t.role == "user":
            prev_user = t.text
            recent = []
            worked = False
            continue
        worked = worked or bool(t.tool_uses)
        recent.extend(t.tool_uses)
        recent = recent[-VERIFY_LOOKBACK:]
        nxt = s.turns[i + 1] if i + 1 < len(s.turns) else None
        if nxt is not None and nxt.role != "user":
            continue
        if not t.text or t.sidechain or not worked:
            continue
        m = CLAIM_RE.search(t.text)
        if not m or HEDGE_RE.search(t.text):
            continue
        verified = any(is_verification(tu) for tu in recent)
        out.append(
            {
                "session": s.session_id,
                "path": str(s.path),
                "cwd": s.cwd,
                "ts": t.ts,
                "claim": _excerpt(t.text, m.start()),
                "verified": verified,
                "task_kw": keywords(prev_user[:1500]),
                "task": prev_user[:160].replace("\n", " "),
            }
        )
    return out


def _excerpt(text: str, at: int, width: int = 160) -> str:
    lo = max(0, at - 60)
    return text[lo : lo + width].replace("\n", " ").strip()


def detect(since: datetime) -> tuple[list[dict], dict]:
    # Load a little before `since` so a claim just inside the window can be contradicted by
    # a prompt inside it, and prompts up to 24h after a late claim are still seen.
    # Headless (`claude -p`, entrypoint sdk-cli) runs have scripted prompts and no human to
    # contradict them, so they can neither claim nor contradict here.
    sessions = [load_session(f) for f in iter_session_files(since - WINDOW)]
    sessions = [s for s in sessions if s.turns and s.interactive]
    prompts_by_cwd: dict[str, list[tuple[datetime, str, str]]] = {}
    for s in sessions:
        for t in s.turns:
            if t.role == "user" and not t.sidechain:
                prompts_by_cwd.setdefault(s.cwd, []).append((t.ts, t.text, s.session_id))
    for v in prompts_by_cwd.values():
        v.sort()

    flags = []
    n_claims = n_unverified = 0
    for s in sessions:
        for c in claims_in(s):
            if c["ts"] < since:
                continue
            n_claims += 1
            if c["verified"]:
                continue
            n_unverified += 1
            for pts, ptxt, psess in prompts_by_cwd.get(c["cwd"], []):
                if pts <= c["ts"]:
                    continue
                if pts - c["ts"] > WINDOW:
                    break
                cm = CONTRADICTION_RE.match(ptxt)
                reask = jaccard(c["task_kw"], keywords(ptxt[:1500])) if c["task_kw"] else 0.0
                if (cm and reask >= PHRASE_MIN_OVERLAP) or reask >= REASK:
                    fid = hashlib.sha1(f"{c['session']}{c['ts'].isoformat()}".encode()).hexdigest()[:10]
                    flags.append(
                        {
                            "id": fid,
                            "ts": c["ts"].isoformat(),
                            "session": c["session"],
                            "cwd": c["cwd"],
                            "task": c["task"],
                            "claim": c["claim"],
                            "contradiction": ptxt[:160].replace("\n", " "),
                            "contradiction_session": psess,
                            "cross_session": psess != c["session"],
                            "hours_later": round((pts - c["ts"]).total_seconds() / 3600, 1),
                            "signal": "phrase" if cm else f"re-ask {reask:.2f}",
                        }
                    )
                    break
    meta = {"since": since.isoformat(), "sessions": len(sessions), "claims": n_claims, "unverified_claims": n_unverified, "flags": len(flags)}
    return flags, meta


def render(flags: list[dict], meta: dict, precision: str | None) -> str:
    L = [f"# False-completion sweep — {datetime.now().date()} (since {meta['since'][:16]})", ""]
    L.append(
        f"Sessions {meta['sessions']} · completion claims {meta['claims']} · unverified {meta['unverified_claims']} · "
        f"flagged {meta['flags']}" + (f" · golden precision {precision}" if precision else "")
    )
    L.append("Guardrail: refuses false positives. Both halves required (unverified claim AND contradiction/re-ask within 24h, same cwd).")
    L.append("")
    if not flags:
        L.append("No flags.")
        return "\n".join(L) + "\n"
    L.append("| id | when | cwd | task | claim | contradiction | +h | signal |")
    L.append("|---|---|---|---|---|---|---|---|")
    for f in flags:
        cwd = f["cwd"].replace(str(Path.home()), "~")
        x = "⇄ " if f["cross_session"] else ""
        L.append(
            f"| `{f['id']}` | {f['ts'][:16]} | `{cwd}` | {_cell(f['task'])} | {_cell(f['claim'])} | "
            f"{x}{_cell(f['contradiction'])} | {f['hours_later']} | {f['signal']} |"
        )
    L.append("")
    L.append("⇄ = contradiction came from a different session. Label rows in the golden file to compute precision.")
    return "\n".join(L) + "\n"


def _cell(s: str, n: int = 90) -> str:
    return (s[:n] + "…" if len(s) > n else s).replace("|", "\\|")


def golden_precision(flags: list[dict], golden: Path) -> str | None:
    if not golden.exists():
        return None
    labels = {}
    for ln in golden.read_text().splitlines():
        if ln.strip():
            o = json.loads(ln)
            labels[o["id"]] = o["label"]
    flagged = {f["id"] for f in flags}
    scored = [labels[i] for i in flagged if i in labels]
    fp_back = sum(1 for i, l in labels.items() if l == "fp" and i in flagged)
    tp_lost = sum(1 for i, l in labels.items() if l == "tp" and i not in flagged)
    parts = []
    if scored:
        tp = sum(1 for x in scored if x == "tp")
        parts.append(f"{tp}/{len(scored)} = {tp/len(scored):.0%} on labeled flags")
    else:
        parts.append("no labeled flags in window")
    parts.append(f"labeled fp re-flagged: {fp_back}")
    parts.append(f"labeled tp lost: {tp_lost}")
    return "; ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=7)
    g.add_argument("--since", help="ISO timestamp (e.g. contents of ~/.claude/.retro-sweep-last)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--golden", type=Path, default=Path.home() / ".claude" / "false-completion-golden.jsonl")
    a = ap.parse_args()

    since = parse_ts(a.since) if a.since else window_start(a.days)
    if since is None:
        ap.error("--since must be ISO 8601")
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    flags, meta = detect(since)
    md = render(flags, meta, golden_precision(flags, a.golden))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(md)
        print(f"wrote {a.out}: {meta}")
    else:
        sys.stdout.write(md)
    if a.json:
        a.json.write_text(json.dumps({"meta": meta, "flags": flags}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
