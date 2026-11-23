"""Private local TDLib session. Only the MCP server defines exposed Telegram operations."""

import base64
import ctypes
import ctypes.util
import errno
import fcntl
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

import keyring
from keyring.errors import KeyringError
from platformdirs import user_data_path

APP = "telegram-mcp"
ROOT = user_data_path(APP, appauthor=False)
SERVICE = "Telegram MCP"
TD_TIMEOUT = 30
TD_CLOSE_TIMEOUT = 5
_UNCLOSED_LOCKS = []
_CLIENT_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


class SessionError(RuntimeError):
    pass


class RequestError(SessionError):
    def __init__(self, method, response):
        self.method = method
        self.code = response.get("code")
        self.detail = response.get("message", "")
        super().__init__(f"Telegram rejected {method} (code {self.code}).")


class SendFailed(SessionError):
    pass


class TerminalQr:
    def __init__(self):
        self.last_link = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.clear()

    def show(self, link):
        if link == self.last_link:
            return
        if not sys.stdout.isatty():
            raise SessionError("Run login in a terminal to display the QR code.")
        executable = shutil.which("qrencode") or shutil.which("qrencode", path="/opt/homebrew/bin:/usr/local/bin")
        if executable is None:
            raise SessionError("Install qrencode for QR sign-in or run login --phone.")
        try:
            qr = subprocess.run(
                [executable, "-t", "ANSI256UTF8", "-l", "M", "-m", "4", "-o", "-"],
                input=link,
                encoding="utf-8",
                capture_output=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            raise SessionError("Could not render the QR code. Check qrencode or run login --phone.") from None

        frame = "Sign in to Telegram. The QR code refreshes automatically.\n"
        frame += "Telegram on your phone → Settings → Devices → Link Desktop Device.\n"
        frame += qr.rstrip("\n")
        rows = re.sub(r"\x1b\[[0-9;]*m", "", frame).splitlines()
        width, height = max(map(len, rows)), len(rows)
        terminal = os.get_terminal_size(sys.stdout.fileno())
        if terminal.columns < width or terminal.lines < height:
            raise SessionError(f"Resize the terminal to {width}×{height} and run login again.")

        enter_alternate_screen = "\x1b[?1049h" if self.last_link is None else ""
        print(f"{enter_alternate_screen}\x1b[H\x1b[2J{frame}", end="", flush=True)
        self.last_link = link

    def clear(self):
        if self.last_link is not None:
            print("\x1b[?1049l", end="", flush=True)
            self.last_link = None


def load_library():
    configured = os.environ.get("TDLIB_LIBRARY")
    candidates = (configured,) if configured else (
        *(f"{prefix}/opt/tdlib/lib/libtdjson.dylib" for prefix in ("/opt/homebrew", "/usr/local")),
        ctypes.util.find_library("tdjson"),
        "libtdjson.so" if sys.platform.startswith("linux") else None,
    )
    for path in filter(None, candidates):
        try:
            library = ctypes.CDLL(path)
            library.td_create_client_id
            return library
        except (OSError, AttributeError):
            continue
    raise SessionError("Could not load TDLib. Install its JSON library or set TDLIB_LIBRARY to the full library path.")


def validate_account_alias(account):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", account):
        raise SessionError("Use an account alias such as primary or work.")


def account_directory(account, root=ROOT):
    validate_account_alias(account)
    return root if account == "primary" else root / "accounts" / account


def credential_key(directory):
    # The account directory includes the profile alias and the resolved data directory.
    digest = hashlib.sha256(os.fsencode(directory.resolve())).hexdigest()
    return f"{APP}:{digest}"


def save_credentials(directory, config):
    try:
        keyring.set_password(SERVICE, credential_key(directory), json.dumps(config))
    except KeyringError:
        raise SessionError("Could not write to the system keyring. Unlock it and run login again.") from None


def parse_credentials(saved):
    try:
        config = json.loads(saved)
        api_id = config["api_id"]
        api_hash = config["api_hash"]
        encryption_key = base64.b64decode(config["encryption_key"], validate=True)
        if (
            type(api_id) is int and 0 < api_id <= 2_147_483_647
            and re.fullmatch(r"[0-9a-fA-F]{32}", api_hash) and len(encryption_key) == 32
        ):
            return config
    except (KeyError, TypeError, ValueError):
        pass
    raise SessionError("The stored Telegram credentials are invalid. Check the system keyring.")


def credentials(account, directory, interactive=False, *, legacy=False):
    database_exists = (directory / "session-db").is_dir()
    try:
        saved = keyring.get_password(SERVICE, credential_key(directory))
        if not saved and legacy and database_exists:
            legacy_key = APP if account == "primary" else f"{APP}.{account}"
            saved = keyring.get_password(SERVICE, legacy_key)
    except KeyringError:
        raise SessionError("Could not open the system keyring. Unlock it and try again.") from None
    if saved:
        return parse_credentials(saved)
    if database_exists:
        raise SessionError(
            "No keyring entry for this session. Use the original data directory or run "
            "login --import-legacy-keyring for an existing legacy installation."
        )
    if not interactive:
        raise SessionError("Run telegram-mcp login in a terminal first.")
    if not sys.stdin.isatty():
        raise SessionError("Run login in a terminal so credential input can be hidden.")

    print("Create an application at https://my.telegram.org/apps to get its api_id and api_hash.")
    value = input("Telegram api_id: ").strip()
    if not re.fullmatch(r"[0-9]{1,10}", value) or not 0 < int(value) <= 2_147_483_647:
        raise SessionError("api_id must be a positive 32-bit integer.")
    api_hash = getpass.getpass("Telegram api_hash: ").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise SessionError("api_hash must contain 32 hexadecimal characters.")

    config = {
        "api_id": int(value),
        "api_hash": api_hash,
        "encryption_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    }
    save_credentials(directory, config)
    return config


class TDLib:
    def __init__(self, send_chat_id=None):
        self.send_chat_id = send_chat_id
        self.library = load_library()
        self.library.td_create_client_id.restype = ctypes.c_int
        self.library.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
        self.library.td_send.restype = None
        self.library.td_receive.argtypes = [ctypes.c_double]
        self.library.td_receive.restype = ctypes.c_char_p
        self.library.td_execute.argtypes = [ctypes.c_char_p]
        self.library.td_execute.restype = ctypes.c_char_p

        self.library.td_execute(b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}')
        self.legacy = None
        self.client_id = self.library.td_create_client_id()
        self.sequence = 0
        self.authorization = None
        self.send_events = {}

    def receive(self, deadline):
        while (remaining := deadline - time.monotonic()) > 0:
            response = self.library.td_receive(min(1, remaining))
            if response is None:
                continue
            event = json.loads(response)
            if event.get("@client_id") != self.client_id:
                continue
            if event.get("@type") == "updateOption" and event.get("name") == "version":
                self.legacy = event["value"].get("value") == "1.8.0"
            if event.get("@type") == "updateAuthorizationState":
                self.authorization = event["authorization_state"]
            if event.get("@type") in ("updateMessageSendSucceeded", "updateMessageSendFailed"):
                if event.get("message", {}).get("chat_id") == self.send_chat_id:
                    self.send_events[event["old_message_id"]] = event
            return event
        raise SessionError("TDLib did not respond in time.")

    def call(self, method, *, deadline=None, **arguments):
        if deadline is None:
            deadline = time.monotonic() + TD_TIMEOUT
        elif time.monotonic() >= deadline:
            raise SessionError("TDLib timed out.")
        self.sequence += 1
        request_id = self.sequence
        request = {"@type": method, "@extra": request_id, **arguments}
        self.library.td_send(self.client_id, json.dumps(request).encode())
        while True:
            response = self.receive(deadline)
            if response.get("@extra") != request_id:
                continue
            if response.get("@type") == "error":
                raise RequestError(method, response)
            return response

    def authorize(self, config, login_method, database_directory):
        with TerminalQr() as qr:
            self._authorize(config, login_method, database_directory, qr)

    def _authorize(self, config, login_method, database_directory, qr):
        state = self.call("getAuthorizationState")
        if self.authorization is None:
            self.authorization = state
        deadline = time.monotonic() + (540 if login_method else TD_TIMEOUT)
        while True:
            while not self.authorization:
                self.receive(deadline)
            state = self.authorization
            step = state["@type"]

            if step == "authorizationStateReady":
                return
            if self.is_closing:
                raise SessionError("Telegram is closing the local session.")
            self.authorization = None
            if step == "authorizationStateWaitTdlibParameters":
                if self.legacy is None:
                    raise SessionError("Could not determine the TDLib version.")
                parameters = {
                    "database_directory": str(database_directory),
                    "use_file_database": False,
                    "use_chat_info_database": False,
                    "use_message_database": False,
                    "use_secret_chats": False,
                    "api_id": config["api_id"],
                    "api_hash": config["api_hash"],
                    "system_language_code": "en",
                    "device_model": "Telegram MCP",
                    "application_version": "1.0",
                }
                if self.legacy:
                    self.call("setTdlibParameters", parameters={"@type": "tdlibParameters", **parameters})
                else:
                    self.call("setTdlibParameters", database_encryption_key=config["encryption_key"], **parameters)
                continue
            if step == "authorizationStateWaitEncryptionKey":
                self.call("checkDatabaseEncryptionKey", encryption_key=config["encryption_key"])
                continue

            if not login_method:
                raise SessionError("Run telegram-mcp login in a terminal to authorize this profile.")
            if step == "authorizationStateWaitPhoneNumber":
                if login_method == "phone":
                    number = getpass.getpass("Telegram phone number (including + and country code): ").strip()
                    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
                        raise SessionError("Enter the number with + and a country code, without spaces or hyphens.")
                    self.call("setAuthenticationPhoneNumber", phone_number=number, settings=None)
                else:
                    self.call("requestQrCodeAuthentication", other_user_ids=[])
            elif step == "authorizationStateWaitCode":
                code = getpass.getpass("Telegram login code: ").strip()
                self.call("checkAuthenticationCode", code=code)
            elif step == "authorizationStateWaitEmailAddress":
                qr.clear()
                email_address = getpass.getpass("Telegram login email: ").strip()
                self.call("setAuthenticationEmailAddress", email_address=email_address)
            elif step == "authorizationStateWaitEmailCode":
                qr.clear()
                code = getpass.getpass("Telegram email login code: ").strip()
                self.call("checkAuthenticationEmailCode", code={"@type": "emailAddressAuthenticationCode", "code": code})
            elif step == "authorizationStateWaitOtherDeviceConfirmation":
                if login_method == "phone":
                    raise SessionError("This login attempt is still waiting for a QR scan.")
                qr.show(state["link"])
            elif step == "authorizationStateWaitPassword":
                qr.clear()
                self.check_password()
            else:
                raise SessionError(f"This login step is unsupported ({step}). Try QR sign-in or update TDLib.")

    def check_password(self):
        while True:
            password = getpass.getpass("Telegram two-step verification password: ")
            try:
                self.call("checkAuthenticationPassword", password=password)
            except RequestError as exc:
                if exc.code != 400 or exc.detail != "PASSWORD_HASH_INVALID":
                    raise
                print("Telegram rejected the password. Try again or press Ctrl+C to cancel.", flush=True)
            else:
                return

    def confirm_send(self, pending):
        if pending.get("chat_id") != self.send_chat_id:
            raise SessionError("Telegram returned an unexpected chat when sending.")
        sending_state = pending.get("sending_state")
        if not sending_state:
            return pending
        if sending_state.get("@type") == "messageSendingStateFailed":
            raise SendFailed("Telegram reported that the message was not sent.")
        if sending_state.get("@type") != "messageSendingStatePending":
            raise SessionError("TDLib returned an unknown send state.")

        pending_id = pending["id"]
        deadline = time.monotonic() + TD_TIMEOUT
        try:
            while pending_id not in self.send_events:
                self.receive(deadline)
        except SessionError as exc:
            raise SessionError("The send is unconfirmed. Check the chat for the message.") from exc
        event = self.send_events.pop(pending_id)
        if event["@type"] != "updateMessageSendSucceeded":
            raise SendFailed("Telegram reported that the message was not sent.")
        return event["message"]

    def confirm_active_session(self):
        # TDLib can restore Ready from disk before Telegram rejects a revoked session.
        sessions = self.call("getActiveSessions").get("sessions", [])
        if self.is_closing or not any(session.get("is_current") is True for session in sessions):
            raise SessionError("Telegram did not confirm an active session.")

    @property
    def is_closing(self):
        return (self.authorization or {}).get("@type") in {
            "authorizationStateLoggingOut", "authorizationStateClosing", "authorizationStateClosed",
        }

    def wait_closed(self, deadline):
        while (self.authorization or {}).get("@type") != "authorizationStateClosed":
            self.receive(deadline)

    def close(self):
        deadline = time.monotonic() + TD_CLOSE_TIMEOUT
        if not self.is_closing:
            self.call("close", deadline=deadline)
        self.wait_closed(deadline)


class LocalSession:
    def __init__(
        self, login_method=None, send_chat_id=None, account="primary", expected_user_id=None,
        *, root=ROOT, legacy_credentials=False,
    ):
        self.login_method = login_method
        self.send_chat_id = send_chat_id
        self.account = account
        self.directory = account_directory(account, root)
        self.expected_user_id = expected_user_id
        self.legacy_credentials = legacy_credentials
        self.config = None
        self.lock = None
        self.client = None

    def __enter__(self):
        if not _CLIENT_LOCK.acquire(blocking=False):
            raise SessionError("TDLib is busy. Retry after the current operation finishes.")
        try:
            if _UNCLOSED_LOCKS:
                raise SessionError("TDLib did not close its previous session. Restart the MCP server.")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.directory / "session.lock", os.O_CREAT | os.O_RDWR, 0o600)
            self.lock = os.fdopen(descriptor, "a")
            try:
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise
                raise SessionError("Another process is using this Telegram session. Retry when it finishes.") from exc

            self.config = credentials(
                self.account, self.directory, interactive=bool(self.login_method), legacy=self.legacy_credentials,
            )
            return self.open_client(self.config)
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def open_client(self, config):
        database = self.directory / "session-db"
        for retry in (False, True):
            database.mkdir(mode=0o700, exist_ok=True)
            self.client = TDLib(self.send_chat_id)
            try:
                self.client.authorize(config, self.login_method, database)
                if self.expected_user_id is not None and self.client.call("getMe").get("id") != self.expected_user_id:
                    raise SessionError("The signed-in Telegram account does not match the profile.")
                if self.login_method:
                    self.client.confirm_active_session()
            except SessionError as error:
                rejected_login = isinstance(error, RequestError) and error.method == "getActiveSessions" and error.code == 401
                if not self.login_method or not (self.client.is_closing or rejected_login):
                    raise
                self.client.wait_closed(time.monotonic() + TD_TIMEOUT)
                if retry:
                    raise SessionError("Telegram closed the new session. Run login again.") from error
                print("Telegram closed the saved session. Starting sign-in again.", flush=True)
            else:
                return self.client

    def __exit__(self, exception_type, *_):
        try:
            if self.client:
                self.client.close()
        except BaseException as exc:
            # The library may still be holding the database open in this process.
            _UNCLOSED_LOCKS.append(self.lock)
            LOGGER.warning("TDLib did not close (%s). Restart the MCP server.", type(exc).__name__)
            if exception_type is None and not isinstance(exc, Exception):
                raise
        else:
            if self.lock:
                self.lock.close()
        finally:
            _CLIENT_LOCK.release()
