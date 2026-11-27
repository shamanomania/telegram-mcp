from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_mcp import cli, server, tdlib_session
from telegram_mcp.profiles import PROFILE_FILE, edit_profiles, load_profiles


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())) / "data with spaces"
        self.enterContext(patch.object(cli.os, "umask"))
        self.enterContext(patch.object(server, "ROOT", self.root))
        self.enterContext(patch.object(server, "LEGACY_CREDENTIALS", False))
        self.enterContext(patch.dict(os.environ, clear=True))

    def run_cli(self, *args):
        with redirect_stdout(StringIO()) as output:
            cli.main(["--data-dir", str(self.root), *args])
        return output.getvalue()

    def test_setup_is_repeatable_and_has_no_default_permissions(self):
        self.run_cli("setup")
        self.run_cli("setup", "--account", "work")
        with edit_profiles(self.root) as accounts:
            accounts["primary"]["user_id"] = 42
        self.run_cli("setup")
        profiles = load_profiles(self.root)
        self.assertEqual(set(profiles), {"primary", "work"})
        self.assertEqual(profiles["primary"].user_id, 42)
        self.assertEqual(profiles["primary"].chats, {})
        self.assertEqual((self.root / PROFILE_FILE).stat().st_mode & 0o777, 0o600)

    def test_client_configs_preserve_spaces_in_paths_and_do_not_require_a_profile(self):
        library = self.root / "native library.so"
        with patch.dict(os.environ, {"TDLIB_LIBRARY": str(library)}):
            codex = tomllib.loads(self.run_cli("client-config", "codex"))["mcp_servers"]
            claude = json.loads(self.run_cli("client-config", "claude"))["mcpServers"]
            cursor = json.loads(self.run_cli("client-config", "cursor"))["mcpServers"]
            vscode = json.loads(self.run_cli("client-config", "vscode"))["servers"]

        for entries in (codex, claude, cursor, vscode):
            entry = entries["telegram-mcp"]
            self.assertEqual(entry["command"], sys.executable)
            self.assertEqual(entry["args"], ["-m", "telegram_mcp", "--data-dir", str(self.root.resolve())])
            self.assertEqual(entry["env"], {"TDLIB_LIBRARY": str(library.resolve())})
        self.assertEqual(vscode["telegram-mcp"]["type"], "stdio")
        self.assertFalse(self.root.exists())

    def test_wildcard_is_reversible_and_scoped_to_a_bound_account(self):
        self.run_cli("setup")
        self.run_cli("setup", "--account", "work")
        with self.assertRaisesRegex(SystemExit, "login"):
            self.run_cli("send-to", "*", "--account", "work")
        with edit_profiles(self.root) as accounts:
            accounts["work"]["user_id"] = 42
        self.run_cli("send-to", "*", "--account", "work")
        listed = server.list_telegram_chats()["accounts"]
        self.assertEqual(listed["work"], {"chats": [], "send_to": "*"})
        self.assertEqual(listed["primary"], {"chats": []})
        self.run_cli("send-to", "configured", "--account", "work")
        self.assertEqual(load_profiles(self.root)["work"].send_to, "configured")

    def test_public_username_must_resolve_to_the_selected_id(self):
        self.run_cli("setup")
        with edit_profiles(self.root) as accounts:
            accounts["primary"]["user_id"] = 42

        with patch.object(server, "open_session") as session:
            client = session.return_value.__enter__.return_value
            client.call.return_value = {"id": -202, "title": "Test Chat"}
            with self.assertRaisesRegex(SystemExit, "different chat ID"):
                self.run_cli("allow-chat", "example", "-101", "--username", "@public_chat")
            self.assertEqual(load_profiles(self.root)["primary"].chats, {})

            self.run_cli("allow-chat", "example", "-202", "--username", "@public_chat")
            chat = load_profiles(self.root)["primary"].chats["example"]
            self.assertEqual(chat.access, "read")
            self.assertEqual(chat.username, "public_chat")
            self.run_cli("remove-chat", "example")
        self.assertEqual(load_profiles(self.root)["primary"].chats, {})

    def test_failed_profile_edit_preserves_existing_json(self):
        self.run_cli("setup")
        path = self.root / PROFILE_FILE
        before = path.read_bytes()
        with self.assertRaises(tdlib_session.SessionError):
            with edit_profiles(self.root) as accounts:
                accounts["primary"]["chats"]["bad alias"] = {}
        self.assertEqual(path.read_bytes(), before)


class KeyringIsolationTest(unittest.TestCase):
    def test_fresh_installation_does_not_read_global_legacy_key(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(tdlib_session.keyring, "get_password", return_value=None) as lookup:
                with self.assertRaisesRegex(tdlib_session.SessionError, "login"):
                    tdlib_session.credentials("primary", root)
            lookup.assert_called_once_with(tdlib_session.SERVICE, tdlib_session.credential_key(root))
            self.assertNotEqual(
                tdlib_session.credential_key(root),
                tdlib_session.credential_key(root / "accounts" / "work"),
            )
            self.assertNotEqual(
                tdlib_session.credential_key(root),
                tdlib_session.credential_key(root / "other-installation"),
            )


class StdioProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_can_discover_tools_and_read_an_empty_allowlist(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "data with spaces"
            with edit_profiles(root, create=True) as accounts:
                accounts["primary"] = {"chats": {}}
            command = StdioServerParameters(
                command=sys.executable,
                args=["-m", "telegram_mcp", "--data-dir", str(root)],
                cwd=folder,
            )
            async with stdio_client(command) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    self.assertEqual(set(tools), {
                        "list_telegram_chats", "read_telegram_history",
                        "read_telegram_message", "send_telegram_message",
                    })
                    self.assertTrue(tools["read_telegram_history"].annotations.readOnlyHint)
                    self.assertFalse(tools["send_telegram_message"].annotations.readOnlyHint)
                    result = await session.call_tool("list_telegram_chats")
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent, {"accounts": {"primary": {"chats": []}}})
                    self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
                    denied = await session.call_tool(
                        "read_telegram_history",
                        {"account": "primary", "chat": "unknown"},
                    )
                    self.assertTrue(denied.isError)
                    self.assertIn("operation is not allowed", denied.content[0].text)
