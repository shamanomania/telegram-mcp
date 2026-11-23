"""Persist the outcome of an outgoing request without storing its message text."""

import hashlib
import json
import sqlite3

from .tdlib_session import SessionError


def operation_keys(account_id, chat_id, text, request_id, entities=None):
    scope = (account_id, chat_id)
    content = (*scope, text)
    if entities is not None:
        content = (*content, entities)
    payload = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    if request_id is None:
        return f"message:{payload}", payload
    supplied = hashlib.sha256(json.dumps((*scope, request_id)).encode()).hexdigest()
    return f"request:{supplied}", payload


class SendJournal:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        try:
            with self.connection:
                self.connection.execute(
                    """CREATE TABLE IF NOT EXISTS outgoing (
                        operation TEXT PRIMARY KEY NOT NULL,
                        payload TEXT NOT NULL,
                        message_id INTEGER
                    )"""
                )
        except BaseException:
            self.close()
            raise

    def close(self):
        self.connection.close()

    def receipt(self, operation, payload):
        previous = self.connection.execute(
            "SELECT payload, message_id FROM outgoing WHERE operation = ?",
            (operation,),
        ).fetchone()
        if previous is None:
            return None
        previous_payload, message_id = previous
        if previous_payload != payload:
            raise SessionError("This request_id already refers to a different message.")
        if message_id is None:
            raise SessionError(
                "The previous send was not confirmed. Check the chat for the message. "
                "Retry with this request_id is blocked."
            )
        return message_id

    def begin(self, operation, payload):
        with self.connection:
            claim = self.connection.execute(
                "INSERT OR IGNORE INTO outgoing (operation, payload) VALUES (?, ?)",
                (operation, payload),
            )
            if claim.rowcount:
                return None
            return self.receipt(operation, payload)

    def confirm(self, operation, message_id):
        with self.connection:
            self.connection.execute(
                "UPDATE outgoing SET message_id = ? WHERE operation = ?",
                (message_id, operation),
            )

    def reject(self, operation):
        with self.connection:
            self.connection.execute(
                "DELETE FROM outgoing WHERE operation = ? AND message_id IS NULL",
                (operation,),
            )
