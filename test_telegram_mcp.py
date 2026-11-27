from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, Mock, call, patch

from mcp.server.fastmcp.exceptions import ToolError

from telegram_mcp import cli, server, tdlib_session
from telegram_mcp.profiles import ChatProfile, edit_profiles
from telegram_mcp.tdlib_session import RequestError, SendFailed, SessionError, TDLib, account_directory


TARGET = ChatProfile(-1000000009876, "Test Chat", "read_write")
ACCOUNT = 42
SAMPLE_MESSAGE = "Test message 🛠"


class ChatScopeTest(unittest.TestCase):
    def test_rejects_foreign_link_before_opening_telegram_session(self):
        with patch.object(server, "get_chat", return_value=(None, TARGET)):
            with patch.object(server, "LocalSession") as session:
                with self.assertRaises(SessionError):
                    server.read_telegram_message("primary", "sandbox", url="https://t.me/c/9877/123")
        session.assert_not_called()

    def test_resolves_only_expected_chat_after_cache_miss(self):
        client = Mock()
        client.call.side_effect = [
            RequestError("getChat", {"code": 400, "message": "Chat not found"}),
            {"chat_ids": [TARGET.chat_id, -1000000009877]},
            {"id": TARGET.chat_id, "title": TARGET.title},
        ]

        self.assertEqual(server.verified_chat(client, TARGET)["id"], TARGET.chat_id)
        self.assertEqual(client.call.call_args_list, [
            call("getChat", chat_id=TARGET.chat_id),
            call("searchChatsOnServer", query=TARGET.title, limit=20, deadline=ANY),
            call("getChat", chat_id=TARGET.chat_id, deadline=ANY),
        ])

    def test_resolves_renamed_chat_in_archive_by_id(self):
        client = Mock()
        missing = RequestError("getChat", {"code": 400, "message": "Chat not found"})
        client.call.side_effect = [
            missing,
            {"chat_ids": []},
            RequestError("loadChats", {"code": 404, "message": "Not Found"}),
            missing,
            {"@type": "ok"},
            {"id": TARGET.chat_id, "title": "Renamed chat"},
        ]
        chat = server.verified_chat(client, TARGET)
        self.assertEqual(chat["id"], TARGET.chat_id)
        client.call.assert_any_call(
            "loadChats", chat_list={"@type": "chatListArchive"}, limit=100, deadline=ANY,
        )

    def test_does_not_search_after_unexpected_error(self):
        client = Mock()
        client.call.side_effect = RequestError("getChat", {"code": 500, "message": "Internal"})
        with self.assertRaises(RequestError):
            server.verified_chat(client, TARGET)
        client.call.assert_called_once_with("getChat", chat_id=TARGET.chat_id)


class SendTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        (root / "telegram-profiles.json").write_text(
            json.dumps({"accounts": {"primary": {"chats": {"sandbox": {
                "chat_id": TARGET.chat_id,
                "title": TARGET.title,
                "access": "read_write",
            }}}}}),
            encoding="utf-8",
        )
        self.enterContext(patch.object(server, "ROOT", root))
        session = self.enterContext(patch.object(server, "LocalSession"))
        self.client = session.return_value.__enter__.return_value
        self.client.legacy = True
        self.checked_chat = {"id": TARGET.chat_id, "title": TARGET.title}
        self.public_chat = self.checked_chat
        self.sent_count = 0
        self.client.call.side_effect = self.handle_request
        self.client.confirm_send.return_value = {
            "chat_id": TARGET.chat_id, "id": 123456, "sender_id": {"user_id": ACCOUNT},
        }

    def handle_request(self, method, **kwargs):
        if method == "searchPublicChat":
            self.assertEqual(kwargs["username"].casefold(), "reader")
            return self.public_chat
        if method == "getChat":
            self.assertEqual(kwargs["chat_id"], TARGET.chat_id)
            return self.checked_chat
        if method == "getMe":
            return {"id": ACCOUNT, "type": {"@type": "userTypeRegular"}}
        if method == "sendMessage":
            self.assertEqual(kwargs["chat_id"], TARGET.chat_id)
            self.assertFalse(kwargs["input_message_content"]["clear_draft"])
            self.sent_count += 1
            return {"chat_id": TARGET.chat_id}
        self.fail(f"Unexpected TDLib request: {method}")

    def send(self, text=SAMPLE_MESSAGE, **options):
        return server.send_telegram_message("primary", "sandbox", text, **options)

    def enable_wildcard(self):
        with edit_profiles(server.ROOT) as accounts:
            profile = accounts["primary"]
            profile.update(user_id=ACCOUNT, send_to="*")
            profile["chats"]["sandbox"]["access"] = "read"

    def test_wildcard_sends_by_username_id_or_read_alias_with_one_receipt(self):
        self.enable_wildcard()
        for address in ("@Reader", str(TARGET.chat_id), "sandbox"):
            with self.subTest(address=address):
                result = server.send_telegram_message("primary", address, SAMPLE_MESSAGE, request_id="one")
                self.assertEqual(result["duplicate"], address != "@Reader")
                self.assertEqual(self.client.send_chat_id, TARGET.chat_id)
        self.assertEqual(self.sent_count, 1)

    def test_retry_after_username_transfer_cannot_reach_the_new_owner(self):
        self.enable_wildcard()
        server.send_telegram_message("primary", "@Reader", SAMPLE_MESSAGE, request_id="one")
        self.public_chat = {"id": 1002, "title": "New owner"}
        with self.assertRaisesRegex(SessionError, "different recipient"):
            server.send_telegram_message("primary", "@reader", SAMPLE_MESSAGE, request_id="one")
        self.assertEqual(self.sent_count, 1)

    def test_cold_private_id_must_resolve_to_that_exact_chat(self):
        self.enable_wildcard()
        self.client.call.side_effect = [
            RequestError("getChat", {"code": 400, "message": "Chat not found"}),
            {"id": 102, "title": "Different contact"},
        ]
        with self.assertRaisesRegex(SessionError, "different chat"):
            server.send_telegram_message("primary", "101", SAMPLE_MESSAGE)
        self.client.call.assert_has_calls([
            call("getChat", chat_id=101),
            call("createPrivateChat", user_id=101, force=False, deadline=ANY),
        ])

    def test_wildcard_gives_no_read_access_and_accepts_only_one_exact_recipient(self):
        self.enable_wildcard()
        for addr in ("@reader", "123", "*"):
            with self.subTest(addr=addr), self.assertRaisesRegex(SessionError, "not allowed"):
                server.read_telegram_history("primary", addr)
            with self.subTest(addr=addr), self.assertRaisesRegex(SessionError, "not allowed"):
                server.read_telegram_message("primary", addr, message_id=1)
        for addr in ("*", "A chat title", "@one,@two", "0", str(2**53)):
            with self.subTest(addr=addr), self.assertRaisesRegex(SessionError, "one configured alias"):
                server.send_telegram_message("primary", addr, SAMPLE_MESSAGE)
        self.client.call.assert_not_called()

    def test_direct_sends_require_the_wildcard(self):
        for addr in ("@reader", str(TARGET.chat_id)):
            with self.subTest(addr=addr), self.assertRaisesRegex(SessionError, "not allowed"):
                server.send_telegram_message("primary", addr, SAMPLE_MESSAGE)
        self.client.call.assert_not_called()

    def test_sends_without_reference_and_reuses_receipt_on_repeat(self):
        first = self.send()
        repeated = self.send()
        self.assertFalse(first["duplicate"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(first["message_id"], repeated["message_id"])
        self.assertEqual(self.sent_count, 1)

    def test_returns_confirmed_receipt_even_when_current_sender_has_changed(self):
        first = self.send(request_id="confirmed")
        self.checked_chat["message_sender_id"] = {"chat_id": -1000000009877}
        repeat = self.send(request_id="confirmed")
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(repeat["message_id"], first["message_id"])
        self.assertEqual(self.sent_count, 1)

    def test_unknown_outcome_remains_pending_and_cannot_be_sent_again(self):
        self.client.confirm_send.side_effect = SessionError("Ожидание подтверждения истекло.")
        with self.assertRaises(SessionError):
            self.send(request_id="attempt-1")
        with self.assertRaisesRegex(SessionError, "previous send was not confirmed"):
            self.send(request_id="attempt-1")
        self.assertEqual(self.sent_count, 1)

    def test_confirmed_failure_does_not_reserve_operation_forever(self):
        self.client.confirm_send.side_effect = SendFailed("Telegram rejected the message.")
        with self.assertRaises(SendFailed):
            self.send()
        self.client.confirm_send.side_effect = None
        self.assertFalse(self.send()["duplicate"])
        self.assertEqual(self.sent_count, 2)

    def test_explicit_request_id_is_bound_to_content(self):
        self.send(request_id="attempt-1")
        with self.assertRaisesRegex(SessionError, "a different message"):
            self.send("Другой текст", request_id="attempt-1")
        self.assertEqual(self.sent_count, 1)

    def test_rejects_alternate_sender_before_queueing(self):
        self.checked_chat["message_sender_id"] = {"chat_id": -1000000009877}
        with self.assertRaisesRegex(SessionError, "a different identity"):
            self.send()
        self.assertEqual(self.sent_count, 0)

    def test_formats_generic_text_with_entities_and_deduplicates(self):
        msg = "Обычное сообщение с эмодзи 🛠"
        args = ("primary", "sandbox", msg)
        result = server.send_telegram_message(*args)
        repeat = server.send_telegram_message(*args)
        self.assertEqual(result["message_id"], repeat["message_id"])
        self.assertEqual(self.sent_count, 1)
        sent = next(request for request in self.client.call.call_args_list if request.args == ("sendMessage",))
        self.assertEqual(sent.kwargs["input_message_content"]["text"]["text"], msg)

    def test_rejects_html_without_visible_text_before_reserving_send(self):
        self.client.call.side_effect = [{"@type": "formattedText", "text": "", "entities": []}]
        with self.assertRaisesRegex(SessionError, "an empty message"):
            server.send_telegram_message("primary", "sandbox", "<b></b>", parse_mode="html")
        self.assertEqual(self.sent_count, 0)
        self.client.call.assert_called_once_with(
            "parseTextEntities", text="<b></b>", parse_mode={"@type": "textParseModeHTML"},
        )

    def test_missing_delivered_id_keeps_send_unconfirmed(self):
        self.client.confirm_send.return_value.pop("id")
        with self.assertRaises(SessionError):
            self.send()
        with self.assertRaisesRegex(SessionError, "Retry with this request_id is blocked"):
            self.send()
        self.assertEqual(self.sent_count, 1)


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        config = {
            "accounts": {
                "primary": {"chats": {
                    "review": {"chat_id": TARGET.chat_id, "title": TARGET.title, "access": "read_write"},
                }},
                "secondary": {"user_id": 34, "chats": {
                    "personal": {"chat_id": 101, "title": "Test contact", "access": "read"},
                    "channel": {"chat_id": -1000000009877, "title": "Test channel",
                                "access": "read", "username": "example"},
                }},
            },
        }
        (self.root / "telegram-profiles.json").write_text(json.dumps(config), encoding="utf-8")
        self.enterContext(patch.object(server, "ROOT", self.root))

    def test_does_not_allow_cross_account_lookup_or_send_to_read_only_chat(self):
        with patch.object(server, "LocalSession") as session:
            with self.assertRaisesRegex(SessionError, "operation is not allowed"):
                server.read_telegram_history("primary", "personal")
            with self.assertRaisesRegex(SessionError, "operation is not allowed"):
                server.send_telegram_message("secondary", "personal", "Test")
        session.assert_not_called()

    def test_rejects_link_to_comments_before_opening_telegram_session(self):
        with patch.object(server, "get_chat", return_value=(None, TARGET)):
            with patch.object(server, "LocalSession") as session:
                with self.assertRaisesRegex(SessionError, "unsupported parameters"):
                    server.read_telegram_message("primary", "sandbox", url="https://t.me/c/9876/123?comment=456")
        session.assert_not_called()

    def test_uses_account_identity_and_configured_chat_on_history(self):
        def message(message_id):
            return {"chat_id": 101, "id": message_id,
                    "content": {"@type": "messageText", "text": {"text": "Test"}}}

        with patch.object(server, "LocalSession") as session:
            client = session.return_value.__enter__.return_value
            client.call.side_effect = [{"id": 101}, {"messages": [message(50), message(49), message(48)]}]
            page = server.read_telegram_history("secondary", "personal", limit=2, before_message_id=50)
        session.assert_called_once_with(account="secondary", expected_user_id=34, root=self.root, legacy_credentials=False)
        client.call.assert_has_calls([
            call("getChat", chat_id=101),
            call("getChatHistory", chat_id=101, from_message_id=50, offset=0, limit=3, only_local=False),
        ])
        self.assertEqual([item["message_id"] for item in page["messages"]], [49, 48])
        self.assertEqual(page["next_before_message_id"], 48)

    def test_resolves_configured_public_link_and_checks_resolved_chat(self):
        link = "https://t.me/example/2"
        with patch.object(server, "LocalSession") as session:
            client = session.return_value.__enter__.return_value
            client.call.return_value = {"chat_id": TARGET.chat_id, "message": {}}
            with self.assertRaisesRegex(SessionError, "message is unavailable"):
                server.read_telegram_message("secondary", "channel", url=link)
            client.call.assert_called_once_with("getMessageLinkInfo", url=link)
        self.assertEqual(account_directory("primary", self.root), self.root)
        self.assertEqual(account_directory("secondary", self.root), self.root / "accounts" / "secondary")

    def test_reads_message_from_topic_link_without_searching_by_title(self):
        message = {
            "chat_id": TARGET.chat_id, "id": 123,
            "content": {"@type": "messageText", "text": {"text": "Example"}},
        }
        with patch.object(server, "LocalSession") as session:
            client = session.return_value.__enter__.return_value
            client.call.return_value = {"chat_id": TARGET.chat_id, "message": message}
            result = server.read_telegram_message("primary", "review", url="https://t.me/c/9876/42/123?single")
            self.assertEqual(result["message_id"], 123)
            client.call.assert_called_once_with("getMessageLinkInfo", url="https://t.me/c/9876/123?single&thread=42")

    def test_loads_private_chat_when_tdlib_cannot_resolve_link_on_cold_cache(self):
        with patch.object(server, "LocalSession") as session:
            client = session.return_value.__enter__.return_value
            client.call.side_effect = [
                {"chat_id": 0},
                {"id": TARGET.chat_id},
                {"chat_id": TARGET.chat_id, "message": {"chat_id": 101}},
            ]
            link = "https://t.me/c/9876/123"
            with self.assertRaisesRegex(SessionError, "message from a different chat"):
                server.read_telegram_message("primary", "review", url=link)
            self.assertEqual(client.call.call_args_list, [
                call("getMessageLinkInfo", url=link),
                call("getChat", chat_id=TARGET.chat_id),
                call("getMessageLinkInfo", url=link),
            ])

    def test_publishes_only_configured_aliases(self):
        listed = server.list_telegram_chats()["accounts"]
        self.assertEqual([chat["alias"] for chat in listed["primary"]["chats"]], ["review"])
        self.assertEqual([chat["access"] for chat in listed["secondary"]["chats"]], ["read", "read"])


class TDLibTest(unittest.TestCase):
    def test_login_requires_the_server_to_confirm_this_session(self):
        client = TDLib.__new__(TDLib)
        client.authorization = {"@type": "authorizationStateReady"}
        client.call = Mock(return_value={"sessions": [{"is_current": False}]})
        with self.assertRaisesRegex(SessionError, "did not confirm an active session"):
            client.confirm_active_session()
        client.call.assert_called_once_with("getActiveSessions")

    def test_confirm_send_handles_immediate_failure(self):
        client = Mock(send_chat_id=TARGET.chat_id)
        message = {"chat_id": TARGET.chat_id, "sending_state": {"@type": "messageSendingStateFailed"}}
        with self.assertRaises(SendFailed):
            TDLib.confirm_send(client, message)
        client.receive.assert_not_called()

    def test_confirm_send_uses_buffered_update_for_matching_message(self):
        delivered = {"chat_id": TARGET.chat_id, "id": 120}
        client = Mock(send_chat_id=TARGET.chat_id, send_events={
            50: {"@type": "updateMessageSendSucceeded", "message": delivered},
        })
        pending = {
            "chat_id": TARGET.chat_id, "id": 50,
            "sending_state": {"@type": "messageSendingStatePending"},
        }
        self.assertEqual(TDLib.confirm_send(client, pending), delivered)
        client.receive.assert_not_called()

    def test_close_failure_preserves_success_and_prevents_reopening_database(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        locks = []
        with (
            patch.object(tdlib_session, "credentials", return_value={}),
            patch.object(tdlib_session, "TDLib") as library,
            patch.object(tdlib_session, "_UNCLOSED_LOCKS", locks),
            self.assertLogs(tdlib_session.LOGGER, level="WARNING"),
        ):
            library.return_value.close.side_effect = OSError("cleanup failure")
            with tdlib_session.LocalSession(root=directory) as client:
                self.assertIs(client, library.return_value)
            self.assertEqual(len(locks), 1)
            self.addCleanup(locks[0].close)
            self.assertFalse(locks[0].closed)
            with self.assertRaisesRegex(SessionError, "Restart the MCP server"):
                with tdlib_session.LocalSession(root=directory):
                    self.fail("Session reopened while TDLib might still be using the database")


class SessionLoginTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(tdlib_session, "credentials", return_value={}))

    def test_server_revocation_reopens_for_login_after_old_client_closes(self):
        previous = TDLib.__new__(TDLib)
        previous.authorization = {"@type": "authorizationStateReady"}
        previous.authorize = Mock()
        previous.call = Mock(side_effect=RequestError("getActiveSessions", {"code": 401}))

        def close_revoked_client(_deadline):
            previous.authorization = {"@type": "authorizationStateClosed"}

        previous.receive = Mock(side_effect=close_revoked_client)
        replacement = TDLib.__new__(TDLib)
        replacement.authorization = {"@type": "authorizationStateReady"}
        replacement.authorize = Mock()
        replacement.call = Mock(return_value={"sessions": [{"is_current": True}]})
        replacement.close = Mock()

        def create_client(_send_chat_id):
            if previous.call.called:
                self.assertEqual(previous.authorization["@type"], "authorizationStateClosed")
                return replacement
            return previous

        with (
            patch.object(tdlib_session, "TDLib", side_effect=create_client) as create,
            patch("builtins.print") as notice,
            tdlib_session.LocalSession(login_method="qr", root=self.root) as client,
        ):
            self.assertIs(client, replacement)
        self.assertEqual(create.call_count, 2)
        previous.call.assert_called_once_with("getActiveSessions")
        replacement.authorize.assert_called_once_with({}, "qr", self.root / "session-db")
        replacement.call.assert_called_once_with("getActiveSessions")
        previous.receive.assert_called_once()
        notice.assert_called_once()

    def test_timeout_during_server_check_keeps_existing_session(self):
        with patch.object(tdlib_session, "TDLib") as create:
            client = create.return_value
            client.is_closing = False
            client.confirm_active_session.side_effect = SessionError("TDLib did not respond in time.")
            with self.assertRaisesRegex(SessionError, "did not respond in time"):
                with tdlib_session.LocalSession(login_method="qr", root=self.root):
                    self.fail("Login succeeded without network confirmation")
            create.assert_called_once()
            client.wait_closed.assert_not_called()
            client.close.assert_called_once()


class MCPValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_coercion_of_boolean_and_string_into_message_id(self):
        with patch.object(server, "get_chat") as get_chat:
            for invalid_id in (True, "123"):
                with self.subTest(message_id=invalid_id), self.assertRaisesRegex(ToolError, "valid integer"):
                    await server.mcp.call_tool("read_telegram_message", {
                        "account": "primary", "chat": "sandbox", "message_id": invalid_id,
                    })
        get_chat.assert_not_called()


class TerminalQrTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(tdlib_session.sys, "stdout", Mock(isatty=Mock(return_value=True))))
        self.enterContext(patch.object(tdlib_session.shutil, "which", return_value="qrencode"))
        self.size = self.enterContext(patch.object(tdlib_session.os, "get_terminal_size"))
        self.size.return_value = tdlib_session.os.terminal_size((80, 24))
        self.encoder = self.enterContext(patch.object(tdlib_session.subprocess, "run"))
        self.draw = self.enterContext(patch("builtins.print"))

    def test_refreshes_one_screen_and_restores_terminal_before_password(self):
        first = "tg://login?token=fixture-first"
        second = "tg://login?token=fixture-second"
        self.encoder.side_effect = [Mock(stdout="QR-1\n"), Mock(stdout="QR-2\n")]
        with tdlib_session.TerminalQr() as qr:
            qr.show(first)
            qr.show(first)
            qr.show(second)
            qr.clear()
        first_frame, second_frame, restore = [printed.args[0] for printed in self.draw.call_args_list]
        self.assertTrue(first_frame.startswith("\x1b[?1049h\x1b[H\x1b[2J"))
        self.assertTrue(second_frame.startswith("\x1b[H\x1b[2J"))
        self.assertTrue(first_frame.endswith("QR-1"))
        self.assertTrue(second_frame.endswith("QR-2"))
        self.assertNotIn(first, first_frame)
        self.assertNotIn(second, second_frame)
        self.assertEqual(restore, "\x1b[?1049l")
        self.assertEqual(self.encoder.call_count, 2)

    def test_rejects_small_terminal_before_drawing_partial_qr(self):
        self.size.return_value = tdlib_session.os.terminal_size((40, 20))
        self.encoder.return_value = Mock(stdout="QR\n")
        with tdlib_session.TerminalQr() as qr:
            with self.assertRaisesRegex(SessionError, "Resize the terminal"):
                qr.show("tg://login?token=fixture")
        self.draw.assert_not_called()

    def test_restores_terminal_on_interrupted_login(self):
        self.encoder.return_value = Mock(stdout="QR\n")
        with self.assertRaises(KeyboardInterrupt):
            with tdlib_session.TerminalQr() as qr:
                qr.show("tg://login?token=fixture")
                raise KeyboardInterrupt
        self.draw.assert_called_with("\x1b[?1049l", end="", flush=True)


class LoginPasswordTest(unittest.TestCase):
    def test_password_rejection_keeps_the_attempt_open_for_user_retry(self):
        client = Mock()
        client.call.side_effect = [
            RequestError("checkAuthenticationPassword", {"code": 400, "message": "PASSWORD_HASH_INVALID"}),
            {"@type": "ok"},
        ]
        with (
            patch.object(tdlib_session.getpass, "getpass", side_effect=["wrong", "correct"]),
            patch("builtins.print") as notice,
        ):
            TDLib.check_password(client)
        self.assertEqual(client.call.call_args_list, [
            call("checkAuthenticationPassword", password="wrong"),
            call("checkAuthenticationPassword", password="correct"),
        ])
        notice.assert_called_once()


class LoginCliTest(unittest.TestCase):
    def test_ctrl_c_exits_with_a_short_cancellation_message(self):
        stderr = StringIO()
        with (
            patch.object(cli.sys, "argv", ["server.py", "login", "--account", "primary"]),
            patch.object(cli.sys, "stdin", Mock(isatty=Mock(return_value=True))),
            patch.object(cli.os, "umask"),
            patch.object(cli, "get_account"),
            patch.object(server, "open_session", side_effect=KeyboardInterrupt),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as stopped,
        ):
            cli.main()
        self.assertEqual(stopped.exception.code, 130)
        self.assertEqual(stderr.getvalue().strip(), "Cancelled.")


if __name__ == "__main__":
    unittest.main()
