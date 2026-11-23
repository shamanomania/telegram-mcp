# Telegram MCP

Read and send Telegram messages from a local MCP client using your own Telegram account. You decide which chats the client can access and whether each chat is read-only, send-only or both.

**Requirements** · macOS or Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/) (or Python 3.11+), TDLib and a local MCP client with stdio support.

## 1. Install

On macOS, install TDLib and the optional terminal QR encoder.

```sh
brew install tdlib qrencode
```

On Linux, install a build of the TDLib JSON library (`libtdjson.so`) for your OS and architecture. Follow the [official TDLib build instructions](https://tdlib.github.io/td/build.html) if your distribution doesn't package it. For a library outside the standard loader paths, set `TDLIB_LIBRARY` to its absolute path before running setup or generating client configuration. Use an unlocked desktop Secret Service or KWallet keyring. Install your distribution's `qrencode` package for QR sign-in.

```sh
uv tool install --from git+https://github.com/shamanomania/telegram-mcp.git telegram-mcp
```

If your shell can't find the command after installation, run `uv tool update-shell` and open a new terminal.

<details>
<summary>Install from a checkout with Python</summary>

```sh
git clone https://github.com/shamanomania/telegram-mcp.git
cd telegram-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/telegram-mcp --help
```

Use `.venv/bin/telegram-mcp` in place of `telegram-mcp` in the following commands.

</details>

## 2. Sign in and choose a chat

```sh
telegram-mcp setup
telegram-mcp doctor
telegram-mcp login
telegram-mcp chats
```

Get your application's `api_id` and `api_hash` from [Telegram API development tools](https://my.telegram.org/apps). The terminal prompts you for them during the first login. Open **Telegram → Settings → Devices → Link Desktop Device** on your phone and scan the QR code. The terminal prompts for your two-step verification password if Telegram requires it. Run `telegram-mcp login --phone` to enter a number and the login codes instead.

Copy a chat ID from the terminal output. Replace the example ID below with that ID.

```sh
telegram-mcp allow-chat my-chat -1001234567890
telegram-mcp doctor --account primary
```

- `chats "Project name"` searches by title. `chats @publicname` looks up a public chat.
- Use `allow-chat my-chat ID --title "Project name"` for an older chat. Use `--username @publicname` for a public chat and to enable reads through its public message links.
- Access defaults to `read`. Repeat `allow-chat` for the same alias and ID with `--access read_write` or `--access send` when you want to enable sending. Use `remove-chat my-chat` to revoke access.
- To add another Telegram account, pass `--account work` to `setup`, `login` and subsequent commands. Each profile is bound to the Telegram user verified at login.

## 3. Connect your MCP client

Run the command for your client and add the generated entry to its existing config. Use your client's local or user configuration for personal chats.

| Client | Command | Where to add it |
| --- | --- | --- |
| Codex | `telegram-mcp client-config codex` | `~/.codex/config.toml` |
| Claude Desktop | `telegram-mcp client-config claude` | **Settings → Developer → Edit Config** |
| Claude Code | `telegram-mcp client-config claude` | `mcpServers` in `.mcp.json`, or the entry as input to `claude mcp add-json` for user scope |
| Cursor | `telegram-mcp client-config cursor` | `~/.cursor/mcp.json` |
| VS Code | `telegram-mcp client-config vscode` | Open **MCP: Open User Configuration** from the Command Palette |
| Other local stdio clients | `telegram-mcp client-config json` | Use the generated `command`, `args` and optional `env` |

Restart the client or reload its MCP servers. To try it, ask **“Read the last five messages from `my-chat` in Telegram.”** The account and chat aliases are discoverable through `list_telegram_chats`.

The server runs as a local process with its user's OS keyring. A client that only accepts a remote MCP URL needs a separate integration.

## Tools

| Tool | Behavior |
| --- | --- |
| `list_telegram_chats` | Lists only configured aliases, chat IDs and access levels. |
| `read_telegram_message` | Reads one message by a `message_id` returned by MCP or a matching `https://t.me/` link. |
| `read_telegram_history` | Returns messages from newest to oldest. Pass `next_before_message_id` as `before_message_id` for the next page. TDLib can return fewer messages than requested. |
| `send_telegram_message` | Sends text as your personal Telegram user. Set `parse_mode: "html"` for Telegram HTML. |

Use a unique `request_id` for each intended send. Repeating the same ID with the same payload returns the recorded receipt for a confirmed send. When Telegram hasn't confirmed the outcome, check the destination before trying a new request. Without an ID, matching payloads to the same account and chat share a receipt, so supply a new ID when you intentionally want to send the same text again.

## Local data and troubleshooting

- Sessions, the chat allowlist and send receipts stay in the per-user data directory. `telegram-mcp doctor` prints its location. The API credentials and database encryption key stay in your OS keyring.
- Use `--data-dir "/absolute/path"` before any subcommand, or set `TELEGRAM_MCP_DATA_DIR`, to use a separate local installation. The generated client entry carries that directory.
- `telegram-mcp doctor --account primary` confirms the session with Telegram. If a session is busy, let the other operation finish and retry. Each account database has a single writer.
- The tools return allowed Telegram content to your MCP client. Review that client's data and tool approval settings before enabling private chats or granting send access.

<details>
<summary>Upgrade an existing local installation</summary>

Existing commands that launch `server.py` from a checkout with a local `telegram-profiles.json` keep using the existing database layout. To switch that installation to the packaged command, use the original data directory and import its original Telegram MCP keyring entry after the session is verified.

```sh
export TELEGRAM_MCP_DATA_DIR="/absolute/path/to/existing/telegram-mcp"
telegram-mcp login --import-legacy-keyring
telegram-mcp doctor --account primary
telegram-mcp client-config codex
```

For additional accounts, repeat login with `--account ALIAS`. Generate the format for your own MCP client.

</details>

## Development

```sh
uv sync --locked
uv run --locked python -m unittest -q
```
