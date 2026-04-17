# 🧠 Agent Shared Memory MCP

**Project-local** shared memory for multi-agent sequential workflows. When agents die, context survives.

## The Problem

Multiple AI agents (Claude, Cursor, Codex, Claude Code, AntiGravity) work on the same project one-at-a-time. When one dies (quota, crash), the next starts from zero. Decisions, discoveries, and context are lost.

## The Solution

A shared `.agent-mem/` directory inside your project that ALL agents read/write via MCP.

```
your-project/
├── .agent-mem/              ← Runtime memory (gitignored)
│   ├── memories.json        ← Entries stamped with agent_name
│   ├── agents.json          ← Agent history (who, when, KIA?)
│   ├── state.json           ← Shared key-value store
│   ├── project.json         ← Project metadata
│   ├── archive.json         ← Compacted old entries
│   ├── digests.json         ← Compressed long-term memory
│   └── checkpoints/         ← Periodic snapshots
├── .agent-mem-hooks/        ← Hook scripts (commit to git)
├── .cursor/hooks.json       ← Cursor hook config
├── .claude/settings.json    ← Claude Code hook config
└── CLAUDE.md / .cursorrules ← Agent rules
```

---

## Installation

### Step 0 — Clone

```bash
git clone https://github.com/swisspra/agent_mem_MCP.git
cd agent_mem_MCP
```

### Step 1 — Create a virtual environment

```bash
cd /path/to/this/repo          # wherever you put this MCP server
python3 -m venv venv
source venv/bin/activate       # macOS/Linux
# venv\Scripts\activate        # Windows
```

### Step 2 — Install dependencies

```bash
./venv/bin/pip install httpx "mcp[cli]" pydantic
```

Verify:

```bash
./venv/bin/python -c "import httpx, mcp, pydantic; print('✅ All OK')"
```

### Step 3 — Note the full Python path

You'll need the **full venv Python path** for all platform configs:

```bash
# Example (yours will differ):
# /Users/yourname/tools/agent-memory-mcp/venv/bin/python
```

---

## Platform Setup

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "/full/path/to/venv/bin/python",
      "args": ["/full/path/to/server.py"],
      "env": {
        "AGENT_PROJECT_DIR": "/full/path/to/your/project"
      }
    }
  }
}
```

### Cursor

Add to your project's `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "/full/path/to/venv/bin/python",
      "args": ["/full/path/to/server.py"],
      "env": {
        "AGENT_PROJECT_DIR": "${workspaceFolder}"
      }
    }
  }
}
```

### Claude Code

```bash
cd /path/to/your/project
claude mcp add agent-memory -- /full/path/to/venv/bin/python /full/path/to/server.py
```

### Codex (OpenAI)

Edit `~/.codex/config.toml`:

```toml
[mcp_servers.agent-memory]
command = "/full/path/to/venv/bin/python"
args = ["/full/path/to/server.py"]

[mcp_servers.agent-memory.env]
AGENT_PROJECT_DIR = "/full/path/to/your/project"
```

Or via CLI:

```bash
codex mcp add agent-memory \
  --env AGENT_PROJECT_DIR="/full/path/to/your/project" \
  -- /full/path/to/venv/bin/python /full/path/to/server.py
