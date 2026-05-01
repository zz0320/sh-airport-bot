from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class AircraftPhoto:
    image_url: str
    thumbnail_url: str
    photographer: str
    source_url: str
    aircraft_registration: str = "-"
    aircraft_type: str = "-"


class PhotoProviderError(RuntimeError):
    pass


class PhotoProvider:
    def find_by_registration(self, registration: str) -> Optional[AircraftPhoto]:
        raise NotImplementedError

    def find_for_aircraft(self, registration: str, mode_s: str) -> Optional[AircraftPhoto]:
        if registration and registration != "-":
            return self.find_by_registration(registration)
        return None


class DisabledPhotoProvider(PhotoProvider):
    def find_by_registration(self, registration: str) -> Optional[AircraftPhoto]:
        return None

    def find_for_aircraft(self, registration: str, mode_s: str) -> Optional[AircraftPhoto]:
        return None


class CombinedPhotoProvider(PhotoProvider):
    def __init__(self, providers: list[PhotoProvider]) -> None:
        self.providers = providers

    def find_by_registration(self, registration: str) -> Optional[AircraftPhoto]:
        for provider in self.providers:
            photo = provider.find_by_registration(registration)
            if photo:
                return photo
        return None

    def find_for_aircraft(self, registration: str, mode_s: str) -> Optional[AircraftPhoto]:
        for provider in self.providers:
            photo = provider.find_for_aircraft(registration, mode_s)
            if photo:
                return photo
        return None


class AdsbdbPhotoProvider(PhotoProvider):
    def __init__(self, cache_seconds: int = 86400) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, Optional[AircraftPhoto]]] = {}

    def find_by_registration(self, registration: str) -> Optional[AircraftPhoto]:
        return self._fetch(normalize_registration(registration))

    def find_for_aircraft(self, registration: str, mode_s: str) -> Optional[AircraftPhoto]:
        identifier = normalize_registration(mode_s) or normalize_registration(registration)
        return self._fetch(identifier)

    def _fetch(self, identifier: str) -> Optional[AircraftPhoto]:
        if not identifier:
            return None

        cached = self._cache.get(identifier)
        now = time.time()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]

        url = f"https://api.adsbdb.com/v0/aircraft/{quote(identifier)}"
        request = Request(url, headers={"User-Agent": "shanghai-flight-telegram-bot/1.0"})
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PhotoProviderError(f"adsbdb 图片接口请求失败：{exc}") from exc

        aircraft = (payload.get("response") or {}).get("aircraft") or {}
        image_url = aircraft.get("url_photo")
        thumbnail_url = aircraft.get("url_photo_thumbnail") or image_url
        if not image_url:
            self._cache[identifier] = (now, None)
            return None

        photo = AircraftPhoto(
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            photographer=value_or_dash(aircraft.get("registered_owner")),
            source_url=image_url,
            aircraft_registration=value_or_dash(aircraft.get("registration")),
            aircraft_type=value_or_dash(aircraft.get("icao_type") or aircraft.get("type")),
        )
        self._cache[identifier] = (now, photo)
        return photo


class PlanespottersPhotoProvider(PhotoProvider):
    def __init__(self, cache_seconds: int = 86400) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, Optional[AircraftPhoto]]] = {}
        self._registration_cache: dict[str, tuple[float, str]] = {}

    def find_by_registration(self, registration: str) -> Optional[AircraftPhoto]:
        normalized = normalize_registration(registration)
        if not normalized:
            return None

        cached = self._cache.get(normalized)
        now = time.time()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]

        photo = self._fetch(normalized)
        self._cache[normalized] = (now, photo)
        return photo

    def find_for_aircraft(self, registration: str, mode_s: str) -> Optional[AircraftPhoto]:
        normalized = normalize_registration(registration)
        if not normalized:
            normalized = self._registration_from_mode_s(mode_s)
        if not normalized:
            return None
        return self.find_by_registration(normalized)

    def _registration_from_mode_s(self, mode_s: str) -> str:
        normalized = normalize_registration(mode_s)
        if not normalized:
            return ""

        cached = self._registration_cache.get(normalized)
        now = time.time()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]

        url = f"https://api.adsbdb.com/v0/aircraft/{quote(normalized)}"
        request = Request(url, headers={"User-Agent": "shanghai-flight-telegram-bot/1.0"})
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            self._registration_cache[normalized] = (now, "")
            return ""

        aircraft = (payload.get("response") or {}).get("aircraft") or {}
        registration = normalize_registration(value_or_dash(aircraft.get("registration")))
        self._registration_cache[normalized] = (now, registration)
        return registration

    def _fetch(self, registration: str) -> Optional[AircraftPhoto]:
        url = f"https://api.planespotters.net/pub/photos/reg/{quote(registration)}"
        request = Request(url, headers={"User-Agent": "shanghai-flight-telegram-bot/1.0"})
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PhotoProviderError(f"飞机图片接口请求失败：{exc}") from exc

        photos = payload.get("photos")
        if not isinstance(photos, list) or not photos:
            return None

        first = photos[0]
        thumbnail = first.get("thumbnail") or {}
        large = first.get("large") or {}
        image_url = large.get("src") or thumbnail.get("src") or first.get("link")
        thumbnail_url = thumbnail.get("src") or image_url
        if not image_url:
            return None

        return AircraftPhoto(
            image_url=image_url,
            thumbnail_url=thumbnail_url or image_url,
            photographer=value_or_dash(first.get("photographer")),
            source_url=f"https://www.planespotters.net/photos/reg/{quote(registration)}",
            aircraft_registration=registration,
        )


def build_photo_provider() -> PhotoProvider:
    provider_name = os.getenv("PHOTO_PROVIDER", "auto").strip().lower()
    if provider_name in {"", "none", "disabled", "off"}:
        return DisabledPhotoProvider()
    if provider_name == "auto":
        cache_seconds = int(os.getenv("PHOTO_CACHE_SECONDS", "86400"))
        return CombinedPhotoProvider([AdsbdbPhotoProvider(cache_seconds), PlanespottersPhotoProvider(cache_seconds)])
    if provider_name == "adsbdb":
        cache_seconds = int(os.getenv("PHOTO_CACHE_SECONDS", "86400"))
        return AdsbdbPhotoProvider(cache_seconds)
    if provider_name == "planespotters":
        cache_seconds = int(os.getenv("PHOTO_CACHE_SECONDS", "86400"))
        return PlanespottersPhotoProvider(cache_seconds)
    raise PhotoProviderError(f"未知 PHOTO_PROVIDER：{provider_name}")


def normalize_registration(registration: str) -> str:
    value = registration.strip().upper()
    return "" if value in {"", "-"} else value


def value_or_dash(value: Any) -> str:
    return str(value) if value not in (None, "") else "-"
