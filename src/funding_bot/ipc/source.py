"""Neutral operator source facts and the reader for the existing inbox format.

The legacy reader preserves current durable request bytes/keys during migration.
It extracts facts only; it cannot declare an actor authorized or approve a plan.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorSource:
    kind: str
    update_id: int | None
    user_id: int | None = None
    chat_id: int | None = None
    text: str | None = None
    date: int | None = None
    callback_id: str | None = None
    message_id: int | None = None
    private: bool = False
    has_conversation: bool = False
    actor_is_bot: bool = False
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1 or self.kind not in ('message', 'action', 'ignore'):
            raise ValueError('unsupported operator source')
        for name in ('update_id', 'user_id', 'chat_id', 'date', 'message_id'):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise ValueError('operator source IDs/timestamps must be integers')
        for name in ('text', 'callback_id'):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError('operator source text/reference must be strings')
        if any(type(getattr(self, name)) is not bool for name in ('private','has_conversation','actor_is_bot')):
            raise ValueError('operator source flags must be boolean')
        if not self.has_conversation and (self.chat_id is not None or self.private):
            raise ValueError('absent conversation cannot contain identity or private flag')


def _int(value):
    return value if type(value) is int else None


def legacy_telegram_source(update) -> OperatorSource:
    if not isinstance(update, dict):
        return OperatorSource('ignore', None)
    uid = _int(update.get('update_id'))
    cq = update.get('callback_query')
    if isinstance(cq, dict):
        msg = cq.get('message') if isinstance(cq.get('message'), dict) else {}
        frm, chat = cq.get('from') or {}, msg.get('chat') or {}
        if not isinstance(frm, dict) or not isinstance(chat, dict):
            return OperatorSource('ignore', uid)
        return OperatorSource('action', uid, _int(frm.get('id')), _int(chat.get('id')),
                              cq.get('data') if isinstance(cq.get('data'), str) else None,
                              callback_id=str(cq['id']) if cq.get('id') is not None else None,
                              message_id=_int(msg.get('message_id')), private=chat.get('type') == 'private',
                              has_conversation=bool(chat), actor_is_bot=bool(frm.get('is_bot')))
    msg = update.get('message')
    if not isinstance(msg, dict):
        return OperatorSource('ignore', uid)
    frm, chat = msg.get('from') or {}, msg.get('chat') or {}
    if not isinstance(frm, dict) or not isinstance(chat, dict):
        return OperatorSource('ignore', uid)
    return OperatorSource('message', uid, _int(frm.get('id')), _int(chat.get('id')),
                          msg.get('text') if isinstance(msg.get('text'), str) else None,
                          _int(msg.get('date')), private=chat.get('type') == 'private',
                          has_conversation=bool(chat), actor_is_bot=bool(frm.get('is_bot')))
