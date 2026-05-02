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
    include_photos: bool
    photo_limit: int
    last_sent_date: str
    last_message_id: Optional[int]
    last_message_hash: str
    last_refresh_ts: float


@dataclass(frozen=True)
class FlightWatch:
    chat_id: Any
    airport: str
    direction: str
    flight_no: str
    last_hash: str
    last_check_ts: float


class SubscriptionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._data = self._load()

    def subscribe(
        self,
        chat_id: Any,
        push_time: str,
        airports: list[str],
        include_photos: bool = True,
        photo_limit: int = 3,
    ) -> Subscription:
        existing = self._data["chats"].get(str(chat_id), {})
        subscription = {
            "chat_id": chat_id,
            "push_time": push_time,
            "airports": airports,
            "include_photos": include_photos,
            "photo_limit": photo_limit,
            "last_sent_date": existing.get("last_sent_date", ""),
            "last_message_id": existing.get("last_message_id"),
            "last_message_hash": existing.get("last_message_hash", ""),
            "last_refresh_ts": existing.get("last_refresh_ts", 0),
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

    def ensure(self, chat_id: Any, default_push_time: str = "08:30") -> Subscription:
        subscription = self.get(chat_id)
        if subscription:
            return subscription
        return self.subscribe(chat_id, default_push_time, ["PVG", "SHA"])

    def update_push_time(self, chat_id: Any, push_time: str) -> Optional[Subscription]:
        raw = self._data["chats"].get(str(chat_id))
        if not raw:
            return None
        raw["push_time"] = push_time
        raw["last_sent_date"] = ""
        self._save()
        return self._to_subscription(raw)

    def update_airports(self, chat_id: Any, airports: list[str]) -> Optional[Subscription]:
        raw = self._data["chats"].get(str(chat_id))
        if not raw:
            return None
        raw["airports"] = airports
        raw["last_sent_date"] = ""
        self._save()
        return self._to_subscription(raw)

    def update_include_photos(self, chat_id: Any, include_photos: bool) -> Optional[Subscription]:
        raw = self._data["chats"].get(str(chat_id))
        if not raw:
            return None
        raw["include_photos"] = include_photos
        raw["last_sent_date"] = ""
        self._save()
        return self._to_subscription(raw)

    def update_photo_limit(self, chat_id: Any, photo_limit: int) -> Optional[Subscription]:
        raw = self._data["chats"].get(str(chat_id))
        if not raw:
            return None
        raw["photo_limit"] = min(max(photo_limit, 1), 6)
        raw["last_sent_date"] = ""
        self._save()
        return self._to_subscription(raw)

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

    def add_watch(self, chat_id: Any, airport: str, direction: str, flight_no: str, last_hash: str) -> FlightWatch:
        watches = self._data.setdefault("watches", {})
        key = watch_key(chat_id, airport, direction, flight_no)
        raw = {
            "chat_id": chat_id,
            "airport": airport,
            "direction": direction,
            "flight_no": flight_no,
            "last_hash": last_hash,
            "last_check_ts": 0,
        }
        watches[key] = raw
        self._save()
        return self._to_watch(raw)

    def remove_watch(self, chat_id: Any, airport: str, direction: str, flight_no: str) -> bool:
        removed = self._data.setdefault("watches", {}).pop(watch_key(chat_id, airport, direction, flight_no), None)
        self._save()
        return removed is not None

    def list_watches(self, chat_id: Any) -> list[FlightWatch]:
        result = []
        for raw in self._data.setdefault("watches", {}).values():
            if str(raw.get("chat_id")) == str(chat_id):
                result.append(self._to_watch(raw))
        return result

    def watches_due(self, now: datetime, refresh_seconds: int) -> list[FlightWatch]:
        if refresh_seconds <= 0:
            return []
        now_ts = now.timestamp()
        result = []
        for raw in self._data.setdefault("watches", {}).values():
            if now_ts - float(raw.get("last_check_ts") or 0) >= refresh_seconds:
                result.append(self._to_watch(raw))
        return result

    def mark_watch_checked(self, watch: FlightWatch, last_hash: str) -> None:
        raw = self._data.setdefault("watches", {}).get(
            watch_key(watch.chat_id, watch.airport, watch.direction, watch.flight_no)
        )
        if not raw:
            return
        raw["last_hash"] = last_hash
        raw["last_check_ts"] = datetime.now().timestamp()
        self._save()

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"chats": {}, "watches": {}}
        with open(self.path, "r", encoding="utf-8") as store_file:
            data = json.load(store_file)
        if not isinstance(data, dict):
            return {"chats": {}, "watches": {}}
        chats = data.get("chats")
        watches = data.get("watches")
        return {
            "chats": chats if isinstance(chats, dict) else {},
            "watches": watches if isinstance(watches, dict) else {},
        }

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
            include_photos=stored_bool(raw.get("include_photos", True)),
            photo_limit=min(max(int(raw.get("photo_limit", 3)), 1), 6),
            last_sent_date=raw.get("last_sent_date", ""),
            last_message_id=raw.get("last_message_id"),
            last_message_hash=raw.get("last_message_hash", ""),
            last_refresh_ts=float(raw.get("last_refresh_ts") or 0),
        )

    def _to_watch(self, raw: dict[str, Any]) -> FlightWatch:
        return FlightWatch(
            chat_id=raw.get("chat_id"),
            airport=raw.get("airport", "PVG"),
            direction=raw.get("direction", "departures"),
            flight_no=raw.get("flight_no", "-"),
            last_hash=raw.get("last_hash", ""),
            last_check_ts=float(raw.get("last_check_ts") or 0),
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


def watch_key(chat_id: Any, airport: str, direction: str, flight_no: str) -> str:
    return f"{chat_id}:{airport}:{direction}:{flight_no}"


def stored_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)
