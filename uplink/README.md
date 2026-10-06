# DedSec Uplink

A Flipper Zero app plus a Windows tray companion that talk over Bluetooth LE (no pairing):
a live PC monitor, a status board for **Codex** and **Claude Code** sessions, and a pocket **cmd.exe**.

![Screens](../docs/uplink_screens.png)

| Part | Where | Does |
|---|---|---|
| Flipper app | [`apps/dedsec_uplink`](../apps/dedsec_uplink) → `SD/apps/Bluetooth/dedsec_uplink.fap` | Own BLE service, the screens, vibration, keyboard for commands, self-update |
| Companion | `uplink/` (Python) or `DedSecUplink.exe` | Tray icon; collects load and agent state, runs commands, serves updates |

Target: Flipper Zero on Unleashed `unlshd-093c` (API 88.9), Windows 10/11 with Bluetooth LE.

## Tabs

- **SYS** — CPU, RAM, network, disk. *Bars* mode autoscales network and disk to a rolling maximum
  (there is no fixed ceiling); *Text* mode shows exact up/down and read/write rates. CPU history graph.
- **CDX** — Codex sessions (`codex --profile router` in a terminal and the Codex desktop app). Sub-agents
  appear as `- name nickname` rows under their root; the root shows `done/total` sub-agents.
- **CLD** — Claude Code sessions (CLI and the Code tab of Claude Desktop); progress from the todo list.
- **CMD** — remote shell. Type a command on the Flipper, it runs on the PC, the output and `[exit N]` come back.

Status icons: spinner = working · blinking `!` = needs your answer or approval · `>_` = your turn · square = idle.
Session names, details and console output support **Cyrillic**.

## Controls

| Key | Action |
|---|---|
| ◀ ▶ | switch tabs |
| ▲ ▼ | select a session / scroll the console |
| OK | session details; on CMD: type a command |
| OK (hold) | settings |
| Back | back / exit; on CMD while a command runs: cancel it |

When a session starts waiting for you the Flipper vibrates, blinks the LED and shows
`!! APPROVAL NEEDED !!` (a question or approval) or `>> YOUR TURN <<` (the agent finished its turn).
OK on the banner opens that session.

## Settings (hold OK)

Saved to `SD/apps_data/dedsec_uplink/.uplink.settings`.

| Setting | Values |
|---|---|
| Vibration | on / off (works even in Unleashed stealth mode) |
| Vibrate on cmd reply | on / off |
| LED alerts | on / off |
| Wake screen on alert | on / off |
| Indicators | Bars / Text |
| Theme | Normal / Inverted |
| Font size | Normal / Large |
| Tab 1…4 | SYS / CDX / CLD / CMD / Off — order and visibility of the tabs |
| Updates | Notify / Auto |
| Version | shows the installed version; OK installs a pending update |

## Pocket cmd.exe

On the CMD tab press OK, type a command on the on-screen keyboard and choose **save**. It runs in one
persistent `cmd.exe` on the PC, so `cd` and environment changes stick between commands. Output streams back
line by line; at the end you get `[exit N]` and a short vibration. Back cancels a running command.

- The Flipper keyboard capitalizes the first letter (`Ver`, `Dir`). Windows commands are case-insensitive.
- Up to 400 output lines per command; a command running over 2 minutes is stopped.
- The shell switches to UTF-8 (`chcp 65001`), so Cyrillic file names come through.
- `"shell": "powershell"` in `%LOCALAPPDATA%\DedSecUplink\config.json` switches to PowerShell.

**Security.** The BLE link has no pairing, so anyone in Bluetooth range who knows the protocol could send
commands. The remote shell can be turned off in the tray menu (*Allow remote shell*), every command is logged,
output is capped. Turn it off if you don't use CMD.

## Over-the-air updates

