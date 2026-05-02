from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FlightChange:
    key: str
    flight_no: str
    airport: str
    direction: str
    changes: list[str]


class FlightMemory:
    def __init__(self, path: str) -> None:
        self.path = path
        self._data = self._load()

    def seen_registration(self, registration: str) -> bool:
        if not registration or registration == "-":
            return True
        return registration in self._data.setdefault("seen_registrations", {})

    def compare(self, airport: str, direction: str, flights: list[Any]) -> list[FlightChange]:
        results = []
        snapshots = self._data.setdefault("snapshots", {})
        for flight in flights:
            key = snapshot_key(airport, direction, flight.flight_no)
            previous = snapshots.get(key)
            current = make_snapshot(airport, direction, flight)
            if previous:
                changes = compare_snapshot(previous, current)
                if changes:
                    results.append(FlightChange(key, flight.flight_no, airport, direction, changes))
        return results

    def update(self, airport: str, direction: str, flights: list[Any]) -> None:
        snapshots = self._data.setdefault("snapshots", {})
        seen = self._data.setdefault("seen_registrations", {})
        now = datetime.now().isoformat(timespec="seconds")
        for flight in flights:
            snapshots[snapshot_key(airport, direction, flight.flight_no)] = make_snapshot(airport, direction, flight)
            if flight.aircraft_registration != "-":
                seen.setdefault(flight.aircraft_registration, now)
        self._save()

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"snapshots": {}, "seen_registrations": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as memory_file:
                data = json.load(memory_file)
        except Exception:
            return {"snapshots": {}, "seen_registrations": {}}
        if not isinstance(data, dict):
            return {"snapshots": {}, "seen_registrations": {}}
        return {
            "snapshots": data.get("snapshots") if isinstance(data.get("snapshots"), dict) else {},
            "seen_registrations": data.get("seen_registrations")
            if isinstance(data.get("seen_registrations"), dict)
            else {},
        }

    def _save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as memory_file:
            json.dump(self._data, memory_file, ensure_ascii=False, indent=2)


def snapshot_key(airport: str, direction: str, flight_no: str) -> str:
    return f"{airport}:{direction}:{flight_no}"


def make_snapshot(airport: str, direction: str, flight: Any) -> dict[str, str]:
    return {
        "airport": airport,
        "direction": direction,
        "flight_no": flight.flight_no,
        "airline": flight.airline,
        "status": flight.status,
        "aircraft_registration": flight.aircraft_registration,
        "aircraft_type": flight.aircraft_type,
        "departure_scheduled": flight.departure_scheduled,
        "departure_estimated": flight.departure_estimated,
        "departure_actual": flight.departure_actual,
        "arrival_scheduled": flight.arrival_scheduled,
        "arrival_estimated": flight.arrival_estimated,
        "arrival_actual": flight.arrival_actual,
        "terminal": flight.terminal,
        "gate": flight.gate,
        "delay_minutes": str(flight.delay_minutes or ""),
        "departure_runway": flight.departure_runway,
        "arrival_runway": flight.arrival_runway,
    }


def compare_snapshot(previous: dict[str, str], current: dict[str, str]) -> list[str]:
    fields = [
        ("status", "状态"),
        ("departure_estimated", "预计起飞"),
        ("arrival_estimated", "预计到达"),
        ("departure_actual", "实际起飞"),
        ("arrival_actual", "实际到达"),
        ("terminal", "航站楼"),
        ("gate", "登机口"),
        ("delay_minutes", "延误"),
        ("aircraft_registration", "注册号"),
        ("aircraft_type", "机型"),
        ("departure_runway", "起飞跑道"),
        ("arrival_runway", "到达跑道"),
    ]
    changes = []
    for key, label in fields:
        before = previous.get(key, "-") or "-"
        after = current.get(key, "-") or "-"
        if before != after:
            changes.append(f"{label}：{before} -> {after}")
    return changes