```

### AntiGravity (Gemini)

Use the MCP settings UI:
- **Command**: `/full/path/to/venv/bin/python`
- **Arguments**: `/full/path/to/server.py`
- **Environment variables**:
  - `AGENT_PROJECT_DIR` = `/full/path/to/your/project`
- **Working directory**: `/full/path/to/your/project`

---

## Optional: External Context Directories

If you have reference docs, specs, or shared info outside the project:

```json
"env": {
  "AGENT_PROJECT_DIR": "/path/to/project",
  "AGENT_MEM_CONTEXT_DIRS": "/path/to/docs:/path/to/specs:/path/to/shared"
}
```

Agents can browse with `memory_context_dirs` and read with `memory_context_read`.

---

## Optional: Setup Hooks (Cursor + Claude Code)

Run from your project root to auto-configure hooks:

```bash
bash /path/to/this/repo/setup-project.sh
```

> ⚠️ If your project already has `.cursor/hooks.json` or `.claude/settings.json`, the script will skip them and ask you to merge manually. See `configs/` for reference.

Hooks auto-inject memory context on session start and save emergency checkpoints on agent death.

---

## For Agents Without Hooks (Codex, AntiGravity, etc.)

Agents that don't support lifecycle hooks won't auto-read memory on startup. You need to tell them via **system prompt / rules file / AGENTS.md**.

### Option 1 — System Prompt (recommended for Codex/AntiGravity)

Add this to the agent's system prompt or custom instructions:

```
You have access to an Agent Shared Memory MCP server for this project.

MANDATORY PROTOCOL — follow these steps in order:
1. Call memory_get_briefing() FIRST — read the full project context before doing anything
2. Call memory_agent_join(agent_name="<your-unique-name>", agent_platform="<platform>")
3. Call memory_list_tickets() — check if any tickets are assigned to you
4. While working: call memory_write() after EVERY significant action
5. Every 10-15 minutes: call memory_checkpoint()
6. Before finishing: call memory_handoff()

TICKETING — when you need help from another agent:
- memory_create_ticket() — request help, assign to a specific agent/platform or leave open
- memory_claim_ticket() — pick up a ticket assigned to you
- memory_submit_ticket() — submit your work for review when done
- memory_review_ticket() — approve or reject submitted work (with fix instructions if rejected)

If you skip these steps, the next agent will have no context and will redo your work.
Your agent_name is stamped on every entry — you are accountable for what you write.
```

### Option 2 — AGENTS.md (for Codex)

Add to `AGENTS.md` in the project root:

```markdown
## Agent Shared Memory

