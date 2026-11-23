# Telegram MCP

Read and send Telegram messages from Codex, Claude, Cursor, or VS Code using your own Telegram account. Start by giving your client read access to one chat. You can grant sending access later.

## Quickstart

You'll need an active Telegram account, macOS or Linux, and a client that can launch a local MCP server over stdio.

### 1. Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you don't have it yet. On macOS, install Telegram's client library (TDLib) and the terminal QR encoder.

```sh
brew install tdlib qrencode
```

<details>
<summary>Linux prerequisites</summary>

Install the TDLib JSON library (`libtdjson.so`) for your OS and architecture. Use the [official TDLib build instructions](https://tdlib.github.io/td/build.html) if your distribution doesn't package it. For a library outside the standard loader paths, set `TDLIB_LIBRARY` to its absolute path.

Use an unlocked desktop keyring such as Secret Service or KWallet. Install `qrencode` if you want to sign in by QR.

</details>

Install Telegram MCP.

```sh
uv tool install --from git+https://github.com/shamanomania/telegram-mcp.git telegram-mcp
```

<details>
<summary>Install a pull request before merging</summary>

For a PR preview, run the following instead of the install command above. Set `pr_number` to the number in the GitHub PR URL. `--force` installs that build even if Telegram MCP is already installed.

```sh
pr_number=1
uv tool install --force --from "git+https://github.com/shamanomania/telegram-mcp.git@refs/pull/${pr_number}/head" telegram-mcp
```

</details>

<details>
<summary>Install from a checkout with Python</summary>

```sh
git clone https://github.com/shamanomania/telegram-mcp.git
cd telegram-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Use Python 3.11 or newer and replace `telegram-mcp` with `.venv/bin/telegram-mcp` in the commands below.

</details>

If your shell reports `command not found`, run `uv tool update-shell` and open a new terminal.

### 2. Sign in to Telegram

```sh
telegram-mcp setup
telegram-mcp doctor
telegram-mcp login
```

On the first login, the terminal asks for your Telegram application's `api_id` and `api_hash`. Enter the hash in the terminal. The input is hidden.

<details>
<summary>Where do I get my api_id and api_hash?</summary>

1. Open [Telegram API development tools](https://my.telegram.org/apps) and sign in with your phone number. The confirmation code arrives in Telegram.
2. Copy **App api_id** and **App api_hash** if the page already shows them. If Telegram shows a form, create an application and copy the two values it gives you.

An existing API application works for a fresh Telegram MCP login. [Telegram's guide to API credentials](https://core.telegram.org/api/obtaining_api_id)

</details>

When the QR appears, open **Telegram → Settings → Devices → Link Desktop Device** on your phone and scan it. Enter your two-step verification password in the terminal if prompted. Wait for `Telegram session is active` before continuing. To sign in with a phone number and login code, run `telegram-mcp login --phone`.

### 3. Choose one chat

`chats` shows recent chats and their numeric IDs in your terminal, including archived chats.

```sh
telegram-mcp chats
```

A chat named **Reading List** might appear like this.

```text
-1001234567890    "Reading List"
```

Replace the example ID and title below with the ones from your output. Keep the minus sign if the ID has one. `my-chat` is the alias you'll use when talking to the MCP client.

```sh
telegram-mcp allow-chat my-chat -1001234567890 --title "Reading List"
telegram-mcp doctor --account primary
```

Telegram MCP grants read access by default. `doctor --account primary` checks that Telegram confirms the session is active.

### 4. Connect your MCP client

For Codex, generate the configuration and add the displayed entry to `~/.codex/config.toml`.

```sh
telegram-mcp client-config codex
```

<details>
<summary>Claude, Cursor, VS Code, and other local clients</summary>

| Client | Generate config | Where to add it |
| --- | --- | --- |
| Claude Desktop | `telegram-mcp client-config claude` | **Settings → Developer → Edit Config** |
| Claude Code | `telegram-mcp client-config claude` | `mcpServers` in `.mcp.json`, or pass the server object to `claude mcp add-json --scope user telegram-mcp` |
| Cursor | `telegram-mcp client-config cursor` | `~/.cursor/mcp.json` |
| VS Code | `telegram-mcp client-config vscode` | **MCP: Open User Configuration** in the Command Palette |
| Other local stdio clients | `telegram-mcp client-config json` | Use the generated `command`, `args` and optional `env` |

Use local or user configuration for personal chats. Clients that only accept a remote MCP URL need a separate integration.

</details>

Restart the client or reload its MCP servers. Try this prompt.

> Read the last five messages from `my-chat` in Telegram.

## More examples

### Find a particular chat

```sh
telegram-mcp chats "Reading List"   # Search by title
telegram-mcp chats @publicname     # Find a public chat by username
```

For a public chat, use its ID and add `--username @publicname` to `allow-chat`. This enables reading public `t.me` message links for that chat.

### Read or summarize messages

> Summarize the last 20 messages in `my-chat` and list any action items.

> Read the message at `https://t.me/c/1234567890/42` in `my-chat`.

Use a message link from the chat you allowed. For a public link, configure the chat's username as shown above.

<details>
<summary>Send a message or remove access</summary>

Choose a chat you control, such as Saved Messages. Substitute its ID and title from `chats` in the command below. If you already allowed that chat, use its existing alias.

```sh
telegram-mcp allow-chat send-test 123456789 --title "Saved Messages" --access read_write
```

> Send "Telegram MCP is working" to `send-test` in Telegram.

Use `--access send` for send-only access. Run the following command to remove access to the chat.

```sh
telegram-mcp remove-chat send-test
```

</details>

<details>
<summary>Use a second Telegram account</summary>

```sh
telegram-mcp setup --account work
telegram-mcp login --account work
telegram-mcp chats --account work
telegram-mcp allow-chat work-updates -1002345678901 --account work --title "Work Updates"
```

Replace the example ID and title with a chat from that account. Ask your client to read `work-updates` on the `work` account.

</details>

## Reference

<details>
<summary>MCP tools and send retries</summary>

| Tool | Behavior |
| --- | --- |
| `list_telegram_chats` | Lists allowed account and chat aliases, chat IDs, and permissions. |
| `read_telegram_message` | Reads one message by a `message_id` from MCP or a matching `t.me` message link. |
| `read_telegram_history` | Reads newest to oldest. Pass `next_before_message_id` as `before_message_id` for the next page. TDLib can return fewer messages than requested. |
| `send_telegram_message` | Sends text as your personal Telegram account. Use `parse_mode: "html"` for Telegram HTML. |

Use a unique `request_id` for each intended send. Retrying a confirmed send with the same ID and payload returns its recorded receipt. If the outcome is unconfirmed, check the chat before trying a new request. Without an ID, matching payloads for the same account and chat share a receipt.

</details>

<details>
<summary>Local data and troubleshooting</summary>

- Run `telegram-mcp doctor` to find the local directory for profiles, sessions, and send receipts. Your API credentials and database encryption key are stored in the OS keyring.
- Use `telegram-mcp doctor --account primary` to check the session. If Telegram has signed it out, run `telegram-mcp login` again. If the session is busy, let the current operation finish and retry.
- Use `--data-dir "/absolute/path"` before a subcommand or set `TELEGRAM_MCP_DATA_DIR` to keep a separate profile directory. The generated client configuration includes that directory.
- Your MCP client receives messages returned by the tools. Check its data and tool approval settings before granting access to private chats or allowing sends.

</details>

<details>
<summary>Upgrade an existing checkout-based installation</summary>

A client that launches `server.py` from a checkout with a local `telegram-profiles.json` can keep using that profile and database. To switch to the packaged command, point it at the same directory and import the existing keyring entry.

```sh
export TELEGRAM_MCP_DATA_DIR="/absolute/path/to/existing/telegram-mcp"
telegram-mcp login --import-legacy-keyring
telegram-mcp doctor --account primary
telegram-mcp client-config codex
```

Repeat login with `--account ALIAS` for each additional account. Generate the configuration for your own MCP client.

</details>

<details>
<summary>Run the tests locally</summary>

```sh
uv sync --locked
uv run --locked python -m unittest -q
```

</details>
