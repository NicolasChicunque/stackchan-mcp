---
name: stackchanchat
description: >-
  Token-efficient voice conversation loop with StackChan. Supports the unified
  ESP32-native AppGemini + Auto-Attach MCP bridge (zero Whisper, zero PC API
  key via stackchan_bridge.py) as well as legacy AppMcp (stackchan_live.py with
  auto-fallback to stackchan_chat.py). Activate when the user invokes
  /stackchanchat, or asks to start voice chat with StackChan.
---

# StackChan Voice Chat (`/stackchanchat`)

Run voice conversations with StackChan outside the Antigravity context window
(0 IDE quota for casual turns), escalating back to Antigravity only when coding,
repo edits, builds, flashes, or technical tasks are requested.

## Procedure

1. **Preflight**: Check `stackchan_status` (or `GET http://<STACKCHAN_IP>/audio/status`):
   - If `mode == "gemini_mcp"` (**Unified `GEMINI` — Recommended**): use
     `scripts/stackchan_bridge.py` (ESP32 runs Gemini Live natively; PC only
     maintains heartbeat lease, polls `/mcp/escalation`, and speaks engineer
     replies with Edge TTS).
   - If `mode == "mcp"` (**Legacy `MCP`**): use `scripts/stackchan_live.py --lang auto`
     (with automatic fallback to `scripts/stackchan_chat.py`).
   - If unreachable, ask the user to open **`GEMINI`** on StackChan and verify
     `STACKCHAN_IP` in `.env`.

2. **Launch the background loop** with `run_command` (`IsDaemon=true`) inside the
   `stackchan-mcp` directory:
   - **Unified `GEMINI` (`mode == "gemini_mcp"`):**
     ```bash
     uv run python -u scripts/stackchan_bridge.py 2>&1
     ```
   - **Legacy `MCP` (`mode == "mcp"`):**
     ```bash
     uv run python -u scripts/stackchan_live.py --lang auto 2>&1
     ```
   - Tell the user the voice bridge is active and stop calling tools while it runs.

3. **Handle escalation**: When the user asks for technical work, the script
   prints `[ESCALATED] Task: <summary>` and exits (keeping the MCP lease alive).
   Execute the requested work with the main agent's tools:
   - **If you need user confirmation or a follow-up answer** (`IsDaemon=true`,
     `WaitMsBeforeAsync=500`):
     - Unified `GEMINI`:
       ```bash
       uv run python -u scripts/stackchan_bridge.py --wait-reply --say "<concise spoken question>" 2>&1
       ```
     - Legacy `MCP`:
       ```bash
       uv run python -u scripts/stackchan_chat.py --lang auto --wait-reply --say "<concise spoken question>" 2>&1
       ```
   - **Once the technical work is complete**:
     - Unified `GEMINI`: speak summary & resume LIVE in one command:
       ```bash
       uv run python -u scripts/stackchan_bridge.py --say "<concise spoken summary>" --context "<1-line summary>" 2>&1
       ```
     - Legacy `MCP`: speak via `stackchan_say`, then relaunch
       `scripts/stackchan_live.py --lang auto --no-greeting --context "<1-line summary>"`.

4. **Handle end**: When the user ends the session or the script exits cleanly,
   `stackchan_bridge.py` calls `/mcp/detach` and StackChan returns to 1-avatar
   standalone `GEMINI` mode automatically.
