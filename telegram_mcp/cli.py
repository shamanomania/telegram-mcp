"""Set up local profiles or run the Telegram MCP stdio server."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

import keyring
from keyring.errors import KeyringError

from . import server
from .profiles import (
    ACCESS, ALIAS, PROFILE_FILE, USERNAME, ChatProfile, edit_profiles, get_account, is_chat_id, load_profiles, parse_chat,
)
from .tdlib_session import (
    ROOT, TD_TIMEOUT, SessionError, account_directory, load_library, save_credentials, validate_account_alias,
)


def setup(account):
    validate_account_alias(account)
    with edit_profiles(server.ROOT, create=True) as accounts:
        accounts.setdefault(account, {"chats": {}})
    print(f"Profile {account!r} is ready. Next, run telegram-mcp login.")
    print(f"Local data directory: {server.ROOT}")


def login(args):
    if not sys.stdin.isatty():
        raise SessionError("Run login in a terminal so credential input can be hidden.")

    profile = get_account(args.account, server.ROOT)
    session = server.open_session(
        args.account, profile,
        login_method="phone" if args.phone else "qr",
        legacy_credentials=args.import_legacy_keyring or server.LEGACY_CREDENTIALS,
    )
    with session as client:
        user_id = client.call("getMe").get("id")
        if type(user_id) is not int or not 0 < user_id < 2**53:
            raise SessionError("Could not verify the signed-in account.")
        with edit_profiles(server.ROOT) as accounts:
            account = accounts[args.account]
            if account.get("user_id") not in (None, user_id):
                raise SessionError("This profile is already bound to a different Telegram account.")
            account["user_id"] = user_id
            if args.import_legacy_keyring:
                save_credentials(account_directory(args.account, server.ROOT), session.config)
    print("Telegram session is active. Run telegram-mcp chats to find a chat to allow.")


def recent_chat_ids(client, limit, deadline):
    ids = {}
    for kind in ("chatListMain", "chatListArchive"):
        result = client.call("getChats", chat_list={"@type": kind}, limit=limit, deadline=deadline)
        ids.update(dict.fromkeys(result["chat_ids"]))
    return ids


def discover_chats(client, query, limit):
    deadline = time.monotonic() + TD_TIMEOUT
    if query.startswith("@"):
        username = query[1:]
        if not USERNAME.fullmatch(username):
            raise SessionError("Enter a valid public @username.")
        return [client.call("searchPublicChat", username=username, deadline=deadline)]
    if query:
        result = client.call("searchChatsOnServer", query=query, limit=limit, deadline=deadline)
        ids = result["chat_ids"]
    else:
        ids = recent_chat_ids(client, limit, deadline)
    return [client.call("getChat", chat_id=chat_id, deadline=deadline) for chat_id in ids]


def chats(args):
    if not 1 <= args.limit <= 100:
        raise SessionError("Choose a limit from 1 to 100.")
    profile = get_account(args.account, server.ROOT)
    with server.open_session(args.account, profile) as client:
        found = discover_chats(client, args.query, args.limit)
    if not found:
        print("No chats found. Try a different title or @username.")
        return
    for entry in found:
        print(f'{entry["id"]}\t{json.dumps(entry["title"], ensure_ascii=False)}')


def allow_chat(args):
    if not ALIAS.fullmatch(args.alias) or not is_chat_id(args.chat_id):
        raise SessionError("Use a valid chat ID and an alias such as my-chat.")
    profile = get_account(args.account, server.ROOT)
    if profile.user_id is None:
        raise SessionError("Run telegram-mcp login to bind the profile to its Telegram account first.")

    username = args.username.removeprefix("@") if args.username else None
    with server.open_session(args.account, profile) as client:
        if username is not None:
            if not USERNAME.fullmatch(username):
                raise SessionError("Enter a valid public @username.")
            found = client.call("searchPublicChat", username=username)
            if found.get("id") != args.chat_id:
                raise SessionError("The public username points to a different chat ID.")
        elif args.title:
            target = ChatProfile(args.chat_id, args.title, args.access)
            found = server.verified_chat(client, target)
        else:
            deadline = time.monotonic() + TD_TIMEOUT
            if args.chat_id not in recent_chat_ids(client, 50, deadline):
                raise SessionError("Chat not found in recent conversations. Pass --title or --username.")
            found = client.call("getChat", chat_id=args.chat_id, deadline=deadline)
        if found.get("id") != args.chat_id or not found.get("title"):
            raise SessionError("Could not verify the selected chat ID and title.")

    chat = {"chat_id": args.chat_id, "title": found["title"], "access": args.access}
    if username is not None:
        chat["username"] = username
    parse_chat(args.alias, chat)
    with edit_profiles(server.ROOT) as accounts:
        account = accounts[args.account]
        if account.get("user_id") != profile.user_id:
            raise SessionError("The profile changed during setup. Try again.")
        existing = account["chats"].get(args.alias)
        if existing and existing["chat_id"] != args.chat_id:
            raise SessionError("This alias is already assigned to a different chat.")
        if existing:
            chat = {**existing, **chat}
        account["chats"][args.alias] = chat
    print(f"Allowed {args.access} as {args.account}/{args.alias}.")


def remove_chat(args):
    get_account(args.account, server.ROOT)
    with edit_profiles(server.ROOT) as accounts:
        if accounts[args.account]["chats"].pop(args.alias, None) is None:
            raise SessionError("No allowed chat with that alias.")
    print(f"Removed {args.account}/{args.alias} from the allowlist.")


def doctor(args):
    print(f"Local data directory: {server.ROOT}", flush=True)
    profiles = load_profiles(server.ROOT)
    print(f"Profile file valid. Accounts: {len(profiles)}. Allowed chats: {sum(len(p.chats) for p in profiles.values())}.")
    load_library()
    print("TDLib loaded.")
    try:
        if keyring.get_keyring().priority <= 0:
            raise KeyringError("No usable backend")
    except KeyringError:
        raise SessionError("Set up an OS keyring. On Linux, unlock a desktop Secret Service or KWallet.") from None
    print("OS keyring backend available.")
    if args.account is not None:
        profile = get_account(args.account, server.ROOT)
        with server.open_session(args.account, profile) as client:
            client.confirm_active_session()
        print(f"Telegram confirmed an active session for {args.account!r}.")


def print_client_config(client, *, legacy_script):
    executable = str(Path(sys.executable).absolute())
    arguments = [str(legacy_script)] if legacy_script else ["-m", "telegram_mcp"]
    if not legacy_script:
        arguments.extend(["--data-dir", str(server.ROOT)])
    environment = {}
    if library := os.environ.get("TDLIB_LIBRARY"):
        environment["TDLIB_LIBRARY"] = str(Path(library).expanduser().resolve())

    name = "telegram-mcp"
    config = {"command": executable, "args": arguments}
    if environment:
        config["env"] = environment
    if client == "codex":
        print(f"[mcp_servers.{name}]")
        print(f"command = {json.dumps(executable, ensure_ascii=False)}")
        print(f"args = {json.dumps(arguments, ensure_ascii=False)}")
        print("tool_timeout_sec = 120")
        if environment:
            print(f"\n[mcp_servers.{name}.env]")
            print(f'TDLIB_LIBRARY = {json.dumps(environment["TDLIB_LIBRARY"], ensure_ascii=False)}')
    elif client == "vscode":
        print(json.dumps({"servers": {name: {"type": "stdio", **config}}}, indent=2))
    else:
        if client == "cursor":
            config["type"] = "stdio"
        print(json.dumps({"mcpServers": {name: config}}, indent=2))


def add_account_option(parser, *, default="primary"):
    parser.add_argument("--account", default=default, help="Local profile alias.")


def argument_parser():
    parser = argparse.ArgumentParser(
        prog="telegram-mcp",
        description=__doc__,
        epilog="Without a subcommand, run the MCP server over stdio.",
    )
    parser.add_argument("--data-dir", type=Path, help="Override the per-user data directory (or set TELEGRAM_MCP_DATA_DIR).")
    commands = parser.add_subparsers(dest="command")
    setup_parser = commands.add_parser("setup", help="Create an account profile with no allowed chats.")
    add_account_option(setup_parser)
    login_parser = commands.add_parser("login", help="Sign in and bind a profile to the Telegram account.")
    add_account_option(login_parser)
    login_parser.add_argument("--phone", action="store_true", help="Use your phone number instead of a QR code.")
    login_parser.add_argument("--import-legacy-keyring", action="store_true", help="Reuse credentials for an existing legacy database.")
    chats_parser = commands.add_parser("chats", help="Find chat IDs in Telegram, locally in your terminal.")
    add_account_option(chats_parser)
    chats_parser.add_argument("query", nargs="?", default="", help="Chat title or @username. Defaults to recent chats.")
    chats_parser.add_argument("--limit", type=int, default=20)
    allow_parser = commands.add_parser("allow-chat", help="Add a chat to the MCP allowlist or change its permissions.")
    add_account_option(allow_parser)
    allow_parser.add_argument("alias")
    allow_parser.add_argument("chat_id", type=int)
    allow_parser.add_argument("--access", choices=sorted(ACCESS), default="read")
    allow_parser.add_argument("--title", help="Help locate an older chat by its title.")
    allow_parser.add_argument("--username", help="Public username, also used to resolve t.me links.")
    remove_parser = commands.add_parser("remove-chat", help="Remove a chat from the MCP allowlist.")
    add_account_option(remove_parser)
    remove_parser.add_argument("alias")
    send_to_parser = commands.add_parser("send-to", help="Choose which recipients this account can send text to.")
    add_account_option(send_to_parser)
    send_to_parser.add_argument("recipients", choices=("configured", "*"), help="Quote '*' to allow any recipient.")
    config_parser = commands.add_parser("client-config", help="Print a ready-to-paste stdio client configuration.")
    config_parser.add_argument("client", choices=("codex", "claude", "cursor", "vscode", "json"))
    doctor_parser = commands.add_parser("doctor", help="Check the local setup and optionally verify a Telegram session.")
    add_account_option(doctor_parser, default=None)
    return parser


def main(argv=None, *, legacy_root=None):
    os.umask(0o077)
    args = argument_parser().parse_args(argv)
    custom_data_dir = args.data_dir or os.environ.get("TELEGRAM_MCP_DATA_DIR")
    legacy = not custom_data_dir and legacy_root and (legacy_root / PROFILE_FILE).is_file()
    server.ROOT = Path(custom_data_dir or (legacy_root if legacy else ROOT)).expanduser().resolve()
    server.LEGACY_CREDENTIALS = bool(legacy)

    try:
        match args.command:
            case None:
                server.mcp.run(transport="stdio")
            case "setup":
                setup(args.account)
            case "login":
                login(args)
            case "chats":
                chats(args)
            case "allow-chat":
                allow_chat(args)
            case "remove-chat":
                remove_chat(args)
            case "send-to":
                with edit_profiles(server.ROOT) as accounts:
                    if args.account not in accounts:
                        raise SessionError("Account profile missing. Run telegram-mcp setup first.")
                    accounts[args.account]["send_to"] = args.recipients
                print(f"Text recipients for {args.account}: {args.recipients}")
            case "client-config":
                print_client_config(
                    args.client, legacy_script=legacy_root / "server.py" if legacy else None,
                )
            case "doctor":
                doctor(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, SessionError) as error:
        raise SystemExit(str(error)) from None
