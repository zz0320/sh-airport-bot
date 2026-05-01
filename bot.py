from __future__ import annotations

import html
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flight_provider import AIRPORTS, Flight, FlightProviderError, build_provider, normalize_airport
from photo_provider import AircraftPhoto, PhotoProviderError, build_photo_provider
from subscription_store import Subscription, SubscriptionStore, normalize_hhmm


HELP_TEXT = """✈️ 上海机场起降查询

可用命令：
/today - 立即发送浦东和虹桥今日概述
/refresh - 刷新并编辑当前总览消息
/subscribe 08:30 - 订阅每日定时推送
/unsubscribe - 取消每日推送
/settings - 查看订阅设置
/pvg - 浦东机场起降概览
/sha - 虹桥机场起降概览
/departures PVG - 查询起飞
/arrivals SHA - 查询到达
/photos PVG - 获取近期航班涂装图片

也可以直接发：今日总览、订阅、浦东起飞、虹桥到达、浦东起飞图、PVG、SHA"""

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class TelegramBot:
    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN。")
        self.base_url = f"https://api.telegram.org/bot{token}"

    def get_updates(self, offset: Optional[int], timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        result = self._request("getUpdates", params)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: Any, text: str, reply_markup: Optional[dict[str, Any]] = None) -> Any:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._request("sendMessage", params)

    def edit_message_text(
        self,
        chat_id: Any,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> Any:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._request("editMessageText", params)

    def send_photo(self, chat_id: Any, photo_url: str, caption: str, reply_markup: Optional[dict[str, Any]] = None) -> None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        self._request("sendPhoto", params)

    def send_chat_action(self, chat_id: Any, action: str) -> None:
        self._request("sendChatAction", {"chat_id": chat_id, "action": action})

    def answer_callback_query(self, callback_query_id: str) -> None:
        self._request("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        data = urlencode(params).encode("utf-8")
        request = Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"User-Agent": "shanghai-flight-telegram-bot/1.0"},
        )
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload.get("result")


def main() -> None:
    load_env()
    bot = TelegramBot(os.getenv("TELEGRAM_BOT_TOKEN", ""))
    provider = build_provider()
    photo_provider = build_photo_provider()
    subscriptions = SubscriptionStore(os.getenv("SUBSCRIPTION_STORE", "subscriptions.json"))
    poll_timeout = int(os.getenv("POLL_TIMEOUT_SECONDS", "30"))
    default_limit = int(os.getenv("DEFAULT_LIMIT", "8"))
    daily_summary_limit = int(os.getenv("DAILY_SUMMARY_LIMIT", "4"))
    daily_push_time = normalize_hhmm(os.getenv("DAILY_PUSH_TIME", "08:30"), "08:30")
    daily_include_photos = parse_bool(os.getenv("DAILY_INCLUDE_PHOTOS", "false"))
    live_refresh_seconds = int(os.getenv("LIVE_REFRESH_SECONDS", "300"))
    photo_limit = int(os.getenv("PHOTO_LIMIT", "3"))

    print("Shanghai flight Telegram bot is running. Press Ctrl+C to stop.", flush=True)
    offset: Optional[int] = None
    while True:
        try:
            updates = bot.get_updates(offset, poll_timeout)
            for update in updates:
                offset = max(offset or 0, update["update_id"] + 1)
                handle_update(
                    bot,
                    provider,
                    photo_provider,
                    subscriptions,
                    update,
                    default_limit,
                    daily_summary_limit,
                    daily_push_time,
                    daily_include_photos,
                    photo_limit,
                )
            run_due_daily_pushes(
                bot,
                provider,
                photo_provider,
                subscriptions,
                daily_summary_limit,
                daily_include_photos,
                photo_limit,
            )
            run_due_daily_refreshes(bot, provider, subscriptions, daily_summary_limit, live_refresh_seconds)
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            return
        except Exception as exc:
            print(f"Loop error: {exc}", file=sys.stderr, flush=True)
            time.sleep(3)


