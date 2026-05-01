from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


AIRPORTS = {
    "PVG": "上海浦东",
    "SHA": "上海虹桥",
}

AIRPORT_ALIASES = {
    "pvg": "PVG",
    "浦东": "PVG",
    "浦东机场": "PVG",
    "上海浦东": "PVG",
    "上海浦东机场": "PVG",
    "sha": "SHA",
    "虹桥": "SHA",
    "虹桥机场": "SHA",
    "上海虹桥": "SHA",
    "上海虹桥机场": "SHA",
}

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Flight:
    flight_no: str
    airline: str
    aircraft_registration: str
    aircraft_type: str
    aircraft_hex: str
    aircraft_photo_url: str
    aircraft_photo_thumbnail_url: str
    aircraft_photo_source: str
    status: str
    scheduled: str
    estimated: str
    actual: str
    airport_name: str
    airport_iata: str
    terminal: str
    gate: str
    delay_minutes: Optional[int]
    departure_scheduled: str
    departure_estimated: str
    departure_actual: str
    arrival_scheduled: str
    arrival_estimated: str
    arrival_actual: str
    departure_runway: str
    arrival_runway: str


class FlightProviderError(RuntimeError):
    pass


class FlightProvider:
    def departures(self, airport_iata: str, limit: int) -> list[Flight]:
        raise NotImplementedError

    def arrivals(self, airport_iata: str, limit: int) -> list[Flight]:
        raise NotImplementedError


