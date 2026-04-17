#!/usr/bin/env python3
"""
Agent Shared Memory MCP Server (agent_memory_mcp)

PROJECT-LOCAL shared memory for multi-agent sequential workflows.
Memory lives in .agent-mem/ inside the project directory — NOT global.

Design principles:
  - Agents run ONE AT A TIME (after previous agent KIA/done)
  - Every entry is stamped with agent_name + agent_platform for traceability
  - Agents are FORCED to read memory on session start via hooks
  - Agents are FORCED to checkpoint via hooks before session ends
  - If an agent dies (KIA), the next agent gets full context from .agent-mem/

Supports: Claude, Cursor, Codex, Claude Code, AntiGravity, any MCP client.
"""

import json, os, time, hashlib
from datetime import datetime
from typing import Optional, List
from enum import Enum
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

# ── Config — PROJECT LOCAL ──────────────────────────────
PROJECT_ROOT = Path(os.environ.get(
    "AGENT_PROJECT_DIR",
    os.environ.get("CURSOR_PROJECT_DIR",
    os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
))
MEMORY_DIR = PROJECT_ROOT / ".agent-mem"

# ── Tiered Memory Config ────────────────────────────────
# HOT:  recent entries (full detail) — loaded in briefings
# WARM: older entries compressed into per-session digests
# COLD: raw archive on disk, never loaded unless searched
HOT_WINDOW_HOURS = int(os.environ.get("AGENT_MEM_HOT_HOURS", "24"))
MAX_HOT_ENTRIES = int(os.environ.get("AGENT_MEM_MAX_HOT", "50"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COMPACT_MODEL = os.environ.get("AGENT_MEM_MODEL", "claude-sonnet-4-20250514")

# External info/reference folders — colon-separated paths
# e.g. "/Users/swiss/docs/specs:/Users/swiss/shared/reference"
CONTEXT_DIRS = [
    Path(p.strip()) for p in os.environ.get("AGENT_MEM_CONTEXT_DIRS", "").split(":")
    if p.strip()
]

# ── MCP Server ──────────────────────────────────────────
mcp = FastMCP("agent_memory_mcp")

# ── Enums ───────────────────────────────────────────────
class MemoryType(str, Enum):
    DECISION="decision"; PROGRESS="progress"; BLOCKER="blocker"
    CONTEXT="context"; HANDOFF="handoff"; TODO="todo"
    FILE_CHANGE="file_change"; DISCOVERY="discovery"
    WARNING="warning"; CHECKPOINT="checkpoint"

class AgentStatus(str, Enum):
    ACTIVE="active"; KIA="kia"; COMPLETED="completed"; HANDED_OFF="handed_off"

class ResponseFormat(str, Enum):
    MARKDOWN="markdown"; JSON="json"

# ── Storage ─────────────────────────────────────────────
def _ensure():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "checkpoints").mkdir(exist_ok=True)
    gi = PROJECT_ROOT / ".gitignore"
    marker = ".agent-mem/"
    if gi.exists():
        content = gi.read_text()
        if marker not in content:
            with open(gi, "a") as f:
                f.write("\n# Agent shared memory — runtime data, do NOT commit\n.agent-mem/\n")
    else:
        with open(gi, "w") as f:
            f.write("# Agent shared memory — runtime data, do NOT commit\n.agent-mem/\n")

def _load(fp):
    if fp.exists():
        with open(fp, "r", encoding="utf-8") as f: return json.load(f)
    return {}

def _save(fp, data):
    _ensure()
    tmp = fp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.rename(fp)

def _local_now() -> datetime:
    return datetime.now().astimezone()

def _now(): return _local_now().isoformat()
def _id(): return hashlib.md5(f"{time.time()}-{os.urandom(8).hex()}".encode()).hexdigest()[:12]

# Paths
def _mem_p(): return MEMORY_DIR / "memories.json"
def _agt_p(): return MEMORY_DIR / "agents.json"
def _sta_p(): return MEMORY_DIR / "state.json"
def _prj_p(): return MEMORY_DIR / "project.json"

def _load_mem(): return _load(_mem_p()).get("entries", [])
def _save_mem(e): _save(_mem_p(), {"entries": e})
def _load_agt(): return _load(_agt_p())
def _save_agt(a): _save(_agt_p(), a)
def _load_sta(): return _load(_sta_p())
def _save_sta(s): _save(_sta_p(), s)
def _load_prj(): return _load(_prj_p())
def _save_prj(p): _save(_prj_p(), p)

def _mark_prev_kia(exclude=None):
    agents = _load_agt(); changed = False
    for aid, info in agents.items():
        if aid != exclude and info.get("status") == AgentStatus.ACTIVE:
            info["status"] = AgentStatus.KIA
            info["kia_at"] = _now(); info["kia_reason"] = "new_agent_joined"; changed = True
    if changed: _save_agt(agents)

# ── Tiered Memory Engine ────────────────────────────────
def _archive_p(): return MEMORY_DIR / "archive.json"
def _digests_p(): return MEMORY_DIR / "digests.json"

def _load_archive(): return _load(_archive_p()).get("entries", [])
def _save_archive(e): _save(_archive_p(), {"entries": e})
def _load_digests(): return _load(_digests_p()).get("digests", [])
def _save_digests(d): _save(_digests_p(), {"digests": d})

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return len(text) // 4

def _count_mem_tokens(memories: list) -> int:
    """Estimate total tokens across all memory entries."""
    total = 0
    for m in memories:
        total += _estimate_tokens(m.get("title", ""))
        total += _estimate_tokens(m.get("content", ""))
    return total

async def _llm_summarize(entries: list, context: str = "") -> str:
    """Call Claude API to compress a batch of memories into a digest.
    Falls back to rule-based compression if no API key."""
    if not ANTHROPIC_API_KEY:
        return _rule_based_compress(entries)

    entries_text = ""
    for e in entries:
        entries_text += f"[{e['memory_type'].upper()}] {e['title']} (by {e['agent_name']})\n{e['content']}\n---\n"

    prompt = f"""Compress these agent memory entries into a concise digest.
Keep: all decisions, warnings, file changes, and blockers (with who made them).
Summarize: progress entries into one paragraph.
Drop: redundant checkpoints (keep only the last state).
Always preserve agent_name attribution.

{f"Project context: {context}" if context else ""}

ENTRIES:
{entries_text}

Write a structured digest in this format:
## Decisions
- [decision] by [agent_name]

## Progress summary
[1-2 paragraphs]

## Warnings & blockers
- [item] by [agent_name]

## Files changed
- [file] — [what changed] by [agent_name]

## Key discoveries
- [item] by [agent_name]

Keep it under 500 words. Every fact must have agent_name attribution."""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": COMPACT_MODEL,
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]
    except Exception as ex:
        # Fallback to rule-based if API fails
        return _rule_based_compress(entries, error=str(ex))

def _rule_based_compress(entries: list, error: str = "") -> str:
    """Compress without LLM — extract key facts, drop verbose content."""
    lines = []
    if error:
        lines.append(f"*[Compressed without LLM: {error[:80]}]*\n")

    by_type = {}
    for e in entries:
        by_type.setdefault(e["memory_type"], []).append(e)

    # Decisions — keep full
    if "decision" in by_type:
        lines.append("## Decisions")
        for d in by_type["decision"]:
            lines.append(f"- **{d['title']}** (`{d['agent_name']}`): {d['content'][:150]}")

    # Progress — compress to one line each
    if "progress" in by_type:
        lines.append("\n## Progress")
        for p in by_type["progress"]:
            lines.append(f"- {p['title']} (`{p['agent_name']}`)")

    # Warnings, blockers — keep
    for t in ("warning", "blocker"):
        if t in by_type:
            lines.append(f"\n## {t.title()}s")
            for w in by_type[t]:
                lines.append(f"- **{w['title']}** (`{w['agent_name']}`): {w['content'][:150]}")

    # File changes — keep
    if "file_change" in by_type:
        lines.append("\n## Files")
        for f in by_type["file_change"]:
            lines.append(f"- {f['title']} (`{f['agent_name']}`)")

    # Discoveries — keep title only
    if "discovery" in by_type:
        lines.append("\n## Discoveries")
        for d in by_type["discovery"]:
            lines.append(f"- {d['title']} (`{d['agent_name']}`)")

    # Everything else — count only
    other_types = set(by_type.keys()) - {"decision","progress","warning","blocker","file_change","discovery","handoff","checkpoint"}
    for t in other_types:
        lines.append(f"\n*{len(by_type[t])} {t} entries compressed*")

    return "\n".join(lines)

def _split_hot_cold(memories: list) -> tuple:
    """Split memories into hot (recent) and cold (old) based on time and count."""
    if len(memories) <= MAX_HOT_ENTRIES:
        return memories, []

    cutoff_time = time.time() - (HOT_WINDOW_HOURS * 3600)

    # Always keep: pinned, handoffs, latest checkpoint
    hot = []
    cold = []

    for m in memories:
        is_protected = (
            m.get("pinned") or
            m["memory_type"] == MemoryType.HANDOFF or
            m.get("priority", 0) >= 2
        )
        is_recent = m.get("timestamp", 0) >= cutoff_time

        if is_protected or is_recent:
            hot.append(m)
        else:
            cold.append(m)

    # If still too many hot, keep only the most recent MAX_HOT_ENTRIES
    if len(hot) > MAX_HOT_ENTRIES:
        # Sort by priority desc then timestamp desc
        hot.sort(key=lambda m: (m.get("priority", 0), m.get("timestamp", 0)), reverse=True)
        overflow = hot[MAX_HOT_ENTRIES:]
        hot = hot[:MAX_HOT_ENTRIES]
        cold = overflow + cold

    return hot, cold

# ── Input Models ────────────────────────────────────────
class ProjectInitInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    description: str = Field(..., description="What this project is about", min_length=1, max_length=1000)
    tech_stack: Optional[str] = Field(default=None, description="Tech stack summary")

class AgentJoinInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., description="Your unique identity (e.g. 'claude-opus-api', 'cursor-agent-1', 'codex-pr-42')", min_length=1, max_length=100)
    agent_platform: str = Field(..., description="'claude'|'cursor'|'codex'|'claude-code'|'antigravity'|'windsurf'|'other'", min_length=1, max_length=50)
    task_focus: Optional[str] = Field(default=None, description="What you'll work on", max_length=500)

class MemoryWriteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., description="Your agent name (MUST match agent_join)", min_length=1, max_length=100)
    memory_type: MemoryType = Field(..., description="decision|progress|blocker|context|handoff|todo|file_change|discovery|warning|checkpoint")
    title: str = Field(..., description="Short summary", min_length=1, max_length=200)
    content: str = Field(..., description="Detail", min_length=1, max_length=10000)
    tags: Optional[List[str]] = Field(default_factory=list)
    related_files: Optional[List[str]] = Field(default_factory=list)
    priority: Optional[int] = Field(default=0, ge=0, le=3, description="0=normal 3=critical(auto-pin)")

class MemoryReadInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    memory_type: Optional[MemoryType] = None
    tag: Optional[str] = None
    agent_name: Optional[str] = Field(default=None, description="Filter by who wrote it")
    since_minutes: Optional[int] = Field(default=None, ge=1)
    limit: Optional[int] = Field(default=50, ge=1, le=500)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

class CheckpointInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., min_length=1, max_length=5000)
    remaining_tasks: Optional[List[str]] = Field(default_factory=list)
    active_branch: Optional[str] = None
    blockers: Optional[List[str]] = Field(default_factory=list)

class HandoffInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., min_length=1, max_length=5000)
    next_steps: List[str] = Field(..., min_length=1)
    warnings: Optional[List[str]] = Field(default_factory=list)
    files_modified: Optional[List[str]] = Field(default_factory=list)
    files_created: Optional[List[str]] = Field(default_factory=list)

class BriefingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    focus_area: Optional[str] = None
    include_full_history: Optional[bool] = False
    token_budget: Optional[int] = Field(default=4000, description="Max tokens for briefing output. Lower = cheaper. Default 4000.", ge=500, le=50000)

class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=1, max_length=200)
    limit: Optional[int] = Field(default=20, ge=1, le=100)

class PinInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    memory_id: str = Field(..., min_length=1)
    pinned: bool = True

class UpdateStateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., max_length=5000)
    agent_name: Optional[str] = Field(default=None, max_length=100)

class CompactInput(BaseModel):
    """Compact old memories into digests to save tokens."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: Optional[str] = Field(default=None, description="Who is running the compaction", max_length=100)
    force: Optional[bool] = Field(default=False, description="Force compaction even if under threshold")
    use_llm: Optional[bool] = Field(default=True, description="Use Claude API for smart summaries (needs ANTHROPIC_API_KEY)")

class BootstrapInput(BaseModel):
    """Bootstrap memory for an existing project by scanning the codebase."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., description="Your agent name doing the bootstrap", min_length=1, max_length=100)
    description: str = Field(..., description="Project description", min_length=1, max_length=1000)
    tech_stack: Optional[str] = Field(default=None, description="Tech stack")
    scan_readme: Optional[bool] = Field(default=True, description="Read README.md/README for context")
    scan_git: Optional[bool] = Field(default=True, description="Scan recent git log for history")
    scan_structure: Optional[bool] = Field(default=True, description="Scan directory structure")
    scan_config: Optional[bool] = Field(default=True, description="Read package.json, pyproject.toml, etc. for tech stack")
    extra_context: Optional[str] = Field(default=None, description="Any additional context you want to seed", max_length=5000)
    current_task: Optional[str] = Field(default=None, description="What the project should work on next", max_length=1000)
    known_warnings: Optional[List[str]] = Field(default_factory=list, description="Gotchas you already know about")

# ── Tools ───────────────────────────────────────────────