def handle_update(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    subscriptions: SubscriptionStore,
    update: dict[str, Any],
    default_limit: int,
    daily_summary_limit: int,
    daily_push_time: str,
    daily_include_photos: bool,
    photo_limit: int,
) -> None:
    if "callback_query" in update:
        callback = update["callback_query"]
        bot.answer_callback_query(callback["id"])
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        if chat_id:
            safe_dispatch(
                bot,
                provider,
                photo_provider,
                subscriptions,
                chat_id,
                callback.get("data", ""),
                default_limit,
                daily_summary_limit,
                daily_push_time,
                daily_include_photos,
                photo_limit,
                message_id,
            )
        return

    message = update.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if chat_id and text:
        safe_dispatch(
            bot,
            provider,
            photo_provider,
            subscriptions,
            chat_id,
            text,
            default_limit,
            daily_summary_limit,
            daily_push_time,
            daily_include_photos,
            photo_limit,
            None,
        )


def safe_dispatch(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    subscriptions: SubscriptionStore,
    chat_id: Any,
    text: str,
    default_limit: int,
    daily_summary_limit: int,
    daily_push_time: str,
    daily_include_photos: bool,
    photo_limit: int,
    source_message_id: Optional[int],
) -> None:
    try:
        dispatch(
            bot,
            provider,
            photo_provider,
            subscriptions,
            chat_id,
            text,
            default_limit,
            daily_summary_limit,
            daily_push_time,
            daily_include_photos,
            photo_limit,
            source_message_id,
        )
    except Exception as exc:
        bot.send_message(chat_id, f"查询失败：{escape(exc)}", home_keyboard())


def dispatch(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    subscriptions: SubscriptionStore,
    chat_id: Any,
    text: str,
    default_limit: int,
    daily_summary_limit: int,
    daily_push_time: str,
    daily_include_photos: bool,
    photo_limit: int,
    source_message_id: Optional[int],
) -> None:
    command, args = parse_command(text)

    if command in {"start", "help"}:
        bot.send_message(chat_id, HELP_TEXT, home_keyboard())
        return

    if command in {"menu", "菜单"}:
        bot.send_message(chat_id, "请选择你要查看的机场信息。", home_keyboard())
        return

    if command == "refresh":
        refresh_target(
            bot,
            provider,
            chat_id,
            args or "today",
            daily_summary_limit,
            default_limit,
            source_message_id,
        )
        return

    if command in {"today", "daily", "overview", "日报", "今日", "今日总览", "总览"}:
        airports = parse_airports(args) or ["PVG", "SHA"]
        send_daily_summary(
            bot,
            provider,
            photo_provider,
            chat_id,
            airports,
            daily_summary_limit,
            daily_include_photos,
            photo_limit,
        )
        return

    if command in {"subscribe", "订阅", "订阅日报", "日报订阅", "每日推送"}:
        push_time = normalize_hhmm(args, daily_push_time)
        subscription = subscriptions.subscribe(chat_id, push_time, ["PVG", "SHA"])
        bot.send_message(chat_id, render_subscription(subscription), subscription_keyboard())
        return

    if command in {"unsubscribe", "退订", "取消订阅", "取消日报", "取消推送"}:
        removed = subscriptions.unsubscribe(chat_id)
        text = "已取消每日推送。" if removed else "当前聊天还没有订阅每日推送。"
        bot.send_message(chat_id, text, home_keyboard())
        return

    if command in {"settings", "设置"}:
        subscription = subscriptions.get(chat_id)
        if subscription:
            bot.send_message(chat_id, render_subscription(subscription), subscription_keyboard())
        else:
            bot.send_message(chat_id, "当前聊天未订阅每日推送。", home_keyboard())
        return

    if command in {"pvg", "sha"}:
        airport = command.upper()
        bot.send_message(chat_id, airport_overview(provider, airport, default_limit), airport_keyboard(airport))
        return

    if command in {"dep", "departures", "起飞", "出发"}:
        airport = normalize_airport(args) or "PVG"
        bot.send_message(chat_id, render_flights(provider, "departures", airport, default_limit), airport_keyboard(airport))
        return

    if command in {"arr", "arrivals", "到达", "抵达", "降落"}:
        airport = normalize_airport(args) or "PVG"
        bot.send_message(chat_id, render_flights(provider, "arrivals", airport, default_limit), airport_keyboard(airport))
        return

    if command in {"photo", "photos", "图", "图片", "涂装"}:
        airport = normalize_airport(args) or "PVG"
        send_flight_photos(bot, provider, photo_provider, chat_id, "departures", airport, photo_limit)
        return

    if command in {"photos:departures", "photos:arrivals"}:
        direction = command.split(":", 1)[1]
        airport = normalize_airport(args) or "PVG"
        send_flight_photos(bot, provider, photo_provider, chat_id, direction, airport, photo_limit)
        return

    inferred = infer_query(text)
    if inferred:
        direction, airport = inferred
        if direction == "overview":
            bot.send_message(chat_id, airport_overview(provider, airport, default_limit), airport_keyboard(airport))
        elif direction == "departure_photos":
            send_flight_photos(bot, provider, photo_provider, chat_id, "departures", airport, photo_limit)
        elif direction == "arrival_photos":
            send_flight_photos(bot, provider, photo_provider, chat_id, "arrivals", airport, photo_limit)
        else:
            bot.send_message(chat_id, render_flights(provider, direction, airport, default_limit), airport_keyboard(airport))
        return

    bot.send_message(chat_id, HELP_TEXT, home_keyboard())


