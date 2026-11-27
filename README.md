<div align="center">

<img src="assets/logo.svg" width="84" alt="StackChan MCP Logo">

# StackChan MCP Voice Bridge & Antigravity Plugin

**PC companion & Antigravity IDE plugin for [StackChan Community Suite](https://github.com/NicolasChicunque/StackChan)**  
*Hands-free `/stackchanchat` voice loop · Auto-Attach MCP · Zero Whisper · Zero PC API Key*

[![Firmware](https://img.shields.io/badge/Firmware-StackChan%20Community%20Suite-0071C5)](https://github.com/NicolasChicunque/StackChan)
[![MCP](https://img.shields.io/badge/MCP-Server%20%2B%20Plugin-5d5bd6)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[How It Works](#how-it-works) · [Quick Start](#quick-start-antigravity-plugin) · [MCP Tools](#mcp-tools) · [Privacy](#privacy--security) · [Support](#about--support)

</div>

---

## Companion to StackChan Community Suite

> **Looking for the robot firmware?** Start here: **[NicolasChicunque/StackChan](https://github.com/NicolasChicunque/StackChan)** — modular ESP-IDF v6.1 firmware for M5Stack StackChan (CoreS3) with M5Burner 1-click install, `MUSIC`, `ALARM`, `GEMINI`, `APP.CENTER`, and the 12-app Community Labs Pack.

This repository is the **optional PC companion** (forked from [migratorywhale/stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp)) that adds:

1. **Antigravity Plugin (`plugin.json` + `/stackchanchat` + `mcp_config.json`)** — install once to enable both the MCP robot tools and the `/stackchanchat` voice loop.
2. **Unified `GEMINI` + Auto-Attach MCP (`scripts/stackchan_bridge.py`)** — StackChan runs **Gemini Live** natively on the ESP32-S3 for everyday chat (**0 IDE quota**, no local Whisper, no PC `GEMINI_API_KEY`) and escalates coding, Git, build, or camera requests directly to your active Antigravity workspace.
3. **Full MCP Tool Suite** — also works with Claude Desktop, Claude Code, Cursor, Windsurf, or ChatGPT (see [docs/mcp-client-setup.md](docs/mcp-client-setup.md)).

---

## How It Works

```text
You speak ──► StackChan (ESP32-S3 GEMINI app, Gemini Live WSS)
                ├── Casual chat ──► Replies directly on-device (0 IDE quota)
                └── Coding / repo / camera task
                      └──► stackchan_bridge.py ──► Antigravity IDE (executes & replies aloud)
```

---

## Quick Start (Antigravity Plugin)

### 1. Prerequisites

- StackChan flashed with **[StackChan Community Suite](https://github.com/NicolasChicunque/StackChan)** (with your free Google AI Studio key in `/sd/config/gemini.txt`)
- Python 3.11+, `uv` (`pip install uv`), and `ffmpeg`

### 2. Install as an Antigravity Plugin

Clone directly into Antigravity's plugins folder so the **`stackchan` MCP server** and **`/stackchanchat` skill** register automatically:

```bash
git clone https://github.com/NicolasChicunque/stackchan-mcp.git ~/.gemini/config/plugins/stackchan-mcp
cd ~/.gemini/config/plugins/stackchan-mcp
uv sync && uv pip install edge-tts
```

*(On Windows PowerShell, use `$env:USERPROFILE\.gemini\config\plugins\stackchan-mcp`.)*

### 3. Configure `.env` & Run

```bash
cp .env.example .env
```

Edit `.env` and set your robot's local IP (shown on StackChan's **`GEMINI`** screen):

```env
STACKCHAN_IP="192.168.1.xxx"
TTS_ENGINE="edge-tts"
```

Restart Antigravity, open **`GEMINI`** on StackChan, and type **`/stackchanchat`**.

> **Other MCP clients (Claude Desktop, Cursor, Windsurf):** Clone anywhere, run `uv sync`, and see [docs/mcp-client-setup.md](docs/mcp-client-setup.md) for copy-paste configs.

---

## MCP Tools

| Tool | Purpose |
| --- | --- |
| `stackchan_say` / `stackchan_listen` | Speak text through the speaker or record & transcribe from the mic |
| `stackchan_see` | Capture a 320×240 camera frame (shown on screen) and return it to the AI |
| `stackchan_face` | Set expression: `calm`, `thinking`, `happy`, `sleepy`, `shy`, `smug`, `pouty` |
| `stackchan_move` / `nod` / `shake` / `home` | Control head servos (pan/tilt gestures) |
| `stackchan_sense` | Read temperature, humidity, and pressure (optional M5Stack ENV III Unit) |
| `stackchan_status` / `health` / `playback_status` | Check connectivity, audio queues, mic state, and heap/PSRAM |

---

## Privacy & Security

- **Local LAN first:** StackChan and this bridge talk directly over your local Wi-Fi network.
- **Keep `.env` private:** `.env` is gitignored — never commit API keys, auth tokens, or personal IPs.
- **On-demand sensors:** Camera (`stackchan_see`) and microphone audio are only accessed when you explicitly run a tool or start `/stackchanchat`. See [SECURITY.md](SECURITY.md) for full details.

---

## About & Support

Built as a **personal hobby** using personal resources and **AI-assisted development (vibe coding)**, shared freely for the StackChan community. Issues and PRs are welcome!

<!-- DRAFT SUPPORT LINKS — Uncomment once accounts are created:
If you enjoy this project and want to support ongoing hobby development:

[![Ko-fi](https://img.shields.io/badge/Support%20on-Ko--fi-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/YOUR_USERNAME)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/YOUR_USERNAME)
-->

---

## Credits & Upstream

Forked from **[migratorywhale/stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp)** (`upstream` branch kept intact for reference).  
Firmware & M5Burner releases live in **[NicolasChicunque/StackChan](https://github.com/NicolasChicunque/StackChan)**.
