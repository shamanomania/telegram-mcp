"""Local configuration is the only source of Telegram account and chat permissions."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from .tdlib_session import ROOT, SessionError, validate_account_alias

PROFILE_FILE = "telegram-profiles.json"
ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,39}")
USERNAME = re.compile(r"[A-Za-z0-9_]{1,32}")
ACCESS = {"read", "send", "read_write"}
POST_ID = r"[1-9][0-9]{0,9}"
MESSAGE_LINK_OPTION = re.compile(rf"(?:single(?:=1)?|thread={POST_ID}|t=[0-9hms:]{{1,20}})")


@dataclass(frozen=True)
class ChatProfile:
    chat_id: int
    title: str
    access: str
    username: str | None = None

    def permits(self, action):
        return self.access == action or self.access == "read_write"

    def normalize_message_link(self, url):
        path, separator, query = url.partition("?")
        options = query.split("&") if separator else []
        if not all(MESSAGE_LINK_OPTION.fullmatch(option) for option in options):
            raise SessionError("The message link contains unsupported parameters.")
        candidates = []
        if self.chat_id < -10**12:
            candidates.append(f"c/{-self.chat_id - 10**12}")
        if self.username:
            candidates.append(self.username)
        for prefix in candidates:
            match = re.fullmatch(
                rf"https://t\.me/{re.escape(prefix)}/(?:(?P<thread>{POST_ID})/)?(?P<post>{POST_ID})",
                path,
                re.IGNORECASE | re.ASCII,
            )
            if match:
                thread = match["thread"]
                if thread:
                    thread_option = f"thread={thread}"
                    if any(option.startswith("thread=") and option != thread_option for option in options):
                        raise SessionError("The message link contains conflicting topic IDs.")
                    if thread_option not in options:
                        options.append(thread_option)
                normalized = f"https://t.me/{prefix}/{match['post']}"
                return normalized + ("?" + "&".join(options) if options else "")
        raise SessionError("The link must point to a message in the selected chat.")


@dataclass(frozen=True)
class AccountProfile:
    chats: dict[str, ChatProfile]
    user_id: int | None = None


def read_profile_document(root: Path):
    path = root / PROFILE_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document["accounts"], dict):
            raise TypeError("accounts")
    except FileNotFoundError:
        raise SessionError("Run telegram-mcp setup to create your local profile.") from None
    except json.JSONDecodeError as error:
        raise SessionError(f"Invalid JSON in {PROFILE_FILE} at line {error.lineno}, column {error.colno}.") from None
    except (OSError, KeyError, TypeError, UnicodeError):
        raise SessionError(f"Could not read the accounts object in {PROFILE_FILE}.") from None
    return document


def parse_profiles(document):
    return {alias: parse_account(alias, config) for alias, config in document["accounts"].items()}


def load_profiles(root: Path = ROOT):
    return parse_profiles(read_profile_document(root))


@contextmanager
def edit_profiles(root: Path, *, create=False):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root / ".profiles.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / PROFILE_FILE
        if create and not path.exists():
            document = {"accounts": {}}
        else:
            document = read_profile_document(root)
        parse_profiles(document)
        yield document["accounts"]
        parse_profiles(document)

        with tempfile.NamedTemporaryFile(dir=root, mode="w", encoding="utf-8", delete=False) as draft:
            temporary = Path(draft.name)
            try:
                json.dump(document, draft, ensure_ascii=False, indent=2)
                draft.write("\n")
                draft.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def parse_account(alias, config):
    validate_account_alias(alias)
    if not isinstance(config, dict) or not isinstance(config.get("chats"), dict):
        raise SessionError(f"Add a chats object to profile {alias}.")
    user_id = config.get("user_id")
    if user_id is not None and (type(user_id) is not int or not 0 < user_id < 2**53):
        raise SessionError(f"Invalid user_id in profile {alias}. Run login to bind a new profile.")
    chats = {name: parse_chat(name, value) for name, value in config["chats"].items()}
    if len({chat.chat_id for chat in chats.values()}) != len(chats):
        raise SessionError(f"The same chat ID is listed twice in profile {alias}.")
    return AccountProfile(chats, user_id)


def parse_chat(alias, config):
    if not ALIAS.fullmatch(alias) or not isinstance(config, dict):
        raise SessionError("Use a chat alias such as my-chat and give it a configuration object.")
    chat_id = config.get("chat_id")
    title = config.get("title")
    access = config.get("access")
    username = config.get("username")
    if type(chat_id) is not int or chat_id == 0 or not -(2**53) < chat_id < 2**53:
        raise SessionError(f"Invalid chat_id for {alias}.")
    if not isinstance(title, str) or not title.strip() or not isinstance(access, str) or access not in ACCESS:
        raise SessionError(f"Provide a title and valid access level for {alias}.")
    if username is not None and (not isinstance(username, str) or not USERNAME.fullmatch(username)):
        raise SessionError(f"Invalid public username for {alias}.")
    return ChatProfile(chat_id, title, access, username)


def get_account(account, root: Path = ROOT):
    profile = load_profiles(root).get(account)
    if profile is None:
        raise SessionError("Account profile missing. Run setup or choose an account from list_telegram_chats.")
    return profile


def get_chat(account, chat, action, root: Path = ROOT):
    profile = get_account(account, root)
    target = profile.chats.get(chat)
    if target is None or not target.permits(action):
        raise SessionError("This operation is not allowed for the selected chat.")
    return profile, target