def parse_command(text: str) -> tuple[str, str]:
    if text.startswith("/"):
        parts = text[1:].split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return command, args
    if ":" in text:
        parts = [part.strip() for part in text.split(":")]
        if len(parts) >= 3 and parts[0].lower() == "photos":
            return f"photos:{parts[1].lower()}", parts[2]
        command, args = text.split(":", 1)
        return command.strip().lower(), args.strip()
    return text.strip().lower(), ""


def infer_query(text: str) -> Optional[tuple[str, str]]:
    lowered = text.strip().lower()
    airport = None
    for alias in ("pvg", "sha", "浦东", "虹桥", "上海浦东", "上海虹桥"):
        if alias in lowered:
            airport = normalize_airport(alias)
            break
    if not airport:
        return None
    wants_photo = any(word in lowered for word in ("图", "图片", "照片", "涂装", "livery", "photo"))
    if any(word in lowered for word in ("起飞", "出发", "depart", "dep")):
        if wants_photo:
            return ("departure_photos", airport)
        return ("departures", airport)
    if any(word in lowered for word in ("到达", "抵达", "降落", "arrival", "arr")):
        if wants_photo:
            return ("arrival_photos", airport)
        return ("arrivals", airport)
    if wants_photo:
        return ("departure_photos", airport)
    return ("overview", airport)