@mcp.tool(name="memory_init", annotations={"title":"Initialize Project Memory","readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_init(params: ProjectInitInput) -> str:
    """Initialize .agent-mem/ in the project root. Call once per project. Safe to re-call."""
    _ensure()
    prj = _load_prj()
    if prj:
        return f"Already initialized at `{MEMORY_DIR}`\n**Description**: {prj.get('description')}\nUse `memory_get_briefing` to catch up."
    prj = {"description": params.description, "tech_stack": params.tech_stack, "project_root": str(PROJECT_ROOT), "created_at": _now()}
    _save_prj(prj); _save_mem([]); _save_agt({}); _save_sta({})
    return f"✅ Initialized at `{MEMORY_DIR}`\n**Project**: {PROJECT_ROOT.name}\n**Description**: {params.description}\n**Tech**: {params.tech_stack or 'N/A'}\n\nNext: `memory_agent_join`"

@mcp.tool(name="memory_agent_join", annotations={"title":"Register as Active Agent","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_agent_join(params: AgentJoinInput) -> str:
    """Register as the active agent. Marks any previous active agent as KIA (sequential model).
    Your agent_name is stamped on EVERY write for traceability."""
    prj = _load_prj()
    if not prj: return "Error: Not initialized. Call `memory_init` first."
    _mark_prev_kia()
    aid = f"{params.agent_platform}-{_id()}"
    agents = _load_agt()
    agents[aid] = {"agent_name": params.agent_name, "agent_platform": params.agent_platform,
                   "task_focus": params.task_focus, "status": AgentStatus.ACTIVE,
                   "joined_at": _now(), "last_activity": time.time(), "memories_written": 0}
    _save_agt(agents)
    lines = [f"✅ Active agent: **{params.agent_name}** ({params.agent_platform})", f"ID: `{aid}`", ""]
    prev = {k:v for k,v in agents.items() if k != aid}
    if prev:
        lines.append("📜 **Previous agents**:")
        for k,v in prev.items():
            e = {"active":"🟢","kia":"💀","completed":"✅","handed_off":"🤝"}.get(v.get("status",""),"❓")
            lines.append(f"  {e} `{v['agent_name']}` ({v['agent_platform']}) — {v['status']}")
        lines.append("")
    mem = _load_mem()
    ho = [m for m in mem if m["memory_type"] == MemoryType.HANDOFF]
    if ho: lines.append(f"🤝 Last handoff from `{ho[-1]['agent_name']}` — read the briefing!\n")
    # Show pending tickets for this agent
    tickets = _load_ticket_index()
    my_tickets = [t for t in tickets if t["status"] in (TicketStatus.OPEN, TicketStatus.REJECTED)
                  and (not t.get("assigned_to")
                       or params.agent_name.lower() in t["assigned_to"].lower()
                       or params.agent_platform.lower() in t["assigned_to"].lower())]
    if my_tickets:
        pri_emoji = {"low":"🟢","medium":"🟡","high":"🟠","critical":"🔴"}
        lines.append(f"🎫 **{len(my_tickets)} ticket(s) waiting for you!**")
        for t in my_tickets:
            pe = pri_emoji.get(t["priority"],"⚪")
            rej = f" ⚠️ rejected {t['rejection_count']}x" if t.get("rejection_count") else ""
            lines.append(f"  {pe} `{t['id']}` — {t['title']} (from `{t['created_by']}`){rej}")
        lines.append(f"Use `memory_list_tickets` for details, `memory_claim_ticket` to start.\n")
    lines += ["⚡ **Protocol**:", "1. `memory_get_briefing` — read context NOW",
              "2. `memory_write` — after EVERY action", "3. `memory_checkpoint` — every 10-15 min",
              "4. `memory_handoff` — before leaving", f"5. Always use agent_name=`{params.agent_name}`"]
    return "\n".join(lines)

@mcp.tool(name="memory_write", annotations={"title":"Write Memory","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_write(params: MemoryWriteInput) -> str:
    """Write a memory entry stamped with your agent_name. Write frequently — if you die, this is all the next agent has."""
    mem = _load_mem()
    entry = {"id": _id(), "agent_name": params.agent_name, "memory_type": params.memory_type,
             "title": params.title, "content": params.content, "tags": params.tags or [],
             "related_files": params.related_files or [], "priority": params.priority or 0,
             "pinned": (params.priority or 0) >= 3, "created_at": _now(), "timestamp": time.time()}
    mem.append(entry); _save_mem(mem)
    agents = _load_agt()
    for a in agents.values():
        if a.get("agent_name") == params.agent_name and a.get("status") == AgentStatus.ACTIVE:
            a["memories_written"] = a.get("memories_written",0) + 1; a["last_activity"] = time.time(); break
    _save_agt(agents)
    e = {"decision":"🏛️","progress":"✅","blocker":"🚫","context":"📖","handoff":"🤝","todo":"📝","file_change":"📁","discovery":"💡","warning":"⚠️","checkpoint":"💾"}.get(params.memory_type,"📌")
    return f"{e} Saved `{entry['id']}` by **{params.agent_name}** | {params.memory_type} | {'🔴'*(params.priority or 0) or '⚪'}\n**{params.title}**"

@mcp.tool(name="memory_read", annotations={"title":"Read Memories","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_read(params: MemoryReadInput) -> str:
    """Read memories with filters. Filter by agent_name to trace who wrote what."""
    mem = _load_mem()
    if not mem: return "No memories yet."
    f = mem
    if params.memory_type: f = [m for m in f if m["memory_type"] == params.memory_type]
    if params.tag: f = [m for m in f if params.tag in m.get("tags",[])]
    if params.agent_name: f = [m for m in f if m.get("agent_name") == params.agent_name]
    if params.since_minutes: cut = time.time()-(params.since_minutes*60); f = [m for m in f if m.get("timestamp",0) >= cut]
    f.sort(key=lambda m: m.get("timestamp",0), reverse=True); f = f[:params.limit]
    if not f: return "No matches."
    if params.response_format == ResponseFormat.JSON: return json.dumps(f, indent=2)
    lines = [f"# 📚 {len(f)} entries\n"]
    for m in f:
        pin = "📌 " if m.get("pinned") else ""
        lines.append(f"### {pin}[{m['memory_type'].upper()}] {m['title']}")
        lines.append(f"*✍️ {m['agent_name']} | {m['created_at']}*\n{m['content']}")
        if m.get("tags"): lines.append(f"Tags: {', '.join(m['tags'])}")
        lines.append("---\n")
    return "\n".join(lines)

@mcp.tool(name="memory_search", annotations={"title":"Search Memories","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_search(params: SearchInput) -> str:
    """Search across all memories by text."""
    mem = _load_mem(); q = params.query.lower()
    hits = [m for m in mem if q in f"{m.get('title','')} {m.get('content','')} {' '.join(m.get('tags',[]))} {m.get('agent_name','')}".lower()]
    hits.sort(key=lambda m: m.get("timestamp",0), reverse=True); hits = hits[:params.limit]
    if not hits: return f"No results for '{params.query}'."
    lines = [f"# 🔍 '{params.query}' — {len(hits)} results\n"]
    for m in hits:
        lines.append(f"### [{m['memory_type'].upper()}] {m['title']}")
        lines.append(f"*✍️ {m['agent_name']}*\n{m['content'][:300]}{'...' if len(m.get('content',''))>300 else ''}\n---\n")
    return "\n".join(lines)

@mcp.tool(name="memory_checkpoint", annotations={"title":"Save Checkpoint","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_checkpoint(params: CheckpointInput) -> str:
    """Full state checkpoint. Do every 10-15 min or before risky ops. Saves both as memory entry AND standalone file."""
    cpd = {"summary": params.summary, "remaining_tasks": params.remaining_tasks or [],
           "active_branch": params.active_branch, "blockers": params.blockers or [], "state": _load_sta()}
    mem = _load_mem()
    entry = {"id": _id(), "agent_name": params.agent_name, "memory_type": MemoryType.CHECKPOINT,
             "title": f"Checkpoint: {params.summary[:80]}", "content": json.dumps(cpd, indent=2, default=str),
             "tags": ["checkpoint"], "related_files": [], "priority": 2, "pinned": True,
             "created_at": _now(), "timestamp": time.time()}
    mem.append(entry); _save_mem(mem)
    ts = _local_now().strftime("%Y%m%d_%H%M%S")
    cp_file = MEMORY_DIR / "checkpoints" / f"cp_{params.agent_name}_{ts}.json"
    _save(cp_file, cpd)
    return f"💾 Checkpoint by `{params.agent_name}` — {len(params.remaining_tasks or [])} tasks left, {len(params.blockers or [])} blockers"

@mcp.tool(name="memory_handoff", annotations={"title":"Handoff to Next Agent","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_handoff(params: HandoffInput) -> str:
    """Formal handoff. ALWAYS call before leaving. Next agent sees this first."""
    content = f"## Summary\n{params.summary}\n\n## Next Steps\n" + "\n".join(f"{i+1}. {s}" for i,s in enumerate(params.next_steps)) + "\n"
    if params.warnings: content += "\n## ⚠️ Warnings\n" + "\n".join(f"- {w}" for w in params.warnings) + "\n"
    if params.files_modified: content += "\n## Modified\n" + "\n".join(f"- `{f}`" for f in params.files_modified) + "\n"
    if params.files_created: content += "\n## Created\n" + "\n".join(f"- `{f}`" for f in params.files_created) + "\n"
    mem = _load_mem()
    entry = {"id": _id(), "agent_name": params.agent_name, "memory_type": MemoryType.HANDOFF,
             "title": f"Handoff from {params.agent_name}", "content": content,
             "tags": ["handoff"], "related_files": (params.files_modified or [])+(params.files_created or []),
             "priority": 3, "pinned": True, "created_at": _now(), "timestamp": time.time()}
    mem.append(entry); _save_mem(mem)
    agents = _load_agt()
    for a in agents.values():
        if a.get("agent_name") == params.agent_name and a.get("status") == AgentStatus.ACTIVE:
            a["status"] = AgentStatus.HANDED_OFF; a["handed_off_at"] = _now(); break
    _save_agt(agents)
    return f"🤝 Handoff from `{params.agent_name}` — {len(params.next_steps)} next steps, {len(params.warnings or [])} warnings"

@mcp.tool(name="memory_get_briefing", annotations={"title":"Get Full Briefing","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_get_briefing(params: BriefingInput) -> str:
    """CALL THIS FIRST. Token-aware briefing with tiered memory.

    Loads: hot memories (full detail) + warm digests (compressed history).
    Respects token_budget to avoid bloating the agent's context window.
    Default 4000 tokens — set lower for cheaper, higher for more detail."""
    prj = _load_prj()
    if not prj: return "No .agent-mem/ found. Run `memory_init`."
    mem = _load_mem(); agents = _load_agt(); state = _load_sta()
    digests = _load_digests()
    budget = params.token_budget or 4000
    used = 0

    def _add(text):
        nonlocal used
        cost = _estimate_tokens(text)
        if used + cost > budget and used > 500:
            return False
        used += cost
        L.append(text)
        return True

    L = []
    _add(f"# 📋 BRIEFING: {PROJECT_ROOT.name}")
    _add(f"**Description**: {prj.get('description')}\n**Tech**: {prj.get('tech_stack','N/A')}\n**Memories**: {len(mem)} hot + {len(digests)} digests\n")

    # Agents — always show
    _add("## 👥 Agent History")
    for aid,a in agents.items():
        e = {"active":"🟢","kia":"💀","completed":"✅","handed_off":"🤝"}.get(a.get("status",""),"❓")
        _add(f"- {e} **{a['agent_name']}** ({a['agent_platform']}) — {a['status']} — {a.get('memories_written',0)} writes")
    _add("")

    # Handoff — highest priority, always include
    ho = [m for m in mem if m["memory_type"]==MemoryType.HANDOFF]
    if ho:
        h = ho[-1]
        _add("## 🤝 LATEST HANDOFF — READ FIRST")
        # Handoffs get full content, but truncate if over budget
        handoff_content = h['content']
        if _estimate_tokens(handoff_content) > budget // 3:
            handoff_content = handoff_content[:budget] + "\n... (truncated for token budget)"
        _add(f"*From **{h['agent_name']}** at {h['created_at']}*\n{handoff_content}\n")

    # Pinned — second priority
    pinned = [m for m in mem if m.get("pinned") and m["memory_type"]!=MemoryType.HANDOFF]
    if pinned:
        _add("## 📌 Pinned")
        for m in pinned[-8:]:
            max_content = min(300, (budget - used) // 4)
            c = m["content"][:max_content] + ("..." if len(m["content"]) > max_content else "")
            if not _add(f"- [{m['memory_type'].upper()}] **{m['title']}** (`{m['agent_name']}`): {c}"):
                break
        _add("")

    # Latest checkpoint
    cps = [m for m in mem if m["memory_type"]==MemoryType.CHECKPOINT]
    if cps:
        cp = cps[-1]
        _add("## 💾 Latest Checkpoint")
        try:
            d = json.loads(cp["content"])
            _add(f"*✍️ {cp['agent_name']}* — {d.get('summary','')}")
            if d.get("remaining_tasks"): _add("**Tasks**: " + " / ".join(d["remaining_tasks"][:8]))
            if d.get("blockers"): _add("**Blockers**: " + " / ".join(d["blockers"][:5]))
        except: _add(cp["content"][:300])
        _add("")

    # Blockers
    bl = [m for m in mem if m["memory_type"]==MemoryType.BLOCKER]
    if bl:
        _add("## 🚫 Blockers")
        for b in bl[-3:]: _add(f"- **{b['title']}** (`{b['agent_name']}`): {b['content'][:150]}")
        _add("")

    # Decisions — compact
    dc = [m for m in mem if m["memory_type"]==MemoryType.DECISION]
    if dc:
        _add("## 🏛️ Decisions")
        for d in dc[-8:]:
            if not _add(f"- **{d['title']}** (`{d['agent_name']}`): {d['content'][:120]}"): break
        _add("")

    # Warnings — compact
    wr = [m for m in mem if m["memory_type"]==MemoryType.WARNING]
    if wr:
        _add("## ⚠️ Warnings")
        for w in wr[-5:]:
            if not _add(f"- **{w['title']}** (`{w['agent_name']}`): {w['content'][:120]}"): break
        _add("")

    # TODOs — titles only
    td = [m for m in mem if m["memory_type"]==MemoryType.TODO]
    if td:
        _add("## 📝 TODOs")
        for t in td[-8:]:
            if not _add(f"- {t['title']} (`{t['agent_name']}`)"): break
        _add("")

    # State — compact
    if state:
        _add("## 🔧 State")
        for k,v in state.items():
            if not k.startswith("_"):
                val = v.get("value",v) if isinstance(v,dict) else v
                who = v.get("updated_by","?") if isinstance(v,dict) else "?"
                if not _add(f"- **{k}**: {str(val)[:150]} (`{who}`)"): break
        _add("")

    # ── WARM MEMORY: Digests (compressed long-term) ──
    if digests:
        _add("## 🗜️ Long-term Memory (compressed)")
        for dg in digests[-5:]:
            remaining = budget - used
            if remaining < 200: break
            max_summary = min(400, remaining // 2)
            summary_truncated = dg["summary"][:max_summary * 4]  # chars, not tokens
            if not _add(f"### Session: {dg['agent_name']} ({dg['period']})\n"
                       f"*{dg['entry_count']} entries → ~{dg['digest_tokens']} tokens | method: {dg['method']}*\n"
                       f"{summary_truncated}\n"): break
        _add("")

    # Focus area
    if params.focus_area:
        all_searchable = mem + _load_archive()
        fc = [m for m in all_searchable if params.focus_area.lower() in f"{m.get('title','')} {' '.join(m.get('tags',[]))}".lower()]
        if fc:
            _add(f"## 🎯 Focus: {params.focus_area}")
            for m in fc[-8:]:
                if not _add(f"- [{m['memory_type'].upper()}] {m['title']} (`{m['agent_name']}`)"): break
            _add("")

    if params.include_full_history:
        _add("## 📜 Full History")
        for m in mem:
            if not _add(f"[{m['memory_type'].upper()}] {m['title']} by `{m['agent_name']}`: {m['content'][:200]}\n---"): break

    _add("---")
    _add(f"*Briefing: ~{used:,} tokens used of {budget:,} budget*")

    # Context dirs hint
    if CONTEXT_DIRS:
        avail = [str(d.name) for d in CONTEXT_DIRS if d.exists()]
        if avail:
            _add(f"\n📂 **Reference dirs available**: {', '.join(avail)} — use `memory_context_dirs` to browse")

    # Tickets
    tickets = _load_ticket_index()
    open_tickets = [t for t in tickets if t["status"] in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW)]
    if open_tickets:
        pri_emoji = {"low":"🟢","medium":"🟡","high":"🟠","critical":"🔴"}
        _add(f"## 🎫 Open Tickets ({len(open_tickets)})")
        for t in sorted(open_tickets, key=lambda x: {"critical":0,"high":1,"medium":2,"low":3}.get(x["priority"],9)):
            pe = pri_emoji.get(t["priority"],"⚪")
            assign = f"→ `{t['assigned_to']}`" if t.get("assigned_to") else "→ any"
            _add(f"- {pe} `{t['id']}` **{t['title']}** ({t['status']}) {assign} — from `{t['created_by']}`")
        _add("")

    L += ["## 🚀 Protocol","1. `memory_agent_join` — register","2. `memory_write` — after EVERY action",
          "3. `memory_checkpoint` — every 10-15 min","4. `memory_handoff` — before leaving",
          "5. `memory_compact` — when token_usage gets high"]
    return "\n".join(L)

@mcp.tool(name="memory_status", annotations={"title":"Status","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_status() -> str:
    """Quick status dashboard."""
    mem = _load_mem(); agents = _load_agt()
    tc = {}
    for m in mem: tc[m.get("memory_type","?")] = tc.get(m.get("memory_type","?"),0)+1
    L = [f"# 📊 {PROJECT_ROOT.name}", f"`{MEMORY_DIR}`", f"Total: {len(mem)} memories\n## Agents"]
    for a in agents.values():
        e = {"active":"🟢","kia":"💀","completed":"✅","handed_off":"🤝"}.get(a.get("status",""),"❓")
        L.append(f"- {e} **{a['agent_name']}** ({a['agent_platform']}) — {a.get('memories_written',0)} writes")
    L.append("\n## Types")
    for t,c in sorted(tc.items()): L.append(f"- {t}: {c}")
    if mem:
        l = mem[-1]; L.append(f"\n## Last: `{l['agent_name']}` [{l['memory_type']}] {l['title']}")
    return "\n".join(L)

@mcp.tool(name="memory_pin", annotations={"title":"Pin/Unpin","readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_pin(params: PinInput) -> str:
    """Pin/unpin a memory."""
    mem = _load_mem()
    for m in mem:
        if m["id"] == params.memory_id:
            m["pinned"] = params.pinned; _save_mem(mem)
            return f"{'📌 Pinned' if params.pinned else '📍 Unpinned'}: {m['title']}"
    return f"ID '{params.memory_id}' not found."

@mcp.tool(name="memory_update_state", annotations={"title":"Update State","readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_update_state(params: UpdateStateInput) -> str:
    """Update shared key-value state. Stamped with agent_name."""
    s = _load_sta()
    s[params.key] = {"value": params.value, "updated_at": _now(), "updated_by": params.agent_name or "unknown"}
    _save_sta(s)
    return f"🔧 `{params.key}` = `{params.value[:100]}` (by `{params.agent_name or '?'}`)"

# ── Token-Saving Tools ──────────────────────────────────

@mcp.tool(name="memory_compact", annotations={"title":"Compact Old Memories","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":True})
async def memory_compact(params: CompactInput) -> str:
    """Compact old memories into compressed digests to save tokens.

    How it works:
    1. Splits memories into HOT (recent/pinned) and COLD (old)
    2. Groups cold entries by agent session
    3. Compresses each group into a digest (LLM or rule-based)
    4. Archives raw cold entries to archive.json
    5. Keeps only hot entries in memories.json + adds digest references

    Token savings: typically 60-80% reduction on old entries.

    Config via env vars:
    - AGENT_MEM_HOT_HOURS: hours to keep full detail (default: 24)
    - AGENT_MEM_MAX_HOT: max hot entries (default: 50)
    - ANTHROPIC_API_KEY: for LLM-powered summaries
    """
    mem = _load_mem()
    if not mem:
        return "Nothing to compact."

    hot, cold = _split_hot_cold(mem)

    if not cold and not params.force:
        return f"Nothing to compact. {len(hot)} entries, all recent/pinned. (Use force=true to override)"

    if not cold:
        return f"All {len(hot)} entries are protected (pinned/recent/high-priority). Nothing to compact."

    # Token counts before
    tokens_before = _count_mem_tokens(mem)

    # Group cold entries by agent_name for per-session digests
    by_agent = {}
    for m in cold:
        agent = m.get("agent_name", "unknown")
        by_agent.setdefault(agent, []).append(m)

    # Archive raw cold entries
    archive = _load_archive()
    archive.extend(cold)
    _save_archive(archive)

    # Create digests
    prj = _load_prj()
    context = prj.get("description", "") if prj else ""
    digests = _load_digests()
    new_digest_titles = []

    for agent_name, entries in by_agent.items():
        if params.use_llm and ANTHROPIC_API_KEY:
            summary = await _llm_summarize(entries, context)
        else:
            summary = _rule_based_compress(entries)

        ts_range = ""
        timestamps = [e.get("created_at", "") for e in entries]
        if timestamps:
            ts_range = f"{timestamps[0][:10]} → {timestamps[-1][:10]}"

        digest = {
            "id": _id(),
            "agent_name": agent_name,
            "period": ts_range,
            "entry_count": len(entries),
            "original_tokens": _count_mem_tokens(entries),
            "digest_tokens": _estimate_tokens(summary),
            "summary": summary,
            "compressed_at": _now(),
            "method": "llm" if (params.use_llm and ANTHROPIC_API_KEY) else "rule-based",
        }
        digests.append(digest)
        new_digest_titles.append(f"`{agent_name}` ({len(entries)} entries → ~{digest['digest_tokens']} tokens)")

    _save_digests(digests)

    # Replace memories.json with only hot entries
    _save_mem(hot)

    # Token counts after
    tokens_after = _count_mem_tokens(hot) + sum(_estimate_tokens(d["summary"]) for d in digests)
    saved = tokens_before - tokens_after
    pct = (saved / tokens_before * 100) if tokens_before > 0 else 0

    return (
        f"🗜️ Compaction complete!\n\n"
        f"**Before**: {len(mem)} entries (~{tokens_before:,} tokens)\n"
        f"**After**: {len(hot)} hot + {len(digests)} digests (~{tokens_after:,} tokens)\n"
        f"**Saved**: ~{saved:,} tokens ({pct:.0f}% reduction)\n"
        f"**Archived**: {len(cold)} entries to archive.json\n\n"
        f"**New digests**:\n" + "\n".join(f"- {t}" for t in new_digest_titles) +
        f"\n\nMethod: {'LLM (Claude)' if (params.use_llm and ANTHROPIC_API_KEY) else 'rule-based'}"
    )


@mcp.tool(name="memory_token_usage", annotations={"title":"Token Usage Report","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_token_usage() -> str:
    """Show token usage breakdown and recommend compaction if needed.

    Returns token estimates for: hot memories, digests, archive, briefing output.
    """
    mem = _load_mem()
    digests = _load_digests()
    archive = _load_archive()

    hot_tokens = _count_mem_tokens(mem)
    digest_tokens = sum(_estimate_tokens(d.get("summary", "")) for d in digests)
    archive_tokens = _count_mem_tokens(archive)

    # Estimate briefing output
    briefing_est = hot_tokens + digest_tokens + 500  # overhead

    hot, cold = _split_hot_cold(mem)
    cold_tokens = _count_mem_tokens(cold)

    lines = [
        f"# 📊 Token Usage Report",
        f"",
        f"## Current Memory",
        f"- **Hot entries**: {len(mem)} (~{hot_tokens:,} tokens)",
        f"  - Recent/pinned: {len(hot)}",
        f"  - Compactable: {len(cold)} (~{cold_tokens:,} tokens)",
        f"- **Digests**: {len(digests)} (~{digest_tokens:,} tokens)",
        f"- **Archive**: {len(archive)} entries (~{archive_tokens:,} tokens, on disk only)",
        f"",
        f"## Briefing Cost",
        f"- Estimated briefing: ~{briefing_est:,} tokens",
        f"- That's ~{briefing_est/1000:.1f}k of an agent's context window",
        f"",
    ]

    if cold_tokens > 2000:
        potential_saving = int(cold_tokens * 0.7)  # ~70% savings typical
        lines.append(f"## 💡 Recommendation")
        lines.append(f"Run `memory_compact` to save ~{potential_saving:,} tokens ({len(cold)} old entries)")
        if not ANTHROPIC_API_KEY:
            lines.append(f"Set ANTHROPIC_API_KEY for LLM-powered summaries (better quality)")
    else:
        lines.append("✅ Memory is lean — no compaction needed.")

    return "\n".join(lines)


@mcp.tool(name="memory_search_archive", annotations={"title":"Search Archive","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_search_archive(params: SearchInput) -> str:
    """Search the cold archive (compacted entries). Use when you need old details
    that were compressed out of the hot memory."""
    archive = _load_archive()
    if not archive:
        return "Archive is empty. No compacted entries yet."

    q = params.query.lower()
    hits = [m for m in archive if q in
            f"{m.get('title','')} {m.get('content','')} {' '.join(m.get('tags',[]))} {m.get('agent_name','')}".lower()]
    hits.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
    hits = hits[:params.limit]

    if not hits:
        return f"No archived entries matching '{params.query}'."

    lines = [f"# 🗄️ Archive Search: '{params.query}' ({len(hits)} results)\n"]
    for m in hits:
        lines.append(f"### [{m['memory_type'].upper()}] {m['title']}")
        lines.append(f"*✍️ {m['agent_name']} | {m['created_at']}*")
        lines.append(f"{m['content'][:400]}{'...' if len(m.get('content',''))>400 else ''}\n---\n")
    return "\n".join(lines)

# ── Bootstrap for Existing Projects ─────────────────────

def _safe_read(path: Path, max_chars: int = 3000) -> str:
    """Read a file safely, truncating if too large."""
    try:
        if path.exists() and path.is_file() and path.stat().st_size < 500_000:
            text = path.read_text(encoding="utf-8", errors="replace")
            return text[:max_chars] + ("..." if len(text) > max_chars else "")
    except:
        pass
    return ""

def _scan_directory_structure(root: Path, max_depth: int = 3) -> str:
    """Scan directory tree, skip common noise."""
    skip = {".git", "node_modules", "__pycache__", ".next", ".nuxt", "dist",
            "build", ".agent-mem", ".agent-mem-hooks", "venv", ".venv", "env",
            ".env", ".idea", ".vscode", "coverage", ".cache", "target"}
    lines = []

    def _walk(p: Path, depth: int, prefix: str = ""):
        if depth > max_depth:
            return
        try:
            items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return

        dirs = [i for i in items if i.is_dir() and i.name not in skip and not i.name.startswith(".")]
        files = [i for i in items if i.is_file() and not i.name.startswith(".")]

        for f in files[:15]:  # Cap files per dir
            lines.append(f"{prefix}{f.name}")
        if len(files) > 15:
            lines.append(f"{prefix}... +{len(files)-15} more files")

        for d in dirs[:10]:
            lines.append(f"{prefix}{d.name}/")
            _walk(d, depth + 1, prefix + "  ")

    _walk(root, 0)
    return "\n".join(lines[:100])  # Cap total lines

def _scan_git_log(root: Path, n: int = 15) -> str:
    """Get recent git log."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", f"--oneline", f"-{n}", "--no-decorate"],
            cwd=str(root), capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return ""

def _scan_git_branch(root: Path) -> str:
    """Get current branch."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(root), capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return ""

def _detect_tech_stack(root: Path) -> str:
    """Auto-detect tech stack from config files."""
    signals = []
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text())
            deps = list((pkg.get("dependencies", {}) | pkg.get("devDependencies", {})).keys())
            frameworks = [d for d in deps if d in (
                "react", "vue", "svelte", "next", "nuxt", "angular", "express",
                "fastify", "hono", "astro", "remix", "solid-js", "tailwindcss",
                "typescript", "vite", "webpack", "prisma", "drizzle-orm"
            )]
            signals.append(f"Node.js ({', '.join(frameworks[:6])})" if frameworks else "Node.js")
        except: signals.append("Node.js")
    if (root / "pyproject.toml").exists(): signals.append("Python")
    if (root / "requirements.txt").exists(): signals.append("Python")
    if (root / "Cargo.toml").exists(): signals.append("Rust")
    if (root / "go.mod").exists(): signals.append("Go")
    if (root / "Gemfile").exists(): signals.append("Ruby")
    if (root / "docker-compose.yml").exists() or (root / "docker-compose.yaml").exists(): signals.append("Docker")
    if (root / "Dockerfile").exists(): signals.append("Docker")
    if (root / ".env").exists(): signals.append("env-file")
    if (root / "tsconfig.json").exists(): signals.append("TypeScript")
    return " / ".join(signals) if signals else "Unknown"

@mcp.tool(name="memory_bootstrap", annotations={"title":"Bootstrap Existing Project","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_bootstrap(params: BootstrapInput) -> str:
    """Bootstrap .agent-mem/ for an EXISTING project by auto-scanning the codebase.

    Reads: README, git log, directory structure, package configs.
    Seeds memory with: project context, tech stack, recent history, structure, warnings.

    Use this instead of memory_init when joining an existing project for the first time.
    After bootstrap, the next agent gets full context without you manually typing everything.

    Returns:
        str: Bootstrap summary with what was discovered and seeded.
    """
    _ensure()
    prj = _load_prj()
    if prj and _load_mem():
        return (f"⚠️ Project already has memory ({len(_load_mem())} entries).\n"
                f"Use `memory_get_briefing` instead. If you want to re-bootstrap, delete `.agent-mem/` first.")

    # Auto-detect tech if not provided
    tech = params.tech_stack or _detect_tech_stack(PROJECT_ROOT)

    # Init project
    prj = {"description": params.description, "tech_stack": tech,
            "project_root": str(PROJECT_ROOT), "created_at": _now(), "bootstrapped": True}
    _save_prj(prj)
    _save_agt({})
    _save_sta({})

    entries = []
    discoveries = []

    def _add_entry(mtype, title, content, tags=None, priority=0):
        entries.append({
            "id": _id(), "agent_name": params.agent_name, "memory_type": mtype,
            "title": title, "content": content, "tags": tags or [],
            "related_files": [], "priority": priority,
            "pinned": priority >= 3, "created_at": _now(), "timestamp": time.time()
        })

    # ── Scan README ──
    if params.scan_readme:
        for readme_name in ("README.md", "README.rst", "README.txt", "README"):
            readme_text = _safe_read(PROJECT_ROOT / readme_name, 2000)
            if readme_text:
                _add_entry(MemoryType.CONTEXT, f"Project README ({readme_name})",
                          readme_text, tags=["bootstrap", "readme"], priority=2)
                discoveries.append(f"📖 Read {readme_name} ({len(readme_text)} chars)")
                break

    # ── Scan directory structure ──
    if params.scan_structure:
        structure = _scan_directory_structure(PROJECT_ROOT)
        if structure:
            _add_entry(MemoryType.CONTEXT, "Codebase structure",
                      f"```\n{structure}\n```", tags=["bootstrap", "structure"], priority=1)
            discoveries.append(f"📁 Scanned directory ({structure.count(chr(10))+1} items)")

    # ── Scan git ──
    if params.scan_git:
        branch = _scan_git_branch(PROJECT_ROOT)
        log = _scan_git_log(PROJECT_ROOT, 20)
        if log:
            git_content = f"**Branch**: {branch or 'unknown'}\n\n**Recent commits**:\n```\n{log}\n```"
            _add_entry(MemoryType.CONTEXT, f"Git history (branch: {branch or '?'})",
                      git_content, tags=["bootstrap", "git"], priority=1)
            discoveries.append(f"🔀 Git: branch `{branch}`, {log.count(chr(10))+1} recent commits")
            if branch:
                _save_sta({"current_branch": {"value": branch, "updated_at": _now(), "updated_by": params.agent_name}})

    # ── Scan config files ──
    if params.scan_config:
        config_files = [
            ("package.json", ["dependencies", "scripts"]),
            ("pyproject.toml", None),
            ("Cargo.toml", None),
            ("tsconfig.json", None),
        ]
        for cfname, _ in config_files:
            cftext = _safe_read(PROJECT_ROOT / cfname, 1500)
            if cftext:
                _add_entry(MemoryType.CONTEXT, f"Config: {cfname}",
                          f"```\n{cftext}\n```", tags=["bootstrap", "config"])
                discoveries.append(f"⚙️ Read {cfname}")

    # ── Tech stack decision ──
    _add_entry(MemoryType.DECISION, f"Tech stack: {tech}",
              f"Detected/declared tech stack: {tech}\nProject root: {PROJECT_ROOT}",
              tags=["bootstrap", "tech"], priority=2)

    # ── Extra context ──
    if params.extra_context:
        _add_entry(MemoryType.CONTEXT, "Additional context (human-provided)",
                  params.extra_context, tags=["bootstrap", "human"], priority=2)
        discoveries.append("📝 Added human-provided context")

    # ── Known warnings ──
    for w in (params.known_warnings or []):
        _add_entry(MemoryType.WARNING, w, w, tags=["bootstrap", "human-warning"], priority=2)
        discoveries.append(f"⚠️ Warning: {w[:60]}")

    # ── Current task ──
    if params.current_task:
        _add_entry(MemoryType.TODO, params.current_task, params.current_task,
                  tags=["bootstrap", "active-task"], priority=2)
        discoveries.append(f"📝 TODO: {params.current_task[:60]}")

    # ── Bootstrap checkpoint ──
    _add_entry(MemoryType.CHECKPOINT, "Bootstrap checkpoint",
              json.dumps({
                  "summary": f"Bootstrapped from existing project. {len(entries)} entries seeded.",
                  "remaining_tasks": [params.current_task] if params.current_task else [],
                  "blockers": [],
                  "active_branch": _scan_git_branch(PROJECT_ROOT) if params.scan_git else None,
              }, indent=2),
              tags=["bootstrap", "checkpoint"], priority=2)

    _save_mem(entries)

    # Register the bootstrap agent
    aid = f"{params.agent_name.split('-')[0] if '-' in params.agent_name else 'bootstrap'}-{_id()}"
    agents = {aid: {
        "agent_name": params.agent_name, "agent_platform": "bootstrap",
        "task_focus": "Initial memory bootstrap", "status": AgentStatus.ACTIVE,
        "joined_at": _now(), "last_activity": time.time(), "memories_written": len(entries)
    }}
    _save_agt(agents)

    # Build response
    lines = [
        f"🚀 **Bootstrap complete!**\n",
        f"**Project**: {PROJECT_ROOT.name}",
        f"**Tech**: {tech}",
        f"**Memories seeded**: {len(entries)}",
        f"**Token cost**: ~{_count_mem_tokens(entries):,} tokens\n",
        f"## Discoveries",
    ]
    lines.extend(f"- {d}" for d in discoveries)
    lines.append(f"\n## What's in .agent-mem/ now")
    by_type = {}
    for e in entries:
        by_type.setdefault(e["memory_type"], []).append(e)
    for t, es in sorted(by_type.items()):
        lines.append(f"- {t}: {len(es)} entries")

    lines.append(f"\n## Next steps")
    lines.append(f"1. Review the seeded memories: `memory_read`")
    lines.append(f"2. Add any missing context: `memory_write(type='context', ...)`")
    lines.append(f"3. Add known decisions/warnings: `memory_write(type='decision|warning', ...)`")
    lines.append(f"4. Hand off: `memory_handoff` — then the next agent is fully onboarded")

    return "\n".join(lines)

# ── Context Dirs (external info folders) ────────────────

def _scan_context_dir(d: Path, max_files: int = 20) -> list:
    """List readable files in an external context dir."""
    if not d.exists() or not d.is_dir():
        return []
    files = []
    skip_ext = {".pyc", ".pyo", ".so", ".dylib", ".o", ".exe", ".dll",
                ".zip", ".tar", ".gz", ".jpg", ".png", ".gif", ".mp4"}
    try:
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() not in skip_ext and not any(
                p.startswith(".") for p in f.relative_to(d).parts
            ):
                files.append(f)
                if len(files) >= max_files:
                    break
    except PermissionError:
        pass
    return files

class ContextDirReadInput(BaseModel):
    """Read a file from one of the configured context directories."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    filename: str = Field(..., description="Filename or relative path to read from context dirs", min_length=1, max_length=500)
    max_chars: Optional[int] = Field(default=3000, description="Max characters to return", ge=100, le=50000)

@mcp.tool(name="memory_context_dirs", annotations={"title":"List Context Directories","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_context_dirs() -> str:
    """List all configured external context/info directories and their files.

    Context dirs are set via AGENT_MEM_CONTEXT_DIRS env var (colon-separated paths).
    These folders contain reference docs, specs, shared info that agents should know about
    but live outside the project folder.

    Returns:
        str: List of context dirs and their contents.
    """
    if not CONTEXT_DIRS:
        return ("No context directories configured.\n\n"
                "Set `AGENT_MEM_CONTEXT_DIRS` in your MCP config env:\n"
                "```\n\"env\": {\n  \"AGENT_MEM_CONTEXT_DIRS\": \"/path/to/docs:/path/to/specs\"\n}\n```")

    lines = ["# 📂 External Context Directories\n"]
    for d in CONTEXT_DIRS:
        if not d.exists():
            lines.append(f"## ❌ `{d}` — not found")
            continue
        files = _scan_context_dir(d)
        lines.append(f"## 📁 `{d}`")
        lines.append(f"*{len(files)} readable files*\n")
        for f in files:
            rel = f.relative_to(d)
            size = f.stat().st_size
            size_str = f"{size:,} bytes" if size < 10000 else f"{size/1024:.1f} KB"
            lines.append(f"- `{rel}` ({size_str})")
        lines.append("")

    lines.append("Use `memory_context_read(filename='...')` to read any file.")
    return "\n".join(lines)

@mcp.tool(name="memory_context_read", annotations={"title":"Read from Context Dir","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_context_read(params: ContextDirReadInput) -> str:
    """Read a file from the external context directories.

    Searches all configured AGENT_MEM_CONTEXT_DIRS for the filename.
    Use memory_context_dirs to list available files first.

    Returns:
        str: File content (truncated to max_chars).
    """
    if not CONTEXT_DIRS:
        return "No context directories configured. Set AGENT_MEM_CONTEXT_DIRS."

    # Search all context dirs for the file
    for d in CONTEXT_DIRS:
        candidate = d / params.filename
        if candidate.exists() and candidate.is_file():
            content = _safe_read(candidate, params.max_chars)
            if content:
                return (f"📄 **{params.filename}** (from `{d}`)\n"
                        f"*{candidate.stat().st_size:,} bytes*\n\n"
                        f"```\n{content}\n```")
            return f"File exists but could not be read: `{candidate}`"

    # Try fuzzy: search by filename only
    target = Path(params.filename).name
    for d in CONTEXT_DIRS:
        for f in _scan_context_dir(d, 50):
            if f.name == target:
                content = _safe_read(f, params.max_chars)
                if content:
                    rel = f.relative_to(d)
                    return (f"📄 **{rel}** (from `{d}`)\n"
                            f"*{f.stat().st_size:,} bytes*\n\n"
                            f"```\n{content}\n```")

    return f"File `{params.filename}` not found in any context directory."


# ── Ticketing System (file-based) ────────────────────────
#
# Folder structure inside .agent-mem/:
#   tickets/
#   ├── TK-abc123.md              ← OPEN tickets (root = queue)
#   ├── review/                   ← Agent submitted work for review
#   │   └── TK-abc123-submit.md
#   ├── closed/                   ← Approved & done
#   │   ├── TK-abc123.md          ← Original ticket (moved here)
#   │   └── TK-abc123-submit.md   ← Submitted work (moved here)
#   └── rejected/                 ← Failed review
#       ├── TK-abc123.md          ← Original ticket (moved here)
#       ├── TK-abc123-submit.md   ← Submitted work (moved here)
#       └── TK-abc123-rejected.md ← Rejection note + how to fix

import shutil

def _tickets_dir() -> Path:
    d = MEMORY_DIR / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "review").mkdir(exist_ok=True)
    (d / "closed").mkdir(exist_ok=True)
    (d / "rejected").mkdir(exist_ok=True)
    return d

def _ticket_index_p() -> Path:
    return MEMORY_DIR / "tickets" / "_index.json"

def _load_ticket_index() -> list:
    return _load(_ticket_index_p()).get("tickets", [])

def _save_ticket_index(tickets: list):
    _save(_ticket_index_p(), {"tickets": tickets})

def _write_ticket_md(filepath: Path, data: dict):
    """Write a ticket as a human-readable .md file."""
    lines = [f"# {data.get('title', 'Untitled')}"]
    lines.append(f"**ID**: `{data.get('id', '?')}`")
    lines.append(f"**Status**: {data.get('status', '?')}")
    lines.append(f"**Priority**: {data.get('priority', '?')}")
    lines.append(f"**Created by**: `{data.get('created_by', '?')}`")
    lines.append(f"**Created at**: {data.get('created_at', '?')}")
    if data.get("assigned_to"):
        lines.append(f"**Assigned to**: `{data['assigned_to']}`")
    if data.get("tags"):
        lines.append(f"**Tags**: {', '.join(data['tags'])}")
    if data.get("related_files"):
        lines.append(f"**Files**: {', '.join(data['related_files'])}")
    lines.append(f"\n---\n")
    lines.append(data.get("description", ""))
    filepath.write_text("\n".join(lines), encoding="utf-8")


class TicketPriority(str, Enum):
    LOW = "low"; MEDIUM = "medium"; HIGH = "high"; CRITICAL = "critical"

class TicketStatus(str, Enum):
    OPEN = "open"; IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"; CLOSED = "closed"; REJECTED = "rejected"

class CreateTicketInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., description="Who is creating this ticket", min_length=1, max_length=100)
    title: str = Field(..., description="Short ticket title", min_length=1, max_length=200)
    description: str = Field(..., description="What needs to be done — be specific", min_length=1, max_length=5000)
    priority: TicketPriority = Field(default=TicketPriority.MEDIUM)
    assigned_to: Optional[str] = Field(default=None, description="Agent name or platform to assign (e.g. 'cursor', 'codex'). Empty = any agent.")
    tags: Optional[List[str]] = Field(default_factory=list)
    related_files: Optional[List[str]] = Field(default_factory=list)

class ClaimTicketInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., min_length=1, max_length=100)
    ticket_id: str = Field(..., description="Ticket ID to claim", min_length=1)

class SubmitTicketInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., min_length=1, max_length=100)
    ticket_id: str = Field(..., min_length=1)
    summary: str = Field(..., description="What was done", min_length=1, max_length=5000)
    files_changed: Optional[List[str]] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, description="Any additional notes for reviewer", max_length=2000)

class ReviewTicketInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_name: str = Field(..., description="Reviewer agent name", min_length=1, max_length=100)
    ticket_id: str = Field(..., min_length=1)
    verdict: str = Field(..., description="'approve' or 'reject'", pattern="^(approve|reject)$")
    review_notes: str = Field(..., description="Review feedback", min_length=1, max_length=5000)
    fix_instructions: Optional[str] = Field(default=None, description="If rejected: how to fix", max_length=5000)

class ListTicketsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: Optional[TicketStatus] = Field(default=None, description="Filter by status")
    assigned_to: Optional[str] = Field(default=None, description="Filter by assignee")
    include_closed: Optional[bool] = Field(default=False, description="Include closed/rejected tickets")


@mcp.tool(name="memory_create_ticket", annotations={"title":"Create Ticket","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_create_ticket(params: CreateTicketInput) -> str:
    """Create a ticket requesting help from another agent.

    Ticket is saved as .md file in tickets/ (open queue).
    Assign to a specific agent/platform or leave open for anyone.

    Examples:
    - PM needs coder: assigned_to="cursor"
    - Coder needs review: assigned_to="claude"
    - Anyone can pick up: assigned_to=None
    """
    _tickets_dir()
    ticket_id = f"TK-{_id()}"
    ticket_data = {
        "id": ticket_id,
        "title": params.title,
        "description": params.description,
        "priority": params.priority,
        "status": TicketStatus.OPEN,
        "created_by": params.agent_name,
        "assigned_to": params.assigned_to,
        "claimed_by": None,
        "tags": params.tags or [],
        "related_files": params.related_files or [],
        "created_at": _now(),
        "updated_at": _now(),
        "timestamp": time.time(),
    }

    # Save .md file in tickets root (= open queue)
    _write_ticket_md(_tickets_dir() / f"{ticket_id}.md", ticket_data)

    # Update index
    idx = _load_ticket_index()
    idx.append(ticket_data)
    _save_ticket_index(idx)

    assign_str = f"→ assigned to **{params.assigned_to}**" if params.assigned_to else "→ open for any agent"
    pe = {"low":"🟢","medium":"🟡","high":"🟠","critical":"🔴"}[params.priority]
    return (
        f"🎫 Ticket created: `{ticket_id}`\n"
        f"{pe} **{params.priority.upper()}** | {params.title}\n"
        f"By `{params.agent_name}` {assign_str}\n"
        f"File: `tickets/{ticket_id}.md`"
    )


@mcp.tool(name="memory_claim_ticket", annotations={"title":"Claim Ticket","readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_claim_ticket(params: ClaimTicketInput) -> str:
    """Claim an open ticket. Changes status to in_progress."""
    idx = _load_ticket_index()
    for t in idx:
        if t["id"] == params.ticket_id:
            if t["status"] not in (TicketStatus.OPEN,):
                return f"Ticket `{t['id']}` is already {t['status']}."
            t["status"] = TicketStatus.IN_PROGRESS
            t["claimed_by"] = params.agent_name
            t["updated_at"] = _now()
            _save_ticket_index(idx)
            # Update .md file
            _write_ticket_md(_tickets_dir() / f"{t['id']}.md", t)
            return (
                f"🔧 Claimed `{t['id']}`: **{t['title']}**\n"
                f"Now in progress by `{params.agent_name}`\n"
                f"When done, use `memory_submit_ticket` to submit for review."
            )
    return f"Ticket `{params.ticket_id}` not found."


@mcp.tool(name="memory_submit_ticket", annotations={"title":"Submit Work for Review","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_submit_ticket(params: SubmitTicketInput) -> str:
    """Submit completed work on a ticket for review.

    Creates a submission report in tickets/review/ and moves ticket to review status.
    Another agent (reviewer/PM) will approve or reject.
    """
    idx = _load_ticket_index()
    for t in idx:
        if t["id"] == params.ticket_id:
            if t["status"] not in (TicketStatus.IN_PROGRESS, TicketStatus.REJECTED):
                return f"Ticket `{t['id']}` is {t['status']} — can only submit in_progress or rejected tickets."
            t["status"] = TicketStatus.IN_REVIEW
            t["updated_at"] = _now()
            _save_ticket_index(idx)

            # Update ticket .md
            _write_ticket_md(_tickets_dir() / f"{t['id']}.md", t)

            # Create submission report
            submit_lines = [
                f"# Submission: {t['title']}",
                f"**Ticket**: `{t['id']}`",
                f"**Submitted by**: `{params.agent_name}`",
                f"**Submitted at**: {_now()}",
                f"**Original request by**: `{t['created_by']}`",
            ]
            if params.files_changed:
                submit_lines.append(f"\n## Files Changed")
                for f in params.files_changed:
                    submit_lines.append(f"- `{f}`")
            submit_lines.append(f"\n## Summary")
            submit_lines.append(params.summary)
            if params.notes:
                submit_lines.append(f"\n## Notes")
                submit_lines.append(params.notes)

            submit_path = _tickets_dir() / "review" / f"{t['id']}-submit.md"
            submit_path.write_text("\n".join(submit_lines), encoding="utf-8")

            # Auto-handoff: agent submitted work, should leave for reviewer
            agents = _load_agt()
            for a in agents.values():
                if a.get("agent_name") == params.agent_name and a.get("status") == AgentStatus.ACTIVE:
                    a["status"] = AgentStatus.HANDED_OFF
                    a["handed_off_at"] = _now()
                    break
            _save_agt(agents)

            # Write handoff memory
            mem = _load_mem()
            mem.append({
                "id": _id(), "agent_name": params.agent_name,
                "memory_type": MemoryType.HANDOFF,
                "title": f"Auto-handoff after submitting {t['id']}",
                "content": (
                    f"## Summary\nSubmitted ticket `{t['id']}`: {t['title']}\n\n"
                    f"## What was done\n{params.summary}\n\n"
                    f"## Next Steps\n1. Reviewer: check `tickets/review/{t['id']}-submit.md`\n"
                    f"2. Approve → `memory_review_ticket(verdict='approve')`\n"
                    f"3. Reject → `memory_review_ticket(verdict='reject')`"
                ),
                "tags": ["handoff", "ticket", "auto"],
                "related_files": params.files_changed or [],
                "priority": 3, "pinned": True,
                "created_at": _now(), "timestamp": time.time()
            })
            _save_mem(mem)

            return (
                f"📤 Submitted `{t['id']}` for review!\n"
                f"**{t['title']}** by `{params.agent_name}`\n"
                f"Report: `tickets/review/{t['id']}-submit.md`\n\n"
                f"🤝 Auto-handoff: you're checked out. Reviewer will pick this up."
            )
    return f"Ticket `{params.ticket_id}` not found."


@mcp.tool(name="memory_review_ticket", annotations={"title":"Review Submitted Ticket","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":False})
async def memory_review_ticket(params: ReviewTicketInput) -> str:
    """Review a submitted ticket. Approve → closed/ or Reject → rejected/.

    On approve: moves ticket + submission to closed/
    On reject: moves to rejected/ + creates rejection note with fix instructions.
    """
    tdir = _tickets_dir()
    idx = _load_ticket_index()
    for t in idx:
        if t["id"] == params.ticket_id:
            if t["status"] != TicketStatus.IN_REVIEW:
                return f"Ticket `{t['id']}` is {t['status']} — can only review tickets in_review."

            ticket_file = tdir / f"{t['id']}.md"
            submit_file = tdir / "review" / f"{t['id']}-submit.md"

            if params.verdict == "approve":
                t["status"] = TicketStatus.CLOSED
                t["reviewed_by"] = params.agent_name
                t["reviewed_at"] = _now()
                t["updated_at"] = _now()
                _save_ticket_index(idx)

                # Move files to closed/
                dest = tdir / "closed"
                if ticket_file.exists():
                    shutil.move(str(ticket_file), str(dest / ticket_file.name))
                if submit_file.exists():
                    shutil.move(str(submit_file), str(dest / submit_file.name))

                # Write review result
                review_path = dest / f"{t['id']}-review.md"
                review_path.write_text("\n".join([
                    f"# ✅ Review: APPROVED",
                    f"**Ticket**: `{t['id']}` — {t['title']}",
                    f"**Reviewed by**: `{params.agent_name}`",
                    f"**Reviewed at**: {_now()}",
                    f"\n## Review Notes",
                    params.review_notes,
                ]), encoding="utf-8")

                # Log as memory
                mem = _load_mem()
                mem.append({
                    "id": _id(), "agent_name": params.agent_name,
                    "memory_type": MemoryType.PROGRESS,
                    "title": f"✅ Approved {t['id']}: {t['title']}",
                    "content": f"Ticket by `{t['created_by']}`, done by `{t.get('claimed_by','?')}`. {params.review_notes[:300]}",
                    "tags": ["ticket","approved"], "related_files": [],
                    "priority": 1, "pinned": False, "created_at": _now(), "timestamp": time.time()
                })
                _save_mem(mem)

                # Auto-handoff reviewer
                agents = _load_agt()
                for a in agents.values():
                    if a.get("agent_name") == params.agent_name and a.get("status") == AgentStatus.ACTIVE:
                        a["status"] = AgentStatus.HANDED_OFF
                        a["handed_off_at"] = _now()
                        break
                _save_agt(agents)

                return (
                    f"✅ Approved `{t['id']}`: **{t['title']}**\n"
                    f"Moved to `tickets/closed/`\n"
                    f"Reviewed by `{params.agent_name}`\n\n"
                    f"🤝 Auto-handoff: ticket closed, you're checked out."
                )

            else:  # reject
                t["status"] = TicketStatus.REJECTED
                t["reviewed_by"] = params.agent_name
                t["reviewed_at"] = _now()
                t["updated_at"] = _now()
                _save_ticket_index(idx)

                # Move files to rejected/
                dest = tdir / "rejected"
                if ticket_file.exists():
                    shutil.move(str(ticket_file), str(dest / ticket_file.name))
                if submit_file.exists():
                    shutil.move(str(submit_file), str(dest / submit_file.name))

                # Write rejection note with fix instructions
                reject_path = dest / f"{t['id']}-rejected.md"
                reject_lines = [
                    f"# ❌ Review: REJECTED",
                    f"**Ticket**: `{t['id']}` — {t['title']}",
                    f"**Rejected by**: `{params.agent_name}`",
                    f"**Rejected at**: {_now()}",
                    f"**Original assignee**: `{t.get('claimed_by', '?')}`",
                    f"\n## What went wrong",
                    params.review_notes,
                ]
                if params.fix_instructions:
                    reject_lines.extend([
                        f"\n## How to fix",
                        params.fix_instructions,
                    ])
                reject_lines.append(f"\n---\n*Re-claim this ticket with `memory_claim_ticket` and try again.*")
                reject_path.write_text("\n".join(reject_lines), encoding="utf-8")

                # Update ticket md in rejected/ for re-claiming
                t_copy = dict(t)
                t_copy["status"] = TicketStatus.OPEN  # reopen for next attempt
                _write_ticket_md(tdir / f"{t['id']}.md", t_copy)

                # Reopen in index so it shows up again
                t["status"] = TicketStatus.OPEN
                t["rejection_count"] = t.get("rejection_count", 0) + 1
                _save_ticket_index(idx)

                # Log as memory
                mem = _load_mem()
                mem.append({
                    "id": _id(), "agent_name": params.agent_name,
                    "memory_type": MemoryType.WARNING,
                    "title": f"❌ Rejected {t['id']}: {t['title']}",
                    "content": f"Rejected work by `{t.get('claimed_by','?')}`. {params.review_notes[:200]}\nFix: {(params.fix_instructions or 'See rejection note')[:200]}",
                    "tags": ["ticket","rejected"], "related_files": [],
                    "priority": 2, "pinned": True, "created_at": _now(), "timestamp": time.time()
                })
                _save_mem(mem)

                # Auto-handoff reviewer — ticket reopened, next agent will see it
                agents = _load_agt()
                for a in agents.values():
                    if a.get("agent_name") == params.agent_name and a.get("status") == AgentStatus.ACTIVE:
                        a["status"] = AgentStatus.HANDED_OFF
                        a["handed_off_at"] = _now()
                        break
                _save_agt(agents)

                return (
                    f"❌ Rejected `{t['id']}`: **{t['title']}**\n"
                    f"Rejection note: `tickets/rejected/{t['id']}-rejected.md`\n"
                    f"Ticket reopened for next agent to fix.\n"
                    f"Reviewed by `{params.agent_name}`\n\n"
                    f"🤝 Auto-handoff: you're checked out. Next agent will see this ticket."
                )
    return f"Ticket `{params.ticket_id}` not found."


@mcp.tool(name="memory_list_tickets", annotations={"title":"List Tickets","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def memory_list_tickets(params: ListTicketsInput) -> str:
    """List tickets. Shows open/in_progress/in_review by default. Use include_closed for history."""
    idx = _load_ticket_index()
    if not idx:
        return "No tickets yet. Use `memory_create_ticket` to create one."

    filtered = idx
    if params.status:
        filtered = [t for t in filtered if t["status"] == params.status]
    elif not params.include_closed:
        filtered = [t for t in filtered if t["status"] not in (TicketStatus.CLOSED,)]
    if params.assigned_to:
        filtered = [t for t in filtered if
                    params.assigned_to.lower() in (t.get("assigned_to") or "").lower() or
                    params.assigned_to.lower() in (t.get("claimed_by") or "").lower()]

    if not filtered:
        return "No tickets matching filters."

    pe = {"low":"🟢","medium":"🟡","high":"🟠","critical":"🔴"}
    se = {"open":"📭","in_progress":"🔧","in_review":"📤","closed":"✅","rejected":"❌"}

    lines = [f"# 🎫 Tickets ({len(filtered)})\n"]
    for t in sorted(filtered, key=lambda x: ({"critical":0,"high":1,"medium":2,"low":3}.get(x["priority"],9), -x.get("timestamp",0))):
        s = se.get(t["status"],"❓"); p = pe.get(t["priority"],"⚪")
        assign = f"→ `{t['assigned_to']}`" if t.get("assigned_to") else "→ any"
        claimed = f" ⚡ `{t['claimed_by']}`" if t.get("claimed_by") else ""
        rej = f" (rejected {t['rejection_count']}x)" if t.get("rejection_count") else ""
        lines.append(f"### {s} {p} `{t['id']}` — {t['title']}{rej}")
        lines.append(f"*By `{t['created_by']}` {assign}{claimed} | {t['status']}*")
        lines.append(f"{t['description'][:200]}{'...' if len(t['description'])>200 else ''}\n---\n")

    # Show folder structure hint
    lines.append("📁 `tickets/` = open queue | `tickets/review/` = submitted | `tickets/closed/` = done | `tickets/rejected/` = failed")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
