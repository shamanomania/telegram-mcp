"""Local configuration is the only source of Telegram account and chat permissions."""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from tdlib_session import ROOT, SessionError, validate_account_alias

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
            raise SessionError("Ссылка содержит неподдерживаемые параметры сообщения.")
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
                        raise SessionError("В ссылке указаны разные ID темы.")
                    if thread_option not in options:
                        options.append(thread_option)
                normalized = f"https://t.me/{prefix}/{match['post']}"
                return normalized + ("?" + "&".join(options) if options else "")
        raise SessionError("Ссылка должна вести на сообщение в выбранном чате.")


@dataclass(frozen=True)
class AccountProfile:
    chats: dict[str, ChatProfile]
    user_id: int | None = None


def load_profiles(root: Path = ROOT):
    path = root / "telegram-profiles.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document["accounts"]
        if not isinstance(entries, dict):
            raise TypeError("accounts")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SessionError("Проверьте раздел accounts в локальном telegram-profiles.json.") from exc

    return {alias: parse_account(alias, config) for alias, config in entries.items()}


def parse_account(alias, config):
    validate_account_alias(alias)
    if not isinstance(config, dict) or not isinstance(config.get("chats"), dict):
        raise SessionError(f"Укажите chats в профиле {alias}.")
    user_id = config.get("user_id")
    if user_id is not None and (type(user_id) is not int or not 0 < user_id < 2**53):
        raise SessionError(f"Проверьте user_id в профиле {alias}.")
    chats = {name: parse_chat(name, value) for name, value in config["chats"].items()}
    if len({chat.chat_id for chat in chats.values()}) != len(chats):
        raise SessionError(f"ID чата повторяется в профиле {alias}.")
    return AccountProfile(chats, user_id)


def parse_chat(alias, config):
    if not ALIAS.fullmatch(alias) or not isinstance(config, dict):
        raise SessionError("Проверьте псевдоним и поля чата в профиле.")
    chat_id = config.get("chat_id")
    title = config.get("title")
    access = config.get("access")
    username = config.get("username")
    if type(chat_id) is not int or chat_id == 0 or not -(2**53) < chat_id < 2**53:
        raise SessionError(f"Проверьте chat_id для {alias}.")
    if not isinstance(title, str) or not title.strip() or not isinstance(access, str) or access not in ACCESS:
        raise SessionError(f"Укажите title и access для {alias}.")
    if username is not None and (not isinstance(username, str) or not USERNAME.fullmatch(username)):
        raise SessionError(f"Проверьте username для {alias}.")
    return ChatProfile(chat_id, title, access, username)


def get_account(account, root: Path = ROOT):
    profile = load_profiles(root).get(account)
    if profile is None:
        raise SessionError("Аккаунт отсутствует в локальной конфигурации MCP.")
    return profile


def get_chat(account, chat, action, root: Path = ROOT):
    profile = get_account(account, root)
    target = profile.chats.get(chat)
    if target is None or not target.permits(action):
        raise SessionError("Операция не разрешена для этого чата.")
    return profile, target
