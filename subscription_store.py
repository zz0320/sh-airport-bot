from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class Subscription:
    chat_id: Any
    push_time: str
    airports: list[str]
    last_sent_date: str
    last_message_id: Optional[int]
    last_message_hash: str
    last_refresh_ts: float


class SubscriptionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._data = self._load()

    def subscribe(self, chat_id: Any, push_time: str, airports: list[str]) -> Subscription:
        subscription = {
            "chat_id": chat_id,
            "push_time": push_time,
            "airports": airports,
            "last_sent_date": "",
        }
        self._data["chats"][str(chat_id)] = subscription
        self._save()
        return self._to_subscription(subscription)

    def unsubscribe(self, chat_id: Any) -> bool:
        removed = self._data["chats"].pop(str(chat_id), None)
        self._save()
        return removed is not None

    def get(self, chat_id: Any) -> Optional[Subscription]:
        raw = self._data["chats"].get(str(chat_id))
        return self._to_subscription(raw) if raw else None

    def due(self, now: datetime) -> list[Subscription]:
        today = now.date().isoformat()
        current_minutes = now.hour * 60 + now.minute
        due_subscriptions = []
        for raw in self._data["chats"].values():
            push_minutes = parse_hhmm(raw.get("push_time", "08:30"))
            if raw.get("last_sent_date") != today and current_minutes >= push_minutes:
                due_subscriptions.append(self._to_subscription(raw))
        return due_subscriptions

    def refresh_due(self, now: datetime, refresh_seconds: int) -> list[Subscription]:
        today = now.date().isoformat()
        now_ts = now.timestamp()
        due_subscriptions = []
        for raw in self._data["chats"].values():
            if raw.get("last_sent_date") != today:
                continue
            if not raw.get("last_message_id"):
                continue
            if now_ts - float(raw.get("last_refresh_ts") or 0) >= refresh_seconds:
                due_subscriptions.append(self._to_subscription(raw))
        return due_subscriptions

    def mark_sent(self, chat_id: Any, sent_date: str, message_id: Optional[int], message_hash: str) -> None:
        raw = self._data["chats"].get(str(chat_id))
        if not raw:
            return
        raw["last_sent_date"] = sent_date
        raw["last_message_id"] = message_id
        raw["last_message_hash"] = message_hash
        raw["last_refresh_ts"] = datetime.now().timestamp()
        self._save()

    def mark_refreshed(self, chat_id: Any, message_hash: str) -> None:
        raw = self._data["chats"].get(str(chat_id))
        if not raw:
            return
        raw["last_message_hash"] = message_hash
        raw["last_refresh_ts"] = datetime.now().timestamp()
        self._save()

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"chats": {}}
        with open(self.path, "r", encoding="utf-8") as store_file:
            data = json.load(store_file)
        if not isinstance(data, dict):
            return {"chats": {}}
        chats = data.get("chats")
        return {"chats": chats if isinstance(chats, dict) else {}}

    def _save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as store_file:
            json.dump(self._data, store_file, ensure_ascii=False, indent=2)

    def _to_subscription(self, raw: dict[str, Any]) -> Subscription:
        airports = raw.get("airports") or ["PVG", "SHA"]
        return Subscription(
            chat_id=raw.get("chat_id"),
            push_time=raw.get("push_time", "08:30"),
            airports=[airport for airport in airports if airport in {"PVG", "SHA"}] or ["PVG", "SHA"],
            last_sent_date=raw.get("last_sent_date", ""),
            last_message_id=raw.get("last_message_id"),
            last_message_hash=raw.get("last_message_hash", ""),
            last_refresh_ts=float(raw.get("last_refresh_ts") or 0),
        )


def parse_hhmm(value: str) -> int:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = min(max(int(hour_text), 0), 23)
        minute = min(max(int(minute_text), 0), 59)
        return hour * 60 + minute
    except Exception:
        return 8 * 60 + 30


def normalize_hhmm(value: str, fallback: str) -> str:
    if not value:
        return fallback
    minutes = parse_hhmm(value)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
