#!/bin/bash
# ─────────────────────────────────────────────────────────
# Agent Memory MCP — Project Setup Script
# Run this in your project root to set up hooks for all platforms
# Usage: bash /path/to/agent-memory-mcp/setup-project.sh
# ─────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-$(pwd)}"

echo "🧠 Agent Shared Memory — Project Setup"
echo "Project: $PROJECT_DIR"
echo ""

# ── 1. Create .agent-mem-hooks/ in project ──
HOOKS_DIR="$PROJECT_DIR/.agent-mem-hooks"
mkdir -p "$HOOKS_DIR"

cp "$SCRIPT_DIR/hooks/cursor-session-start.sh" "$HOOKS_DIR/"
cp "$SCRIPT_DIR/hooks/cursor-session-end.sh" "$HOOKS_DIR/"
cp "$SCRIPT_DIR/hooks/claude-code-session-start.sh" "$HOOKS_DIR/"
cp "$SCRIPT_DIR/hooks/claude-code-stop.sh" "$HOOKS_DIR/"

chmod +x "$HOOKS_DIR"/*.sh

echo "✅ Hooks copied to .agent-mem-hooks/"

# ── 1b. Copy SKILL.md to Claude Code skills ──
SKILL_DIR="$PROJECT_DIR/.claude/skills/agent-memory"
mkdir -p "$SKILL_DIR"
if [ -f "$SCRIPT_DIR/SKILL.md" ]; then
    cp "$SCRIPT_DIR/SKILL.md" "$SKILL_DIR/SKILL.md"
    echo "✅ SKILL.md copied to .claude/skills/agent-memory/"
fi

# ── 2. Set up Cursor hooks ──
CURSOR_DIR="$PROJECT_DIR/.cursor"
mkdir -p "$CURSOR_DIR"

if [ -f "$CURSOR_DIR/hooks.json" ]; then
    echo "⚠️  .cursor/hooks.json exists — please merge manually:"
    echo "    See: $SCRIPT_DIR/configs/cursor-hooks.json"
else
    cp "$SCRIPT_DIR/configs/cursor-hooks.json" "$CURSOR_DIR/hooks.json"
    echo "✅ Cursor hooks configured at .cursor/hooks.json"
fi

# ── 3. Set up Claude Code hooks ──
CLAUDE_DIR="$PROJECT_DIR/.claude"
mkdir -p "$CLAUDE_DIR"

if [ -f "$CLAUDE_DIR/settings.json" ]; then
    echo "⚠️  .claude/settings.json exists — please merge hooks manually:"
    echo "    See: $SCRIPT_DIR/configs/claude-code-settings.json"
else
    cp "$SCRIPT_DIR/configs/claude-code-settings.json" "$CLAUDE_DIR/settings.json"
    echo "✅ Claude Code hooks configured at .claude/settings.json"
fi

# ── 4. Update .gitignore ──
GITIGNORE="$PROJECT_DIR/.gitignore"
if [ -f "$GITIGNORE" ]; then
    if ! grep -q ".agent-mem/" "$GITIGNORE"; then
        echo "" >> "$GITIGNORE"
        echo "# Agent shared memory (runtime data)" >> "$GITIGNORE"
        echo ".agent-mem/" >> "$GITIGNORE"
        echo "✅ Added .agent-mem/ to .gitignore"
    fi
else
    echo ".agent-mem/" > "$GITIGNORE"
    echo "✅ Created .gitignore with .agent-mem/"
fi

# ── 5. Create CLAUDE.md / .cursorrules reminder ──
RULES_NOTE="
# Agent Shared Memory Protocol

This project uses .agent-mem/ for multi-agent coordination.
When you start working:
1. Call memory_get_briefing to read full context
2. Call memory_agent_join with your unique agent_name
3. Call memory_write after EVERY significant action
4. Call memory_checkpoint every 10-15 minutes
5. Call memory_handoff before you finish
"

if [ ! -f "$PROJECT_DIR/CLAUDE.md" ]; then
    echo "$RULES_NOTE" > "$PROJECT_DIR/CLAUDE.md"
    echo "✅ Created CLAUDE.md with agent memory protocol"
fi

if [ ! -f "$PROJECT_DIR/.cursorrules" ]; then
    echo "$RULES_NOTE" > "$PROJECT_DIR/.cursorrules"
    echo "✅ Created .cursorrules with agent memory protocol"
fi

echo ""
echo "🎉 Setup complete!"
echo ""
echo "Structure created:"
echo "  $PROJECT_DIR/"
echo "  ├── .agent-mem-hooks/       ← Hook scripts (commit this)"
echo "  │   ├── cursor-session-start.sh"
echo "  │   ├── cursor-session-end.sh"
echo "  │   ├── claude-code-session-start.sh"
echo "  │   └── claude-code-stop.sh"
echo "  ├── .cursor/hooks.json      ← Cursor hook config"
echo "  ├── .claude/settings.json   ← Claude Code hook config"
echo "  ├── .cursorrules            ← Rules for Cursor agents"
echo "  ├── CLAUDE.md               ← Rules for Claude Code agents"
echo "  └── .agent-mem/             ← Runtime memory (gitignored)"
echo ""
echo "Next: Add the MCP server to each agent platform:"
echo "  Cursor:     .cursor/mcp.json"
echo "  Claude Code: claude mcp add agent-memory -- python /path/to/server.py"
echo ""