class AviationstackProvider(FlightProvider):
    def __init__(self, api_key: str, cache_seconds: int = 45) -> None:
        if not api_key:
            raise FlightProviderError("缺少 AVIATIONSTACK_API_KEY。")
        self.api_key = api_key
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str, int], tuple[float, list[Flight]]] = {}
        self.enrich_aircraft = os.getenv("AIRCRAFT_ENRICH_PROVIDER", "adsbdb").strip().lower() == "adsbdb"
        self._aircraft_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self.aircraft_cache_seconds = int(os.getenv("AIRCRAFT_ENRICH_CACHE_SECONDS", "86400"))

    def departures(self, airport_iata: str, limit: int) -> list[Flight]:
        return self._fetch("departures", airport_iata, limit)

    def arrivals(self, airport_iata: str, limit: int) -> list[Flight]:
        return self._fetch("arrivals", airport_iata, limit)

    def _fetch(self, direction: str, airport_iata: str, limit: int) -> list[Flight]:
        key = (direction, airport_iata, limit)
        cached = self._cache.get(key)
        now = time.time()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]

        query_key = "dep_iata" if direction == "departures" else "arr_iata"
        params = {
            "access_key": self.api_key,
            query_key: airport_iata,
            "limit": min(max(limit, 1), 20),
        }
        url = f"https://api.aviationstack.com/v1/flights?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "shanghai-flight-telegram-bot/1.0"})

        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise FlightProviderError(f"航班接口请求失败：{exc}") from exc

        if "error" in payload:
            message = payload["error"].get("message") or payload["error"].get("code") or "未知错误"
            raise FlightProviderError(f"Aviationstack 返回错误：{message}")

        flights = [
            self._parse_flight(item, direction)
            for item in payload.get("data", [])
            if isinstance(item, dict)
        ]
        flights.sort(key=lambda flight: flight.estimated or flight.scheduled or "9999")
        self._cache[key] = (now, flights[:limit])
        return flights[:limit]

    def _parse_flight(self, item: dict[str, Any], direction: str) -> Flight:
        departure = item.get("departure") or {}
        arrival = item.get("arrival") or {}
        current = departure if direction == "departures" else arrival
        other = arrival if direction == "departures" else departure
        flight = item.get("flight") or {}
        airline = item.get("airline") or {}
        aircraft = item.get("aircraft") or {}
        aircraft_registration = value_or_dash(aircraft.get("registration"))
        aircraft_type = value_or_dash(aircraft.get("iata") or aircraft.get("icao"))
        aircraft_hex = value_or_dash(aircraft.get("icao24"))
        aircraft_photo_url = "-"
        aircraft_photo_thumbnail_url = "-"
        aircraft_photo_source = "-"

        details = self._aircraft_details(aircraft_hex if aircraft_hex != "-" else aircraft_registration)
        if details:
            aircraft_registration = first_value(aircraft_registration, details.get("registration", "-"))
            aircraft_type = first_value(aircraft_type, details.get("aircraft_type", "-"))
            aircraft_photo_url = first_value(aircraft_photo_url, details.get("photo_url", "-"))
            aircraft_photo_thumbnail_url = first_value(
                aircraft_photo_thumbnail_url,
                details.get("photo_thumbnail_url", "-"),
            )
            aircraft_photo_source = first_value(aircraft_photo_source, details.get("photo_source", "-"))

        return Flight(
            flight_no=value_or_dash(flight.get("iata") or flight.get("icao") or flight.get("number")),
            airline=value_or_dash(airline.get("name")),
            aircraft_registration=aircraft_registration,
            aircraft_type=aircraft_type,
            aircraft_hex=aircraft_hex,
            aircraft_photo_url=aircraft_photo_url,
            aircraft_photo_thumbnail_url=aircraft_photo_thumbnail_url,
            aircraft_photo_source=aircraft_photo_source,
            status=translate_status(value_or_dash(item.get("flight_status"))),
            scheduled=format_time(current.get("scheduled")),
            estimated=format_time(current.get("estimated")),
            actual=format_time(current.get("actual")),
            airport_name=value_or_dash(other.get("airport")),
            airport_iata=value_or_dash(other.get("iata")),
            terminal=value_or_dash(current.get("terminal")),
            gate=value_or_dash(current.get("gate")),
            delay_minutes=current.get("delay"),
            departure_scheduled=format_time(departure.get("scheduled")),
            departure_estimated=format_time(departure.get("estimated")),
            departure_actual=format_time(departure.get("actual")),
            arrival_scheduled=format_time(arrival.get("scheduled")),
            arrival_estimated=format_time(arrival.get("estimated")),
            arrival_actual=format_time(arrival.get("actual")),
            departure_runway=format_runway(departure),
            arrival_runway=format_runway(arrival),
        )

    def _aircraft_details(self, identifier: str) -> dict[str, str]:
        if not self.enrich_aircraft or identifier == "-":
            return {}
        cached = self._aircraft_cache.get(identifier)
        now = time.time()
        if cached and now - cached[0] <= self.aircraft_cache_seconds:
            return cached[1]

        url = f"https://api.adsbdb.com/v0/aircraft/{identifier}"
        request = Request(url, headers={"User-Agent": "shanghai-flight-telegram-bot/1.0"})
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            self._aircraft_cache[identifier] = (now, {})
            return {}

        aircraft = (payload.get("response") or {}).get("aircraft") or {}
        details = {
            "registration": value_or_dash(aircraft.get("registration")),
            "aircraft_type": value_or_dash(aircraft.get("icao_type") or aircraft.get("type")),
            "photo_url": value_or_dash(aircraft.get("url_photo")),
            "photo_thumbnail_url": value_or_dash(aircraft.get("url_photo_thumbnail")),
            "photo_source": "adsbdb",
        }
        self._aircraft_cache[identifier] = (now, details)
        return details