This project uses .agent-mem/ for multi-agent coordination.
Before starting ANY work:
1. Call memory_get_briefing to read full context
2. Call memory_agent_join with your unique agent_name
3. Call memory_write after EVERY significant action
4. Call memory_checkpoint every 10-15 minutes
5. Call memory_handoff before you finish
```

### Option 3 — .cursorrules (for Cursor without hooks)

Already created by `setup-project.sh`, or create manually in project root.

> **Note**: Hooks (Cursor, Claude Code) are more reliable because they run automatically. System prompts depend on the agent following instructions, which isn't guaranteed — but it's the best available option for platforms without hooks.

---

## Usage

### New project

```
memory_init(description="My project", tech_stack="React/Node")
memory_agent_join(agent_name="claude-v1", agent_platform="claude")
```

### Existing project (first time)

```
memory_bootstrap(
  agent_name="claude-onboard",
  description="My existing project",
  tech_stack="React/Node",
  current_task="Implement feature X",
  known_warnings=["Don't touch legacy auth module"]
)
```

Bootstrap auto-scans: README, git log, directory structure, package.json/pyproject.toml.

### Every subsequent agent

```
memory_get_briefing()                    ← read full context
memory_agent_join(agent_name="...", agent_platform="...")
... work ...
memory_write(agent_name="...", memory_type="progress", title="...", content="...")
memory_checkpoint(agent_name="...", summary="...")
memory_handoff(agent_name="...", summary="...", next_steps=["..."])
```

---

## Tools (22 total)

| Category | Tool | Purpose |
|----------|------|---------|
| Setup | `memory_init` | Initialize `.agent-mem/` |
| Setup | `memory_bootstrap` | Auto-scan existing project |
| Agent | `memory_agent_join` | Register (auto-KIA previous) |
| Agent | `memory_handoff` | Formal handoff to next agent |
| Write | `memory_write` | Write memory (stamped with name) |
| Read | `memory_read` | Read with filters |
| Read | `memory_search` | Full-text search |
| State | `memory_checkpoint` | Full state snapshot |
| State | `memory_pin` | Pin/unpin critical entries |
| State | `memory_update_state` | Shared key-value store |
| Context | `memory_get_briefing` | Full project briefing |
| Context | `memory_status` | Quick dashboard |
| Context | `memory_context_dirs` | List external ref dirs |
| Context | `memory_context_read` | Read from external dirs |
| Tokens | `memory_compact` | Compress old → save 70%+ |
| Tokens | `memory_token_usage` | Token breakdown report |
| Tokens | `memory_search_archive` | Search compacted entries |
| Tickets | `memory_create_ticket` | Request help from another agent |
| Tickets | `memory_claim_ticket` | Pick up a ticket to work on |
| Tickets | `memory_submit_ticket` | Submit work for review |
| Tickets | `memory_review_ticket` | Approve or reject submitted work |
| Tickets | `memory_list_tickets` | List all tickets with filters |

## Memory Types

| Type | Use For |
|------|---------|
| `decision` | Architectural choices |
| `progress` | What was accomplished |
| `blocker` | What's stuck |
| `context` | Background info |
| `handoff` | Structured handoff notes |
| `todo` | Remaining tasks |
| `file_change` | Files created/modified |
| `discovery` | Something learned |
| `warning` | Gotchas & pitfalls |
| `checkpoint` | Full state snapshot |

## Agent Name Tracing

Every entry is permanently stamped with `agent_name` + `agent_platform`:

```json
{
  "id": "a1b2c3d4e5f6",
  "agent_name": "cursor-feat-auth",
  "memory_type": "decision",
  "title": "Use JWT instead of sessions",
  "content": "Decided to use JWT because...",
  "created_at": "2025-04-15T18:30:00+07:00"
}
```

Filter by agent: `memory_read(agent_name="cursor-feat-auth")` — trace who hallucinated.

## Token Management

Memories grow over time. Use the tiered system:

- **Hot**: Recent entries, loaded in briefings (configurable: `AGENT_MEM_HOT_HOURS`, `AGENT_MEM_MAX_HOT`)
- **Warm**: Compressed digests from old sessions
- **Cold**: Raw archive on disk, only loaded on search

Run `memory_compact` when `memory_token_usage` recommends it. Typical savings: 70%+.

Set `ANTHROPIC_API_KEY` for LLM-powered compression, or use rule-based (default).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_PROJECT_DIR` | cwd | Project root path |
| `AGENT_MEM_CONTEXT_DIRS` | (none) | Colon-separated external info dirs |
| `AGENT_MEM_HOT_HOURS` | 24 | Hours to keep full detail |
| `AGENT_MEM_MAX_HOT` | 50 | Max hot entries |
| `ANTHROPIC_API_KEY` | (none) | For LLM-powered compaction |
| `AGENT_MEM_MODEL` | claude-sonnet-4-20250514 | Model for compaction |

## Folder Structure

```
agent-memory-mcp/
├── server.py              ← MCP server (17 tools)
├── pyproject.toml         ← Package metadata
├── README.md              ← This file
├── setup-project.sh       ← Project setup script
├── hooks/                 ← Platform hook scripts
│   ├── cursor-session-start.sh
│   ├── cursor-session-end.sh
│   ├── claude-code-session-start.sh
│   └── claude-code-stop.sh
└── configs/               ← Example platform configs
    ├── cursor-hooks.json
    ├── cursor-mcp.json
    └── claude-code-settings.json
```

***Suggestion***
Few prompt you should tell your agent right after setup or add these to bootstrap

-MANDATORY RULE: Do not edit the code until agent got confirmation from user
-MANDATORY RULE: Auto-save memory after EVERY code change. All agents must do memory_write immediately right after edit/create/delete *always* do not wait for user to tell you to do it.

**Bonus**
-If apply this MCP to existing project, You should tell your first agent, Opus is recommended, to read and understand architecture and codebase. Then tell it to compact all information to be ready for other agent with minimum token usage.


***********

## License

NOT MIT
