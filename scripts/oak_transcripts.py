"""Read Claude Code transcripts as telemetry. Stdlib only.

Claude Code writes one JSONL per session under ~/.claude/projects/<project>/<session>.jsonl
and one JSONL per subagent run under <project>/<session>/subagents/agent-<id>.jsonl.
This module turns those files into a flat event stream so the audit scripts never
parse JSON themselves.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))

# User-role messages the harness writes on the user's behalf: peer-agent mail, skill bodies.
INJECTED_PREFIXES = ("Another Claude session sent a message", "Base directory for this skill", "[Request interrupted")
IMAGE_TOKEN_RE = re.compile(r"\[Image: [^\]]*\]")

STOPWORDS = frozenset(
    """a an the and or but if then else for to of in on at by with from as is are was were be been
    being this that these those it its into onto over under about after before during while do does
    did done have has had not no yes can could should would will shall may might must also just
    very so than too we you i me my our your they them their he she his her what which who whom
    how when where why all any some each both few more most other such only own same please let
    want need make get use using used run check look see try make sure now here there""".split()
)


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict
    ts: datetime


@dataclass
class Turn:
    """One assistant message or one real user prompt (tool_results are folded into the
    preceding assistant turn as `results`)."""

    role: str  # "user" | "assistant"
    ts: datetime
    text: str
    tool_uses: list[ToolUse] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)  # tool_use_id -> result text
    uuid: str = ""
    sidechain: bool = False


@dataclass
class Session:
    path: Path
    session_id: str
    project_dir: Path
    cwd: str
    turns: list[Turn]
    entrypoint: str = ""  # "cli" / "claude-desktop" = interactive; "sdk-cli" = headless -p run

    @property
    def interactive(self) -> bool:
        return self.entrypoint in ("cli", "claude-desktop")

    @property
    def start(self) -> datetime | None:
        return self.turns[0].ts if self.turns else None

    @property
    def end(self) -> datetime | None:
        return self.turns[-1].ts if self.turns else None

    def subagent_file(self, agent_id: str) -> Path | None:
        p = self.path.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        return p if p.exists() else None


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def iter_session_files(since: datetime | None = None, projects_dir: Path = PROJECTS_DIR) -> Iterator[Path]:
    """Main session transcripts (not subagent files) modified since `since`."""
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            if since and datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < since:
                continue
            yield f


def load_session(path: Path) -> Session:
    turns: list[Turn] = []
    cwd = ""
    entrypoint = ""
    session_id = path.stem
    last_assistant: Turn | None = None
    with path.open(errors="ignore") as fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = o.get("message") or {}
            ts = parse_ts(o.get("timestamp"))
            if ts is None:
                continue
            cwd = o.get("cwd") or cwd
            entrypoint = entrypoint or o.get("entrypoint", "")
            session_id = o.get("sessionId") or session_id
            content = msg.get("content")
            side = bool(o.get("isSidechain"))
            if t == "assistant":
                tus = [
                    ToolUse(b.get("id", ""), b.get("name", ""), b.get("input") or {}, ts)
                    for b in (content if isinstance(content, list) else [])
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                turn = Turn("assistant", ts, _text_of(content), tus, {}, o.get("uuid", ""), side)
                turns.append(turn)
                last_assistant = turn
            else:
                blocks = content if isinstance(content, list) else []
                results = {
                    b.get("tool_use_id", ""): _result_text(b)
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                if results:
                    if last_assistant is not None:
                        last_assistant.results.update(results)
                    continue
                text = _text_of(content).strip()
                # Harness-injected user messages (`!` command output, task notifications,
                # slash-command echoes, system reminders) all open with a tag. Not prompts.
                if not text or text.startswith("<") or text.startswith(INJECTED_PREFIXES):
                    continue
                text = IMAGE_TOKEN_RE.sub("", text).strip()
                if not text:
                    continue
                turns.append(Turn("user", ts, text, [], {}, o.get("uuid", ""), side))
    return Session(path, session_id, path.parent, cwd, turns, entrypoint)


def final_assistant_text(path: Path) -> str:
    """Last non-empty assistant text in a (sub)agent transcript."""
    last = ""
    with path.open(errors="ignore") as fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") != "assistant":
                continue
            txt = _text_of((o.get("message") or {}).get("content")).strip()
            if txt:
                last = txt
    return last


def keywords(text: str, min_len: int = 3) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9_\-]+", text.lower())
    return {w for w in words if len(w) >= min_len and w not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def window_start(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def read_agent_roster(dirs: list[Path]) -> dict[str, dict]:
    """name -> {description, path, mtime} from agent markdown frontmatter."""
    roster: dict[str, dict] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.md")):
            head = f.read_text(errors="ignore")[:4000]
            m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
            if not m:
                continue
            fm = m.group(1)
            name = re.search(r"^name:\s*(.+)$", fm, re.M)
            desc = re.search(r"^description:\s*(.+)$", fm, re.M)
            if not name:
                continue
            roster[name.group(1).strip().strip('"')] = {
                "description": (desc.group(1).strip().strip('"') if desc else ""),
                "path": str(f),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc),
            }
    return roster