class DemoProvider(FlightProvider):
    def departures(self, airport_iata: str, limit: int) -> list[Flight]:
        other = "北京首都" if airport_iata == "SHA" else "深圳宝安"
        return self._flights("MU5101", "中国东方航空", other, "10:35", "10:50", "departures", limit)

    def arrivals(self, airport_iata: str, limit: int) -> list[Flight]:
        other = "成都天府" if airport_iata == "SHA" else "广州白云"
        return self._flights("CA1855", "中国国际航空", other, "11:20", "11:18", "arrivals", limit)

    def _flights(
        self,
        number: str,
        airline: str,
        airport: str,
        scheduled: str,
        estimated: str,
        direction: str,
        limit: int,
    ) -> list[Flight]:
        if direction == "arrivals":
            first_departure_scheduled = "08:50"
            first_departure_estimated = "09:05"
            first_arrival_scheduled = scheduled
            first_arrival_estimated = estimated
            first_departure_runway = "-"
            first_arrival_runway = "34L"
        else:
            first_departure_scheduled = scheduled
            first_departure_estimated = estimated
            first_arrival_scheduled = "13:05"
            first_arrival_estimated = "13:20"
            first_departure_runway = "35R"
            first_arrival_runway = "-"

        sample = [
            Flight(
                flight_no=number,
                airline=airline,
                aircraft_registration="B-20EQ",
                aircraft_type="A359",
                aircraft_hex="780A2B",
                aircraft_photo_url="-",
                aircraft_photo_thumbnail_url="-",
                aircraft_photo_source="-",
                status="计划",
                scheduled=scheduled,
                estimated=estimated,
                actual="-",
                airport_name=airport,
                airport_iata="---",
                terminal="2",
                gate="C12",
                delay_minutes=15,
                departure_scheduled=first_departure_scheduled,
                departure_estimated=first_departure_estimated,
                departure_actual="-",
                arrival_scheduled=first_arrival_scheduled,
                arrival_estimated=first_arrival_estimated,
                arrival_actual="-",
                departure_runway=first_departure_runway,
                arrival_runway=first_arrival_runway,
            ),
            Flight(
                flight_no="HO1295",
                airline="吉祥航空",
                aircraft_registration="B-1783",
                aircraft_type="A320",
                aircraft_hex="78137F",
                aircraft_photo_url="-",
                aircraft_photo_thumbnail_url="-",
                aircraft_photo_source="-",
                status="计划",
                scheduled="12:05",
                estimated="12:05",
                actual="-",
                airport_name="厦门高崎",
                airport_iata="XMN",
                terminal="1",
                gate="B07",
                delay_minutes=None,
                departure_scheduled="12:05",
                departure_estimated="12:05",
                departure_actual="-",
                arrival_scheduled="14:00",
                arrival_estimated="14:00",
                arrival_actual="-",
                departure_runway="-",
                arrival_runway="-",
            ),
            Flight(
                flight_no="CZ3582",
                airline="中国南方航空",
                aircraft_registration="B-308Z",
                aircraft_type="A20N",
                aircraft_hex="78165C",
                aircraft_photo_url="-",
                aircraft_photo_thumbnail_url="-",
                aircraft_photo_source="-",
                status="计划",
                scheduled="12:40",
                estimated="12:33",
                actual="-",
                airport_name="重庆江北",
                airport_iata="CKG",
                terminal="2",
                gate="-",
                delay_minutes=None,
                departure_scheduled="12:40",
                departure_estimated="12:33",
                departure_actual="-",
                arrival_scheduled="15:25",
                arrival_estimated="15:18",
                arrival_actual="-",
                departure_runway="-",
                arrival_runway="18L",
            ),
        ]
        return sample[:limit]


def build_provider() -> FlightProvider:
    provider_name = os.getenv("FLIGHT_PROVIDER", "aviationstack").strip().lower()
    if provider_name == "demo":
        return DemoProvider()
    if provider_name == "aviationstack":
        cache_seconds = int(os.getenv("FLIGHT_CACHE_SECONDS", "45"))
        return AviationstackProvider(os.getenv("AVIATIONSTACK_API_KEY", ""), cache_seconds)
    raise FlightProviderError(f"未知 FLIGHT_PROVIDER：{provider_name}")


def normalize_airport(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip().lower()
    return AIRPORT_ALIASES.get(text)


def format_time(value: Any) -> str:
    if not value:
        return "-"
    if not isinstance(value, str):
        return str(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(SHANGHAI_TZ)
        return parsed.strftime("%m-%d %H:%M")
    except ValueError:
        return value


def translate_status(status: str) -> str:
    return {
        "scheduled": "计划",
        "active": "飞行中",
        "landed": "已落地",
        "cancelled": "已取消",
        "incident": "异常",
        "diverted": "备降",
    }.get(status.lower(), status)


def format_runway(data: dict[str, Any]) -> str:
    return value_or_dash(data.get("actual_runway") or data.get("estimated_runway") or data.get("runway"))


def first_value(primary: str, fallback: str) -> str:
    return primary if primary != "-" else fallback


def value_or_dash(value: Any) -> str:
    return str(value) if value not in (None, "") else "-"