The companion checks the latest [GitHub release](https://github.com/liullinil/flipper-dedsec/releases)
every 30 minutes. If the Flipper runs an older version, the app shows `>> UPDATE AVAILABLE <<`
(or installs it right away with *Updates: Auto*). On OK:

1. the companion downloads `dedsec_uplink.fap` from the release;
2. it streams the file over BLE in base64 chunks with an acknowledgement window (lost chunks are re-sent);
3. the app writes it next to itself, checks size and CRC-32, replaces its own `.fap` and restarts.

The first version with OTA support (v1.1.0) has to be installed once by hand; later versions arrive over the air.

## Companion

### Run

- **No Python:** download `DedSecUplink.exe` from the release and run it.
- **From source:** `python -m pip install -r requirements.txt`, then `pythonw dedsec_uplink.pyw`.
  `bleak` is pinned to 0.22.3 because 1.x does not import on Python 3.9.0.

A hooded-skull icon appears in the tray (Windows may hide it under the `^` arrow next to the clock).
The ring shows the link: green connected, yellow searching, grey paused, red error.

Tray menu: *Pause uplink*, *Allow remote shell (cmd)*, *Start with Windows*, *Claude Code hooks*, *Open log*, *Quit*.

Command line (`.exe` or `.pyw`): `--console` (log to the console), `--dump` (print one data frame, no BLE),
`--install-autostart` / `--uninstall-autostart`, `--install-claude-hooks` / `--remove-claude-hooks`.
Log: `%LOCALAPPDATA%\DedSecUplink\uplink.log`. Only one copy runs at a time.

### Build the .exe

```bash
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name DedSecUplink --collect-submodules bleak \
    --collect-submodules winrt --hidden-import pystray._win32 dedsec_uplink.pyw
```

### Where session state comes from

Nothing in Codex or Claude is changed; the companion only reads their logs.

**Codex** — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and names from `~/.codex/session_index.jsonl`:
`task_started` → working; `task_complete` → your turn (for 20 min), then idle; `request_user_input_async` →
a question waits for you; `*approval_request*` events → approval needed; reasoning summaries, commands and file
edits → "what it is doing"; sub-agents from `session_meta.source.subagent`.

**Claude Code** — `~/.claude/projects/*/*.jsonl`: a user prompt → working; `stop_hook_summary` / `end_turn` →
your turn; a pending `AskUserQuestion` / `ExitPlanMode` → waits for you; the session title, todo progress and
current step come from the transcript.

### Claude Code hooks (precise state)

Without hooks, permission prompts are guessed from the transcript. With hooks, Claude Code itself reports the
transitions. Enable them from the tray menu, with `--install-claude-hooks`, or:

```bash
python install_claude_hooks.py            # add
python install_claude_hooks.py --remove   # remove
```

This adds `Notification`, `Stop`, `SubagentStop` and `UserPromptSubmit` hooks to `~/.claude/settings.json`
(merged with your own hooks, a `settings.json.bak-*` backup is kept). Each hook appends one line to
`%LOCALAPPDATA%\DedSecUplink\claude_events.jsonl`. Restart Claude Code sessions (or run `/hooks`) afterwards.

## Protocol

Service `de5ec000-1d00-4a1e-8b5e-0f11e7ca1000`, advertised as 16-bit UUID `0xDED5`, name `DedSec <flipper name>`.
Text lines, `|`-separated, UTF-8.

PC → Flipper (write, `de5ec001-…`):

```
H|host                                   hello
S|cpu|ram|disk|rd|wr|up|dn|used|total    load, rates in KB/s
L|X|count                                list size (X = Codex, C = Claude)
I|X|idx|key|state|done|total|age|attn|name|detail
O|seq|text   X|seq|code   W|cwd           command output / exit code / shell directory
N|tag|size                               a newer release exists
UB|tag|size|crc32   UD|offset|base64   UE|tag     update transfer
B                                        the PC is going away
```

Flipper → PC (notify, `de5ec002-…`):

```
C|seq|command   K|seq                    run / cancel a command
V|version                                app version (on connect and every 30 s)
U|tag   UA|written                       request an update / acknowledge bytes written
```

`state`: `W` working, `A` needs an answer or approval, `I` your turn, `S` idle.
`attn` grows every time a session starts waiting for you; the Flipper vibrates when it grows.

## Build the Flipper app

```bash
cd apps/dedsec_uplink
ufbt            # -> dist/dedsec_uplink.fap
ufbt launch     # install over USB and start
```

Notes for Unleashed 093c: the firmware only reports "connected" after pairing, so the link counts as alive
while data flows (8 s timeout). The default Bluetooth profile (for the phone) is restored when the app exits.