def airport_overview(provider: Any, airport: str, limit: int) -> str:
    short_limit = max(1, min(limit // 2, 4))
    departures = safe_get(provider.departures, airport, short_limit)
    arrivals = safe_get(provider.arrivals, airport, short_limit)
    return "\n\n".join(
        [
            f"<b>{escape(AIRPORTS[airport])}机场 {airport}</b>",
            render_section("起飞", departures, "departures"),
            render_section("到达", arrivals, "arrivals"),
        ]
    )


def render_flights(provider: Any, direction: str, airport: str, limit: int) -> str:
    title = "起飞" if direction == "departures" else "到达"
    getter = provider.departures if direction == "departures" else provider.arrivals
    flights = safe_get(getter, airport, limit)
    return f"<b>{escape(AIRPORTS[airport])}机场 {airport} {title}</b>\n\n{render_section(title, flights, direction)}"


def safe_get(getter: Any, airport: str, limit: int) -> list[Flight]:
    try:
        return getter(airport, limit)
    except FlightProviderError as exc:
        raise RuntimeError(str(exc)) from exc


def run_due_daily_pushes(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    subscriptions: SubscriptionStore,
    summary_limit: int,
    include_photos: bool,
    photo_limit: int,
) -> None:
    now = datetime.now(SHANGHAI_TZ)
    today = now.date().isoformat()
    for subscription in subscriptions.due(now):
        try:
            message_id, message_hash = send_daily_summary(
                bot,
                provider,
                photo_provider,
                subscription.chat_id,
                subscription.airports,
                summary_limit,
                include_photos,
                photo_limit,
            )
        except Exception as exc:
            print(f"Daily push failed for {subscription.chat_id}: {exc}", file=sys.stderr, flush=True)
            continue
        subscriptions.mark_sent(subscription.chat_id, today, message_id, message_hash)


def run_due_daily_refreshes(
    bot: TelegramBot,
    provider: Any,
    subscriptions: SubscriptionStore,
    summary_limit: int,
    refresh_seconds: int,
) -> None:
    if refresh_seconds <= 0:
        return
    now = datetime.now(SHANGHAI_TZ)
    for subscription in subscriptions.refresh_due(now, refresh_seconds):
        try:
            text = render_daily_summary(provider, subscription.airports, summary_limit)
            message_hash = text_hash(text)
            if message_hash != subscription.last_message_hash and subscription.last_message_id:
                bot.edit_message_text(subscription.chat_id, subscription.last_message_id, text, home_keyboard())
            subscriptions.mark_refreshed(subscription.chat_id, message_hash)
        except Exception as exc:
            print(f"Daily refresh failed for {subscription.chat_id}: {exc}", file=sys.stderr, flush=True)


def send_daily_summary(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    chat_id: Any,
    airports: list[str],
    summary_limit: int,
    include_photos: bool,
    photo_limit: int,
) -> tuple[Optional[int], str]:
    text = render_daily_summary(provider, airports, summary_limit)
    result = bot.send_message(chat_id, text, home_keyboard())
    if not include_photos:
        return extract_message_id(result), text_hash(text)
    per_airport_photo_limit = max(1, min(photo_limit, 2))
    for airport in airports:
        send_flight_photos(bot, provider, photo_provider, chat_id, "departures", airport, per_airport_photo_limit)
    return extract_message_id(result), text_hash(text)


def render_daily_summary(provider: Any, airports: list[str], limit: int) -> str:
    generated_at = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
    sections = [
        "<b>上海机场每日起降概述</b>",
        f"生成时间：{escape(generated_at)} Asia/Shanghai",
    ]
    for airport in airports:
        sections.append(render_airport_daily(provider, airport, limit))
    return "\n\n".join(sections)


def render_airport_daily(provider: Any, airport: str, limit: int) -> str:
    departures = safe_get(provider.departures, airport, limit)
    arrivals = safe_get(provider.arrivals, airport, limit)
    return "\n\n".join(
        [
            f"<b>{escape(AIRPORTS[airport])}机场 {airport}</b>",
            render_section("起飞", departures, "departures"),
            render_section("到达", arrivals, "arrivals"),
        ]
    )


def render_section(title: str, flights: list[Flight], direction: str) -> str:
    if not flights:
        return f"<b>{title}</b>\n暂时没有查到航班。"

    rows = [f"<b>{title}</b>"]
    for flight in flights:
        rows.append(render_flight(flight, direction))
    return "\n".join(rows)


def render_flight(flight: Flight, direction: str) -> str:
    airport_label = "目的地" if direction == "departures" else "出发地"
    delay = f" 延误{flight.delay_minutes}m" if flight.delay_minutes else ""
    terminal = f" T{flight.terminal}" if flight.terminal != "-" else ""
    gate = f" 登机口{flight.gate}" if flight.gate != "-" else ""
    status = f"{flight.status}{delay}{terminal}{gate}".strip()
    runway = format_runways(flight)
    return (
        f"• <b>{escape(flight.flight_no)}</b> {escape(flight.airline)}\n"
        f"  机型/注册号：{escape(aircraft_text(flight))}\n"
        f"  起飞：{escape(best_departure_time(flight))} · 到达：{escape(best_arrival_time(flight))}\n"
        f"  状态：{escape(status)}{runway}\n"
        f"  {airport_label}：{escape(flight.airport_name)} {escape(flight.airport_iata)}\n"
        f"  涂装：{livery_link(flight)}"
    )


def send_flight_photos(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    chat_id: Any,
    direction: str,
    airport: str,
    limit: int,
) -> None:
    title = "起飞" if direction == "departures" else "到达"
    getter = provider.departures if direction == "departures" else provider.arrivals
    flights = safe_get(getter, airport, max(limit * 2, limit))
    if not flights:
        bot.send_message(chat_id, f"{AIRPORTS[airport]}机场暂时没有查到{title}航班。", airport_keyboard(airport))
        return

    bot.send_chat_action(chat_id, "upload_photo")
    sent = 0
    skipped: list[str] = []
    for flight in flights:
        if sent >= limit:
            break
        if flight.aircraft_photo_url != "-":
            photo = AircraftPhoto(
                image_url=flight.aircraft_photo_url,
                thumbnail_url=flight.aircraft_photo_thumbnail_url,
                photographer=flight.aircraft_photo_source,
                source_url=flight.aircraft_photo_url,
                aircraft_registration=flight.aircraft_registration,
                aircraft_type=flight.aircraft_type,
            )
            bot.send_photo(chat_id, photo.image_url, render_photo_caption(flight, direction, photo), airport_keyboard(airport))
            sent += 1
            continue

        if flight.aircraft_registration == "-" and flight.aircraft_hex == "-":
            skipped.append(flight.flight_no)
            continue
        try:
            photo = photo_provider.find_for_aircraft(flight.aircraft_registration, flight.aircraft_hex)
        except PhotoProviderError as exc:
            raise RuntimeError(str(exc)) from exc
        if not photo:
            skipped.append(f"{flight.flight_no}/{aircraft_text(flight)}")
            continue
        bot.send_photo(chat_id, photo.image_url, render_photo_caption(flight, direction, photo), airport_keyboard(airport))
        sent += 1

    if sent == 0:
        suffix = f"\n未命中：{escape(', '.join(skipped[:6]))}" if skipped else ""
        bot.send_message(chat_id, f"这批{title}航班没有找到可用涂装图片。{suffix}", airport_keyboard(airport))


def render_photo_caption(flight: Flight, direction: str, photo: Any) -> str:
    airport_label = "目的地" if direction == "departures" else "出发地"
    return (
        f"<b>{escape(flight.flight_no)}</b> {escape(flight.airline)}\n"
        f"{escape(photo_aircraft_text(flight, photo))} · {escape(flight.status)}\n"
        f"起飞：{escape(best_departure_time(flight))} · 到达：{escape(best_arrival_time(flight))}\n"
        f"{airport_label}：{escape(flight.airport_name)} {escape(flight.airport_iata)}\n"
        f"Photo: {escape(photo.photographer)} · {escape(photo.source_url)}"
    )


def photo_aircraft_text(flight: Flight, photo: Any) -> str:
    registration = photo.aircraft_registration if getattr(photo, "aircraft_registration", "-") != "-" else flight.aircraft_registration
    aircraft_type = photo.aircraft_type if getattr(photo, "aircraft_type", "-") != "-" else flight.aircraft_type
    parts = []
    if registration != "-":
        parts.append(registration)
    if aircraft_type != "-":
        parts.append(aircraft_type)
    if not parts and flight.aircraft_hex != "-":
        parts.append(flight.aircraft_hex)
    return " ".join(parts) if parts else "-"


def aircraft_text(flight: Flight) -> str:
    parts = []
    if flight.aircraft_registration != "-":
        parts.append(flight.aircraft_registration)
    if flight.aircraft_type != "-":
        parts.append(flight.aircraft_type)
    if not parts and flight.aircraft_hex != "-":
        parts.append(flight.aircraft_hex)
    return " ".join(parts) if parts else "-"


def best_departure_time(flight: Flight) -> str:
    return best_time(flight.departure_actual, flight.departure_estimated, flight.departure_scheduled)


def best_arrival_time(flight: Flight) -> str:
    return best_time(flight.arrival_actual, flight.arrival_estimated, flight.arrival_scheduled)


def best_time(actual: str, estimated: str, scheduled: str) -> str:
    if actual != "-":
        return f"实际 {actual}"
    if estimated != "-":
        return f"预计 {estimated}"
    if scheduled != "-":
        return f"计划 {scheduled}"
    return "-"


def format_runways(flight: Flight) -> str:
    parts = []
    if flight.departure_runway != "-":
        parts.append(f"起飞跑道 {flight.departure_runway}")
    if flight.arrival_runway != "-":
        parts.append(f"到达跑道 {flight.arrival_runway}")
    return f"\n  跑道：{escape(' / '.join(parts))}" if parts else ""


def livery_link(flight: Flight) -> str:
    if flight.aircraft_photo_url != "-":
        return f'<a href="{escape(flight.aircraft_photo_url)}">照片</a>'
    if flight.aircraft_registration == "-":
        if flight.aircraft_hex != "-":
            return f'<a href="https://api.adsbdb.com/v0/aircraft/{quote(flight.aircraft_hex)}">待查</a>'
        return "-"
    registration = quote(flight.aircraft_registration)
    return f'<a href="https://www.planespotters.net/photos/reg/{registration}">图库</a>'


def render_subscription(subscription: Subscription) -> str:
    airports = "、".join(f"{AIRPORTS[airport]} {airport}" for airport in subscription.airports)
    return (
        "<b>每日推送已开启</b>\n"
        f"时间：{escape(subscription.push_time)} Asia/Shanghai\n"
        f"机场：{escape(airports)}\n"
        "内容：航班号、航司、机型、注册号、涂装图库、起飞/到达时间、跑道（如数据源提供）"
    )


def refresh_target(
    bot: TelegramBot,
    provider: Any,
    chat_id: Any,
    target: str,
    daily_summary_limit: int,
    default_limit: int,
    source_message_id: Optional[int],
) -> None:
    normalized = target.strip().lower()
    if normalized in {"", "today", "daily", "all", "总览"}:
        text = render_daily_summary(provider, ["PVG", "SHA"], daily_summary_limit)
        keyboard = home_keyboard()
    else:
        airport = normalize_airport(normalized) or normalized.upper()
        if airport not in AIRPORTS:
            text = HELP_TEXT
            keyboard = home_keyboard()
        else:
            text = airport_overview(provider, airport, default_limit)
            keyboard = airport_keyboard(airport)

    if source_message_id:
        try:
            bot.edit_message_text(chat_id, source_message_id, text, keyboard)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, keyboard)


def parse_airports(value: str) -> list[str]:
    if not value or value.upper() == "ALL":
        return []
    airports = []
    for raw in value.replace(",", " ").replace("、", " ").split():
        airport = normalize_airport(raw) or raw.strip().upper()
        if airport in AIRPORTS and airport not in airports:
            airports.append(airport)
    return airports


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def extract_message_id(result: Any) -> Optional[int]:
    if isinstance(result, dict) and isinstance(result.get("message_id"), int):
        return result["message_id"]
    return None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def home_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "今日总览", "callback_data": "today"},
                {"text": "刷新", "callback_data": "refresh:today"},
            ],
            [
                {"text": "订阅日报", "callback_data": "subscribe"},
                {"text": "订阅设置", "callback_data": "settings"},
            ],
            [
                {"text": "浦东 PVG", "callback_data": "pvg"},
                {"text": "虹桥 SHA", "callback_data": "sha"},
            ],
        ]
    }


def airport_keyboard(airport: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "起飞", "callback_data": f"departures:{airport}"},
                {"text": "到达", "callback_data": f"arrivals:{airport}"},
                {"text": "刷新", "callback_data": f"refresh:{airport}"},
            ],
            [
                {"text": "起飞图", "callback_data": f"photos:departures:{airport}"},
                {"text": "到达图", "callback_data": f"photos:arrivals:{airport}"},
            ],
            [
                {"text": "浦东", "callback_data": "pvg"},
                {"text": "虹桥", "callback_data": "sha"},
            ],
            [
                {"text": "今日总览", "callback_data": "today"},
                {"text": "订阅日报", "callback_data": "subscribe"},
            ],
        ]
    }


def subscription_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "立即看今日总览", "callback_data": "today"},
                {"text": "取消订阅", "callback_data": "unsubscribe"},
            ],
            [
                {"text": "菜单", "callback_data": "menu"},
            ],
        ]
    }


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()
