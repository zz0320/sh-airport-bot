from __future__ import annotations

import html
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flight_provider import AIRPORTS, Flight, FlightProviderError, build_provider, normalize_airport
from flight_memory import FlightChange, FlightMemory
from photo_provider import AircraftPhoto, PhotoProviderError, build_photo_provider
from subscription_store import FlightWatch, Subscription, SubscriptionStore, normalize_daily_mode, normalize_hhmm


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
/spotting - 今日值得看的飞机
/changes - 最近航班信息变化
/detail MU5101 - 按航班号查询详情
/watch MU5101 - 关注航班变化

也可以直接发：今日总览、今日看点、最近变化、订阅、浦东起飞、虹桥到达、浦东起飞图、MU5101、关注 MU5101、PVG、SHA"""

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
AIRPORT_ORDER = ["PVG", "SHA"]
TIME_PRESETS = ["07:30", "08:30", "12:00", "18:00"]
DAILY_MODE_LABELS = {"brief": "精简", "spotter": "飞友", "full": "完整"}
FLIGHT_NO_RE = re.compile(r"\b([A-Z0-9]{2,3})\s?(\d{2,5}[A-Z]?)\b", re.IGNORECASE)
WIDEBODY_TYPES = {
    "A300",
    "A310",
    "A330",
    "A332",
    "A333",
    "A338",
    "A339",
    "A340",
    "A343",
    "A345",
    "A346",
    "A350",
    "A359",
    "A35K",
    "A380",
    "A388",
    "B747",
    "B748",
    "B763",
    "B764",
    "B772",
    "B773",
    "B77L",
    "B77W",
    "B787",
    "B788",
    "B789",
    "B78X",
}


class TelegramBot:
    def __init__(self, token: str, http_timeout: int) -> None:
        if not token:
            raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN。")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.http_timeout = http_timeout

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

    def send_media_group(self, chat_id: Any, media: list[dict[str, Any]]) -> Any:
        return self._request("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)})

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
        with urlopen(request, timeout=self.http_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload.get("result")


def main() -> None:
    load_env()
    poll_timeout = int(os.getenv("POLL_TIMEOUT_SECONDS", "30"))
    telegram_http_timeout = int(os.getenv("TELEGRAM_HTTP_TIMEOUT_SECONDS", str(poll_timeout + 10)))
    bot = TelegramBot(os.getenv("TELEGRAM_BOT_TOKEN", ""), telegram_http_timeout)
    provider = build_provider()
    photo_provider = build_photo_provider()
    subscriptions = SubscriptionStore(os.getenv("SUBSCRIPTION_STORE", "subscriptions.json"))
    memory = FlightMemory(os.getenv("FLIGHT_MEMORY_STORE", "flight_memory.json"))
    default_limit = int(os.getenv("DEFAULT_LIMIT", "8"))
    daily_summary_limit = int(os.getenv("DAILY_SUMMARY_LIMIT", "4"))
    daily_push_time = normalize_hhmm(os.getenv("DAILY_PUSH_TIME", "08:30"), "08:30")
    daily_include_photos = parse_bool(os.getenv("DAILY_INCLUDE_PHOTOS", "true"))
    default_daily_mode = normalize_daily_mode(os.getenv("DEFAULT_DAILY_MODE", "spotter"))
    live_refresh_seconds = int(os.getenv("LIVE_REFRESH_SECONDS", "300"))
    watch_refresh_seconds = int(os.getenv("WATCH_REFRESH_SECONDS", "180"))
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
                    memory,
                    update,
                    default_limit,
                    daily_summary_limit,
                    daily_push_time,
                    daily_include_photos,
                    default_daily_mode,
                    photo_limit,
                )
            run_due_daily_pushes(
                bot,
                provider,
                photo_provider,
                subscriptions,
                memory,
                daily_summary_limit,
                daily_include_photos,
                default_daily_mode,
                photo_limit,
            )
            run_due_daily_refreshes(bot, provider, subscriptions, memory, daily_summary_limit, live_refresh_seconds)
            run_due_watch_checks(bot, provider, subscriptions, watch_refresh_seconds)
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
    memory: FlightMemory,
    update: dict[str, Any],
    default_limit: int,
    daily_summary_limit: int,
    daily_push_time: str,
    daily_include_photos: bool,
    default_daily_mode: str,
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
                memory,
                chat_id,
                callback.get("data", ""),
                default_limit,
                daily_summary_limit,
                daily_push_time,
                daily_include_photos,
                default_daily_mode,
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
            memory,
            chat_id,
            text,
            default_limit,
            daily_summary_limit,
            daily_push_time,
            daily_include_photos,
            default_daily_mode,
            photo_limit,
            None,
        )


def safe_dispatch(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    subscriptions: SubscriptionStore,
    memory: FlightMemory,
    chat_id: Any,
    text: str,
    default_limit: int,
    daily_summary_limit: int,
    daily_push_time: str,
    daily_include_photos: bool,
    default_daily_mode: str,
    photo_limit: int,
    source_message_id: Optional[int],
) -> None:
    try:
        dispatch(
            bot,
            provider,
            photo_provider,
            subscriptions,
            memory,
            chat_id,
            text,
            default_limit,
            daily_summary_limit,
            daily_push_time,
            daily_include_photos,
            default_daily_mode,
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
    memory: FlightMemory,
    chat_id: Any,
    text: str,
    default_limit: int,
    daily_summary_limit: int,
    daily_push_time: str,
    daily_include_photos: bool,
    default_daily_mode: str,
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

    watch_request = parse_watch_text(text)
    if watch_request:
        watch_flight_number(bot, provider, subscriptions, chat_id, watch_request, default_limit)
        return

    if is_bare_flight_query(text):
        show_flight_number_result(bot, provider, photo_provider, chat_id, flight_number_from_text(text) or text, default_limit)
        return

    if command in {"realtime", "实时", "实时起降"}:
        airport = normalize_airport(args)
        if airport:
            bot.send_message(chat_id, airport_overview(provider, airport, default_limit), airport_keyboard(airport))
        else:
            bot.send_message(chat_id, "选择机场查看实时起降。", realtime_keyboard())
        return

    if command in {"liveries", "livery", "涂装图片", "涂装相册"}:
        airport = normalize_airport(args)
        if airport or args.strip().upper() == "ALL":
            send_livery_album(
                bot,
                provider,
                photo_provider,
                memory,
                chat_id,
                [airport] if airport else AIRPORT_ORDER,
                photo_limit,
            )
        else:
            bot.send_message(chat_id, "选择机场查看涂装相册。", livery_keyboard())
        return

    if command in {"spotting", "spot", "看点", "今日看点", "今日值得看", "飞友看点"}:
        airports = parse_airports(args) or AIRPORT_ORDER
        bot.send_message(chat_id, render_spotting(provider, memory, airports, max(default_limit, daily_summary_limit)), home_keyboard())
        update_memory_for_airports(provider, memory, airports, max(default_limit, daily_summary_limit))
        return

    if command in {"changes", "change", "变化", "最近变化", "动态"}:
        airports = parse_airports(args) or AIRPORT_ORDER
        bot.send_message(chat_id, render_changes(provider, memory, airports, max(default_limit, daily_summary_limit)), home_keyboard())
        return

    if command == "refresh":
        refresh_target(
            bot,
            provider,
            memory,
            chat_id,
            args or "today",
            daily_summary_limit,
            default_limit,
            source_message_id,
        )
        return

    if command == "detail":
        if flight_number_from_text(args):
            show_flight_number_result(bot, provider, photo_provider, chat_id, args, default_limit)
        else:
            show_flight_detail(bot, provider, photo_provider, chat_id, args, default_limit)
        return

    if command == "watch":
        if parse_flight_ref(args):
            watch_flight(bot, provider, subscriptions, chat_id, args, default_limit)
        elif flight_number_from_text(args):
            watch_flight_number(bot, provider, subscriptions, chat_id, args, default_limit)
        else:
            watch_flight(bot, provider, subscriptions, chat_id, args, default_limit)
        return

    if command == "unwatch":
        unwatch_flight(bot, subscriptions, chat_id, args)
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
            default_daily_mode,
            photo_limit,
            memory,
        )
        return

    if command in {"subscribe", "订阅", "订阅日报", "日报订阅", "每日推送"}:
        push_time = normalize_hhmm(args, daily_push_time)
        subscription = subscriptions.subscribe(
            chat_id,
            push_time,
            AIRPORT_ORDER,
            daily_include_photos,
            photo_limit,
            default_daily_mode,
        )
        bot.send_message(chat_id, render_subscription(subscription), subscription_keyboard(subscription))
        return

    if command in {"unsubscribe", "退订", "取消订阅", "取消日报", "取消推送"}:
        removed = subscriptions.unsubscribe(chat_id)
        text = "已取消每日推送。" if removed else "当前聊天还没有订阅每日推送。"
        bot.send_message(chat_id, text, home_keyboard())
        return

    if command in {"settings", "设置"}:
        if args:
            update_subscription_settings(bot, subscriptions, chat_id, args, daily_push_time, default_daily_mode)
            return
        subscription = subscriptions.get(chat_id)
        if subscription:
            bot.send_message(chat_id, render_subscription(subscription), subscription_keyboard(subscription))
        else:
            bot.send_message(chat_id, "当前聊天未订阅每日推送。", home_keyboard())
        return

    if command in {"flight", "航班", "查询", "查"} and flight_number_from_text(args or text):
        show_flight_number_result(bot, provider, photo_provider, chat_id, args or text, default_limit)
        return

    if command in {"pvg", "sha"}:
        airport = command.upper()
        bot.send_message(chat_id, airport_overview(provider, airport, default_limit), airport_keyboard(airport))
        return

    if command in {"dep", "departures", "起飞", "出发"}:
        airport = normalize_airport(args) or "PVG"
        send_flight_list(bot, provider, chat_id, "departures", airport, default_limit)
        return

    if command in {"arr", "arrivals", "到达", "抵达", "降落"}:
        airport = normalize_airport(args) or "PVG"
        send_flight_list(bot, provider, chat_id, "arrivals", airport, default_limit)
        return

    if command in {"photo", "photos", "图", "图片", "涂装"}:
        airport = normalize_airport(args) or "PVG"
        send_flight_photos(bot, provider, photo_provider, memory, chat_id, "departures", airport, photo_limit)
        return

    if command in {"photos:departures", "photos:arrivals"}:
        direction = command.split(":", 1)[1]
        airport = normalize_airport(args) or "PVG"
        send_flight_photos(bot, provider, photo_provider, memory, chat_id, direction, airport, photo_limit)
        return

    inferred = infer_query(text)
    if inferred:
        direction, airport = inferred
        if direction == "overview":
            bot.send_message(chat_id, airport_overview(provider, airport, default_limit), airport_keyboard(airport))
        elif direction == "departure_photos":
            send_flight_photos(bot, provider, photo_provider, memory, chat_id, "departures", airport, photo_limit)
        elif direction == "arrival_photos":
            send_flight_photos(bot, provider, photo_provider, memory, chat_id, "arrivals", airport, photo_limit)
        else:
            send_flight_list(bot, provider, chat_id, direction, airport, default_limit)
        return

    if flight_number_from_text(text):
        show_flight_number_result(bot, provider, photo_provider, chat_id, text, default_limit)
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


def parse_watch_text(text: str) -> Optional[str]:
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered.startswith(("/watch ", "watch ", "track ", "follow ")) or stripped.startswith(("关注", "追踪")):
        return flight_number_from_text(stripped)
    return None


def is_bare_flight_query(text: str) -> bool:
    flight_no = flight_number_from_text(text)
    return bool(flight_no and text.strip().replace(" ", "").upper() == flight_no)


def flight_number_from_text(text: str) -> Optional[str]:
    for match in FLIGHT_NO_RE.finditer(text or ""):
        prefix = match.group(1)
        if any(char.isalpha() for char in prefix):
            return f"{prefix}{match.group(2)}".upper()
    return None


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


def send_flight_list(bot: TelegramBot, provider: Any, chat_id: Any, direction: str, airport: str, limit: int) -> None:
    title = "起飞" if direction == "departures" else "到达"
    getter = provider.departures if direction == "departures" else provider.arrivals
    flights = safe_get(getter, airport, limit)
    text = f"<b>{escape(AIRPORTS[airport])}机场 {airport} {title}</b>\n\n{render_section(title, flights, direction)}"
    bot.send_message(chat_id, text, flight_list_keyboard(airport, direction, flights))


def show_flight_detail(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    chat_id: Any,
    args: str,
    limit: int,
) -> None:
    parsed = parse_flight_ref(args)
    if not parsed:
        bot.send_message(chat_id, "没有识别到航班。", home_keyboard())
        return
    direction, airport, index = parsed
    flight = get_indexed_flight(provider, direction, airport, index, limit)
    if not flight:
        bot.send_message(chat_id, "这条航班已经不在当前列表里了，请刷新后再试。", airport_keyboard(airport))
        return

    bot.send_message(chat_id, render_flight_detail(flight, direction), flight_detail_keyboard(airport, direction, index, flight))


def watch_flight(bot: TelegramBot, provider: Any, subscriptions: SubscriptionStore, chat_id: Any, args: str, limit: int) -> None:
    parsed = parse_flight_ref(args)
    if not parsed:
        bot.send_message(chat_id, "没有识别到要关注的航班。", home_keyboard())
        return
    direction, airport, index = parsed
    flight = get_indexed_flight(provider, direction, airport, index, limit)
    if not flight:
        bot.send_message(chat_id, "这条航班已经不在当前列表里了，请刷新后再试。", airport_keyboard(airport))
        return
    subscriptions.add_watch(chat_id, airport, direction, flight.flight_no, flight_state_hash(flight))
    bot.send_message(
        chat_id,
        f"已关注 <b>{escape(flight.flight_no)}</b>。状态、时间、登机口、跑道或注册号变化时会提醒你。",
        flight_detail_keyboard(airport, direction, index, flight),
    )


def unwatch_flight(bot: TelegramBot, subscriptions: SubscriptionStore, chat_id: Any, args: str) -> None:
    parts = args.split(":")
    if len(parts) < 3:
        bot.send_message(chat_id, "没有识别到要取消关注的航班。", home_keyboard())
        return
    direction, airport, flight_no = parts[0], parts[1], parts[2]
    removed = subscriptions.remove_watch(chat_id, airport, direction, flight_no)
    text = f"已取消关注 {escape(flight_no)}。" if removed else f"{escape(flight_no)} 当前没有被关注。"
    bot.send_message(chat_id, text, airport_keyboard(airport if airport in AIRPORTS else "PVG"))


def get_indexed_flight(provider: Any, direction: str, airport: str, index: int, limit: int) -> Optional[Flight]:
    if airport not in AIRPORTS or direction not in {"departures", "arrivals"}:
        return None
    getter = provider.departures if direction == "departures" else provider.arrivals
    flights = safe_get(getter, airport, max(limit, index + 1))
    return flights[index] if 0 <= index < len(flights) else None


def parse_flight_ref(args: str) -> Optional[tuple[str, str, int]]:
    parts = args.split(":")
    if len(parts) < 3:
        return None
    direction = parts[0]
    airport = parts[1].upper()
    try:
        index = int(parts[2])
    except ValueError:
        return None
    if direction not in {"departures", "arrivals"} or airport not in AIRPORTS:
        return None
    return direction, airport, index


def show_flight_number_result(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    chat_id: Any,
    text: str,
    limit: int,
) -> None:
    flight_no = flight_number_from_text(text)
    if not flight_no:
        bot.send_message(chat_id, "没有识别到航班号。", home_keyboard())
        return
    candidates = find_flight_candidates(provider, flight_no, limit)
    if not candidates:
        bot.send_message(chat_id, f"暂时没有在 PVG/SHA 今日起降里找到 {escape(flight_no)}。", home_keyboard())
        return
    if len(candidates) > 1:
        bot.send_message(chat_id, render_flight_candidates(flight_no, candidates), home_keyboard())
        return

    airport, direction, index, flight = candidates[0]
    try:
        photo = photo_for_flight(photo_provider, flight)
    except RuntimeError:
        photo = None
    keyboard = flight_detail_keyboard(airport, direction, index, flight)
    if photo:
        bot.send_chat_action(chat_id, "upload_photo")
        bot.send_photo(chat_id, photo.image_url, render_photo_caption(flight, direction, photo), keyboard)
        return
    bot.send_message(chat_id, render_flight_detail(flight, direction), keyboard)


def watch_flight_number(
    bot: TelegramBot,
    provider: Any,
    subscriptions: SubscriptionStore,
    chat_id: Any,
    text: str,
    limit: int,
) -> None:
    flight_no = flight_number_from_text(text)
    if not flight_no:
        bot.send_message(chat_id, "没有识别到要关注的航班号。", home_keyboard())
        return
    candidates = find_flight_candidates(provider, flight_no, limit)
    if not candidates:
        bot.send_message(chat_id, f"暂时没有在 PVG/SHA 今日起降里找到 {escape(flight_no)}，可以稍后再试。", home_keyboard())
        return
    airport, direction, index, flight = candidates[0]
    subscriptions.add_watch(chat_id, airport, direction, flight.flight_no, flight_state_hash(flight))
    bot.send_message(
        chat_id,
        f"已关注 <b>{escape(flight.flight_no)}</b>。状态、时间、登机口、跑道或注册号变化时会提醒你。",
        flight_detail_keyboard(airport, direction, index, flight),
    )


def find_flight_candidates(
    provider: Any,
    flight_no: str,
    limit: int,
) -> list[tuple[str, str, int, Flight]]:
    normalized = flight_no.replace(" ", "").upper()
    candidates = []
    for airport in AIRPORT_ORDER:
        for direction in ("departures", "arrivals"):
            getter = provider.departures if direction == "departures" else provider.arrivals
            for index, flight in enumerate(safe_get(getter, airport, max(limit, 12))):
                if flight.flight_no.replace(" ", "").upper() == normalized:
                    candidates.append((airport, direction, index, flight))
    return candidates


def render_flight_candidates(flight_no: str, candidates: list[tuple[str, str, int, Flight]]) -> str:
    lines = [f"<b>{escape(flight_no)} 找到多条记录</b>"]
    for airport, direction, index, flight in candidates:
        direction_text = "起飞" if direction == "departures" else "到达"
        lines.append(
            f"• {escape(AIRPORTS[airport])} {escape(airport)} {direction_text} #{index + 1}\n"
            f"  {escape(aircraft_text(flight))} · {escape(best_departure_time(flight))} / {escape(best_arrival_time(flight))}"
        )
    lines.append("可以从机场列表点详情，或发送“关注 航班号”追踪变化。")
    return "\n".join(lines)


def run_due_daily_pushes(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    subscriptions: SubscriptionStore,
    memory: FlightMemory,
    summary_limit: int,
    include_photos: bool,
    default_daily_mode: str,
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
                subscription.include_photos,
                subscription.daily_mode or default_daily_mode,
                subscription.photo_limit,
                memory,
            )
        except Exception as exc:
            print(f"Daily push failed for {subscription.chat_id}: {exc}", file=sys.stderr, flush=True)
            continue
        subscriptions.mark_sent(subscription.chat_id, today, message_id, message_hash)


def run_due_daily_refreshes(
    bot: TelegramBot,
    provider: Any,
    subscriptions: SubscriptionStore,
    memory: FlightMemory,
    summary_limit: int,
    refresh_seconds: int,
) -> None:
    if refresh_seconds <= 0:
        return
    now = datetime.now(SHANGHAI_TZ)
    for subscription in subscriptions.refresh_due(now, refresh_seconds):
        try:
            text = render_daily_summary(provider, subscription.airports, summary_limit, subscription.daily_mode, memory)
            message_hash = text_hash(text)
            if message_hash != subscription.last_message_hash and subscription.last_message_id:
                bot.edit_message_text(subscription.chat_id, subscription.last_message_id, text, home_keyboard())
                update_memory_for_airports(provider, memory, subscription.airports, summary_limit)
            subscriptions.mark_refreshed(subscription.chat_id, message_hash)
        except Exception as exc:
            print(f"Daily refresh failed for {subscription.chat_id}: {exc}", file=sys.stderr, flush=True)


def run_due_watch_checks(
    bot: TelegramBot,
    provider: Any,
    subscriptions: SubscriptionStore,
    refresh_seconds: int,
) -> None:
    now = datetime.now(SHANGHAI_TZ)
    for watch in subscriptions.watches_due(now, refresh_seconds):
        try:
            flight = find_watched_flight(provider, watch)
            if not flight:
                subscriptions.mark_watch_checked(watch, watch.last_hash)
                continue
            next_hash = flight_state_hash(flight)
            if next_hash != watch.last_hash:
                title = "起飞" if watch.direction == "departures" else "到达"
                bot.send_message(
                    watch.chat_id,
                    f"<b>{escape(watch.flight_no)} 状态更新</b>\n\n{render_flight(flight, watch.direction)}",
                    flight_watch_keyboard(watch.airport, watch.direction, watch.flight_no),
                )
            subscriptions.mark_watch_checked(watch, next_hash)
        except Exception as exc:
            print(f"Watch check failed for {watch.flight_no}: {exc}", file=sys.stderr, flush=True)


def find_watched_flight(provider: Any, watch: FlightWatch) -> Optional[Flight]:
    getter = provider.departures if watch.direction == "departures" else provider.arrivals
    for flight in safe_get(getter, watch.airport, 20):
        if flight.flight_no == watch.flight_no:
            return flight
    return None


def send_daily_summary(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    chat_id: Any,
    airports: list[str],
    summary_limit: int,
    include_photos: bool,
    daily_mode: str,
    photo_limit: int,
    memory: FlightMemory,
) -> tuple[Optional[int], str]:
    text = render_daily_summary(provider, airports, summary_limit, daily_mode, memory)
    result = bot.send_message(chat_id, text, home_keyboard())
    update_memory_for_airports(provider, memory, airports, summary_limit)
    if not include_photos:
        return extract_message_id(result), text_hash(text)
    for airport in airports:
        send_livery_album(bot, provider, photo_provider, memory, chat_id, [airport], photo_limit)
    return extract_message_id(result), text_hash(text)


def render_daily_summary(
    provider: Any,
    airports: list[str],
    limit: int,
    daily_mode: str = "spotter",
    memory: Optional[FlightMemory] = None,
) -> str:
    generated_at = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
    mode = normalize_daily_mode(daily_mode)
    sections = [
        f"<b>上海机场 v2 每日情报</b> · {escape(DAILY_MODE_LABELS[mode])}模式",
        f"生成时间：{escape(generated_at)} Asia/Shanghai",
    ]
    if mode in {"brief", "spotter"}:
        sections.append(render_intelligence_digest(provider, memory, airports, limit, mode))
    if mode == "brief":
        return "\n\n".join(sections)
    for airport in airports:
        sections.append(render_airport_daily(provider, airport, limit if mode == "full" else min(limit, 4)))
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


def render_intelligence_digest(
    provider: Any,
    memory: Optional[FlightMemory],
    airports: list[str],
    limit: int,
    mode: str,
) -> str:
    scored = ranked_flights(provider, memory, airports, max(limit, 8))
    total_departures = sum(1 for _, _, _, direction, _ in scored if direction == "departures")
    total_arrivals = sum(1 for _, _, _, direction, _ in scored if direction == "arrivals")
    delayed = [item for item in scored if item[4].delay_minutes or "延误" in item[4].status or "取消" in item[4].status]
    top_count = 3 if mode == "brief" else 5

    lines = [
        "<b>情报摘要</b>",
        f"覆盖：{escape('、'.join(airports))} · 起飞 {total_departures} 条 · 到达 {total_arrivals} 条",
    ]
    if delayed:
        lines.append(f"异常/延误：{len(delayed)} 条，发送 /changes 可看是否有新变化。")
    else:
        lines.append("异常/延误：当前样本内未见明显异常。")

    lines.append("<b>今日值得看</b>")
    if not scored:
        lines.append("暂时没有查到可用于判断的航班。")
        return "\n".join(lines)
    for index, item in enumerate(scored[:top_count], start=1):
        lines.append(render_spotting_item(index, item))
    return "\n".join(lines)


def render_spotting(provider: Any, memory: Optional[FlightMemory], airports: list[str], limit: int) -> str:
    scored = ranked_flights(provider, memory, airports, max(limit, 10))
    lines = [
        "<b>今日值得看</b>",
        "按宽体、注册号、可用照片、跑道/登机口信息、延误异常和新见飞机综合排序。",
    ]
    if not scored:
        lines.append("暂时没有查到可用于判断的航班。")
        return "\n".join(lines)
    for index, item in enumerate(scored[:8], start=1):
        lines.append(render_spotting_item(index, item))
    lines.append("发送“关注 MU5101”可以追踪某一班的状态变化。")
    return "\n".join(lines)


def render_changes(provider: Any, memory: FlightMemory, airports: list[str], limit: int) -> str:
    all_changes: list[FlightChange] = []
    for airport in airports:
        departures = safe_get(provider.departures, airport, max(limit, 8))
        arrivals = safe_get(provider.arrivals, airport, max(limit, 8))
        all_changes.extend(memory.compare(airport, "departures", departures))
        all_changes.extend(memory.compare(airport, "arrivals", arrivals))
        memory.update(airport, "departures", departures)
        memory.update(airport, "arrivals", arrivals)

    if not all_changes:
        return "\n".join(
            [
                "<b>最近变化</b>",
                "这次没有发现状态、时间、登机口、跑道或注册号变化。",
                "我已经刷新基线；下一次再点“最近变化”会和这次结果对比。",
            ]
        )

    lines = ["<b>最近变化</b>"]
    for change in all_changes[:10]:
        direction_text = "起飞" if change.direction == "departures" else "到达"
        lines.append(
            f"• <b>{escape(change.flight_no)}</b> {escape(AIRPORTS.get(change.airport, change.airport))} {direction_text}\n"
            f"  {escape('；'.join(change.changes[:3]))}"
        )
    if len(all_changes) > 10:
        lines.append(f"还有 {len(all_changes) - 10} 条变化未展示。")
    return "\n".join(lines)


def update_memory_for_airports(provider: Any, memory: FlightMemory, airports: list[str], limit: int) -> None:
    for airport in airports:
        memory.update(airport, "departures", safe_get(provider.departures, airport, max(limit, 8)))
        memory.update(airport, "arrivals", safe_get(provider.arrivals, airport, max(limit, 8)))


def ranked_flights(
    provider: Any,
    memory: Optional[FlightMemory],
    airports: list[str],
    limit: int,
) -> list[tuple[int, list[str], str, str, Flight]]:
    scored: list[tuple[int, list[str], str, str, Flight]] = []
    for airport in airports:
        for direction in ("departures", "arrivals"):
            getter = provider.departures if direction == "departures" else provider.arrivals
            for flight in safe_get(getter, airport, max(limit, 8)):
                score, reasons = flight_priority(flight, memory)
                scored.append((score, reasons, airport, direction, flight))
    scored.sort(key=lambda item: (item[0], best_time_for_sort(item[4], item[3])), reverse=True)
    return scored


def flight_priority(flight: Flight, memory: Optional[FlightMemory]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if flight.aircraft_photo_url != "-":
        score += 5
        reasons.append("有图")
    if flight.aircraft_registration != "-":
        score += 2
        reasons.append(flight.aircraft_registration)
        if memory and not memory.seen_registration(flight.aircraft_registration):
            score += 4
            reasons.append("新见")
    if is_widebody(flight):
        score += 4
        reasons.append("宽体")
    if flight.delay_minutes:
        score += 3
        reasons.append(f"延误{flight.delay_minutes}m")
    if "取消" in flight.status or "延误" in flight.status or "改道" in flight.status:
        score += 4
        reasons.append("状态异常")
    if format_runways_text(flight):
        score += 2
        reasons.append("有跑道")
    if flight.gate != "-":
        score += 1
    if is_non_base_airline(flight.airline):
        score += 1
        reasons.append("少见航司")
    if not reasons:
        reasons.append("时刻靠前")
    return score, dedupe_text(reasons)[:4]


def render_spotting_item(index: int, item: tuple[int, list[str], str, str, Flight]) -> str:
    _, reasons, airport, direction, flight = item
    direction_text = "起飞" if direction == "departures" else "到达"
    opposite_label = "去" if direction == "departures" else "从"
    route = f"{airport} {opposite_label} {airport_endpoint_text(flight)}"
    time_text = best_departure_time(flight) if direction == "departures" else best_arrival_time(flight)
    return (
        f"{index}. <b>{escape(flight.flight_no)}</b> {escape(flight.airline)}\n"
        f"   {escape(AIRPORTS[airport])} {direction_text} · {escape(route)}\n"
        f"   {escape(aircraft_text(flight))} · {escape(time_text)} · 看点：{escape('、'.join(reasons))}"
    )


def airport_endpoint_text(flight: Flight) -> str:
    if flight.airport_iata not in {"-", "---"}:
        return f"{flight.airport_name} {flight.airport_iata}"
    return flight.airport_name


def is_widebody(flight: Flight) -> bool:
    aircraft_type = flight.aircraft_type.replace("-", "").replace(" ", "").upper()
    return aircraft_type in WIDEBODY_TYPES or any(aircraft_type.startswith(prefix) for prefix in ("A33", "A34", "A35", "A38", "B77", "B78", "B74", "B76"))


def is_non_base_airline(airline: str) -> bool:
    base_names = ("东方", "上海航空", "中国国际", "南方", "春秋", "吉祥", "中国货运", "顺丰", "邮政")
    return airline != "-" and not any(name in airline for name in base_names)


def best_time_for_sort(flight: Flight, direction: str) -> str:
    if direction == "departures":
        return best_departure_time(flight)
    return best_arrival_time(flight)


def dedupe_text(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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


def render_flight_detail(flight: Flight, direction: str) -> str:
    direction_text = "起飞" if direction == "departures" else "到达"
    airport_label = "目的地" if direction == "departures" else "出发地"
    lines = [
        f"<b>{escape(flight.flight_no)} 航班详情</b>",
        f"航司：{escape(flight.airline)}",
        f"机型/注册号：{escape(aircraft_text(flight))}",
        f"方向：{direction_text}",
        f"起飞：{escape(best_departure_time(flight))}",
        f"到达：{escape(best_arrival_time(flight))}",
        f"状态：{escape(flight.status)}",
        f"{airport_label}：{escape(flight.airport_name)} {escape(flight.airport_iata)}",
    ]
    if flight.terminal != "-" or flight.gate != "-":
        lines.append(f"航站楼/登机口：{escape(flight.terminal)} / {escape(flight.gate)}")
    if flight.delay_minutes:
        lines.append(f"延误：{escape(flight.delay_minutes)} 分钟")
    if flight.departure_runway != "-" or flight.arrival_runway != "-":
        lines.append(f"跑道：{escape(format_runways_text(flight))}")
    if flight.aircraft_hex != "-":
        lines.append(f"Mode-S：{escape(flight.aircraft_hex)}")
    lines.append(f"涂装：{livery_link(flight)}")
    return "\n".join(lines)


def send_flight_photos(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    memory: Optional[FlightMemory],
    chat_id: Any,
    direction: str,
    airport: str,
    limit: int,
) -> None:
    title = "起飞" if direction == "departures" else "到达"
    items = collect_flight_photos(provider, photo_provider, memory, airport, direction, limit)
    if not items:
        bot.send_message(chat_id, f"{AIRPORTS[airport]}机场暂时没有查到{title}航班。", airport_keyboard(airport))
        return

    bot.send_chat_action(chat_id, "upload_photo")
    send_photo_items(bot, chat_id, items, airport_keyboard(airport))


def send_livery_album(
    bot: TelegramBot,
    provider: Any,
    photo_provider: Any,
    memory: Optional[FlightMemory],
    chat_id: Any,
    airports: list[str],
    limit: int,
) -> None:
    items: list[tuple[Flight, str, AircraftPhoto]] = []
    for airport in airports:
        items.extend(collect_flight_photos(provider, photo_provider, memory, airport, "departures", limit))
        items.extend(collect_flight_photos(provider, photo_provider, memory, airport, "arrivals", limit))
    if not items:
        bot.send_message(chat_id, "暂时没有找到可用涂装图片。", home_keyboard())
        return
    bot.send_chat_action(chat_id, "upload_photo")
    send_photo_items(bot, chat_id, dedupe_photo_items(items)[: max(1, min(limit * max(len(airports), 1), 10))], home_keyboard())


def collect_flight_photos(
    provider: Any,
    photo_provider: Any,
    memory: Optional[FlightMemory],
    airport: str,
    direction: str,
    limit: int,
) -> list[tuple[Flight, str, AircraftPhoto]]:
    getter = provider.departures if direction == "departures" else provider.arrivals
    flights = safe_get(getter, airport, max(limit * 3, limit))
    flights = sorted(flights, key=lambda flight: flight_priority(flight, memory)[0], reverse=True)
    items: list[tuple[Flight, str, AircraftPhoto]] = []
    for flight in flights:
        if len(items) >= limit:
            break
        try:
            photo = photo_for_flight(photo_provider, flight)
        except RuntimeError:
            continue
        if photo:
            items.append((flight, direction, photo))
    return items


def photo_for_flight(photo_provider: Any, flight: Flight) -> Optional[AircraftPhoto]:
    if flight.aircraft_photo_url != "-":
        return AircraftPhoto(
            image_url=flight.aircraft_photo_url,
            thumbnail_url=flight.aircraft_photo_thumbnail_url,
            photographer=flight.aircraft_photo_source,
            source_url=flight.aircraft_photo_url,
            aircraft_registration=flight.aircraft_registration,
            aircraft_type=flight.aircraft_type,
        )
    if flight.aircraft_registration == "-" and flight.aircraft_hex == "-":
        return None
    try:
        return photo_provider.find_for_aircraft(flight.aircraft_registration, flight.aircraft_hex)
    except PhotoProviderError as exc:
        raise RuntimeError(str(exc)) from exc


def send_photo_items(
    bot: TelegramBot,
    chat_id: Any,
    items: list[tuple[Flight, str, AircraftPhoto]],
    reply_markup: Optional[dict[str, Any]],
) -> None:
    if len(items) == 1:
        flight, direction, photo = items[0]
        bot.send_photo(chat_id, photo.image_url, render_photo_caption(flight, direction, photo), reply_markup)
        return
    media = []
    for flight, direction, photo in items[:10]:
        media.append(
            {
                "type": "photo",
                "media": photo.image_url,
                "caption": render_photo_caption(flight, direction, photo)[:1024],
                "parse_mode": "HTML",
            }
        )
    bot.send_media_group(chat_id, media)
    if reply_markup:
        bot.send_message(chat_id, "涂装相册已发送。", reply_markup)


def dedupe_photo_items(items: list[tuple[Flight, str, AircraftPhoto]]) -> list[tuple[Flight, str, AircraftPhoto]]:
    seen = set()
    result = []
    for item in items:
        url = item[2].image_url
        if url in seen:
            continue
        seen.add(url)
        result.append(item)
    return result


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
    text = format_runways_text(flight)
    return f"\n  跑道：{escape(text)}" if text else ""


def format_runways_text(flight: Flight) -> str:
    parts = []
    if flight.departure_runway != "-":
        parts.append(f"起飞跑道 {flight.departure_runway}")
    if flight.arrival_runway != "-":
        parts.append(f"到达跑道 {flight.arrival_runway}")
    return " / ".join(parts)


def livery_link(flight: Flight) -> str:
    if flight.aircraft_photo_url != "-":
        return f'<a href="{escape(flight.aircraft_photo_url)}">照片</a>'
    if flight.aircraft_registration == "-":
        if flight.aircraft_hex != "-":
            return f'<a href="https://api.adsbdb.com/v0/aircraft/{quote(flight.aircraft_hex)}">待查</a>'
        return "-"
    registration = quote(flight.aircraft_registration)
    return f'<a href="https://www.planespotters.net/photos/reg/{registration}">图库</a>'


def flight_state_hash(flight: Flight) -> str:
    payload = "|".join(
        [
            flight.flight_no,
            flight.status,
            flight.departure_scheduled,
            flight.departure_estimated,
            flight.departure_actual,
            flight.arrival_scheduled,
            flight.arrival_estimated,
            flight.arrival_actual,
            flight.terminal,
            flight.gate,
            str(flight.delay_minutes),
            flight.aircraft_registration,
            flight.aircraft_type,
            flight.departure_runway,
            flight.arrival_runway,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_subscription(subscription: Subscription) -> str:
    airports = "、".join(f"{AIRPORTS[airport]} {airport}" for airport in subscription.airports)
    include_photos = "开启" if subscription.include_photos else "关闭"
    daily_mode = DAILY_MODE_LABELS.get(subscription.daily_mode, DAILY_MODE_LABELS["spotter"])
    return (
        "<b>每日推送已开启</b>\n"
        f"时间：{escape(subscription.push_time)} Asia/Shanghai\n"
        f"机场：{escape(airports)}\n"
        f"模式：{escape(daily_mode)}\n"
        f"涂装图片：{include_photos}，每次最多 {subscription.photo_limit} 张\n"
        "内容：航班号、航司、机型、注册号、涂装图片、起飞/到达时间、跑道（如数据源提供）"
    )


def update_subscription_settings(
    bot: TelegramBot,
    subscriptions: SubscriptionStore,
    chat_id: Any,
    args: str,
    default_push_time: str,
    default_daily_mode: str,
) -> None:
    subscription = subscriptions.ensure(chat_id, default_push_time, default_daily_mode)
    parts = args.split(":")
    action = parts[0] if parts else ""
    updated: Optional[Subscription] = subscription

    if action == "time" and len(parts) >= 2:
        updated = subscriptions.update_push_time(chat_id, normalize_hhmm(":".join(parts[1:]), subscription.push_time))
    elif action == "photos" and len(parts) >= 2:
        updated = subscriptions.update_include_photos(chat_id, parts[1] == "on")
    elif action == "limit" and len(parts) >= 2:
        try:
            updated = subscriptions.update_photo_limit(chat_id, int(parts[1]))
        except ValueError:
            updated = subscription
    elif action == "airports" and len(parts) >= 2:
        updated = subscriptions.update_airports(chat_id, parse_airports(parts[1]) or ["PVG", "SHA"])
    elif action == "mode" and len(parts) >= 2:
        updated = subscriptions.update_daily_mode(chat_id, normalize_daily_mode(parts[1] or default_daily_mode))
    else:
        bot.send_message(chat_id, render_subscription(subscription), subscription_keyboard(subscription))
        return

    bot.send_message(chat_id, render_subscription(updated or subscription), subscription_keyboard(updated or subscription))


def refresh_target(
    bot: TelegramBot,
    provider: Any,
    memory: FlightMemory,
    chat_id: Any,
    target: str,
    daily_summary_limit: int,
    default_limit: int,
    source_message_id: Optional[int],
) -> None:
    normalized = target.strip().lower()
    if normalized in {"", "today", "daily", "all", "总览"}:
        text = render_daily_summary(provider, ["PVG", "SHA"], daily_summary_limit, "spotter", memory)
        keyboard = home_keyboard()
    elif normalized in {"spotting", "spot", "看点"}:
        text = render_spotting(provider, memory, AIRPORT_ORDER, max(default_limit, daily_summary_limit))
        keyboard = home_keyboard()
    elif normalized in {"changes", "change", "变化"}:
        text = render_changes(provider, memory, AIRPORT_ORDER, max(default_limit, daily_summary_limit))
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
                {"text": "今日看点", "callback_data": "spotting"},
            ],
            [
                {"text": "实时起降", "callback_data": "realtime"},
                {"text": "涂装图片", "callback_data": "liveries"},
            ],
            [
                {"text": "最近变化", "callback_data": "changes"},
                {"text": "订阅设置", "callback_data": "settings"},
            ],
        ]
    }


def realtime_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "浦东 PVG", "callback_data": "pvg"},
                {"text": "虹桥 SHA", "callback_data": "sha"},
            ],
            [
                {"text": "全部机场总览", "callback_data": "today"},
                {"text": "今日看点", "callback_data": "spotting"},
                {"text": "最近变化", "callback_data": "changes"},
            ],
        ]
    }


def livery_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "浦东涂装", "callback_data": "liveries:PVG"},
                {"text": "虹桥涂装", "callback_data": "liveries:SHA"},
            ],
            [
                {"text": "全部涂装", "callback_data": "liveries:ALL"},
                {"text": "菜单", "callback_data": "menu"},
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
                {"text": "菜单", "callback_data": "menu"},
            ],
        ]
    }


def flight_list_keyboard(airport: str, direction: str, flights: list[Flight]) -> dict[str, Any]:
    rows = []
    for index, flight in enumerate(flights[:6]):
        rows.append(
            [
                {"text": f"{flight.flight_no} 详情", "callback_data": f"detail:{direction}:{airport}:{index}"},
                {"text": "图片", "callback_data": f"photos:{direction}:{airport}"},
                {"text": "关注", "callback_data": f"watch:{direction}:{airport}:{index}"},
            ]
        )
    rows.extend(airport_keyboard(airport)["inline_keyboard"])
    return {"inline_keyboard": rows}


def flight_detail_keyboard(airport: str, direction: str, index: int, flight: Flight) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "涂装图片", "callback_data": f"photos:{direction}:{airport}"},
                {"text": "关注航班", "callback_data": f"watch:{direction}:{airport}:{index}"},
            ],
            [
                {"text": "返回列表", "callback_data": f"{direction}:{airport}"},
                {"text": "取消关注", "callback_data": f"unwatch:{direction}:{airport}:{flight.flight_no}"},
            ],
            [
                {"text": "菜单", "callback_data": "menu"},
            ],
        ]
    }


def flight_watch_keyboard(airport: str, direction: str, flight_no: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "查看机场", "callback_data": airport.lower()},
                {"text": "取消关注", "callback_data": f"unwatch:{direction}:{airport}:{flight_no}"},
            ]
        ]
    }


def subscription_keyboard(subscription: Optional[Subscription] = None) -> dict[str, Any]:
    photo_enabled = subscription.include_photos if subscription else True
    return {
        "inline_keyboard": [
            [
                {"text": "立即看今日总览", "callback_data": "today"},
                {"text": "取消订阅", "callback_data": "unsubscribe"},
            ],
            [
                {"text": "07:30", "callback_data": "settings:time:07:30"},
                {"text": "08:30", "callback_data": "settings:time:08:30"},
                {"text": "12:00", "callback_data": "settings:time:12:00"},
                {"text": "18:00", "callback_data": "settings:time:18:00"},
            ],
            [
                {"text": "精简", "callback_data": "settings:mode:brief"},
                {"text": "飞友", "callback_data": "settings:mode:spotter"},
                {"text": "完整", "callback_data": "settings:mode:full"},
            ],
            [
                {
                    "text": "关闭图片" if photo_enabled else "开启图片",
                    "callback_data": f"settings:photos:{'off' if photo_enabled else 'on'}",
                },
                {"text": "图片3张", "callback_data": "settings:limit:3"},
                {"text": "图片6张", "callback_data": "settings:limit:6"},
            ],
            [
                {"text": "PVG+SHA", "callback_data": "settings:airports:PVG,SHA"},
                {"text": "仅PVG", "callback_data": "settings:airports:PVG"},
                {"text": "仅SHA", "callback_data": "settings:airports:SHA"},
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
