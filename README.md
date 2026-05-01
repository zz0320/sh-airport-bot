# 上海机场起降 Telegram 机器人

一个零依赖 Python Telegram bot，用来查询上海浦东机场（PVG）和上海虹桥机场（SHA）的航班起降信息。

## 功能

- `/today` 立即发送浦东和虹桥机场每日起降概述
- `/subscribe 08:30` 订阅每日定时推送，时间使用 Asia/Shanghai
- `/unsubscribe` 取消每日推送
- `/settings` 查看订阅状态
- `/pvg` 查看浦东机场起飞和到达概览
- `/sha` 查看虹桥机场起飞和到达概览
- `/departures PVG` 查询指定机场起飞
- `/arrivals SHA` 查询指定机场到达
- `/photos PVG` 获取近期起飞航班的飞机涂装图
- 支持按钮回调和简单中文自然语言，例如“浦东起飞”“虹桥到达”
- 支持“浦东起飞图”“虹桥到达照片”这类图片查询
- 内置 `demo` 数据源，方便没有航班 API key 时测试 Telegram 交互

## 数据源

默认接入 Aviationstack 的实时航班接口。它的 `flights` endpoint 支持用 `dep_iata` 和 `arr_iata` 按机场 IATA 代码筛选航班；Telegram Bot API 支持用 `getUpdates` 做长轮询并用 `sendMessage` 回复消息。

涂装图片默认先用 Aviationstack 返回的 `aircraft.icao24` 去 adsbdb 补注册号、机型和照片；如果 adsbdb 没有照片，再用注册号去 Planespotters 公共图片接口查询，并用 Telegram Bot API 的 `sendPhoto` 返回图片。

目标字段：

```text
航班号、航司、机型、注册号、涂装图库、起飞时间、到达时间、起飞跑道、到达跑道、航站楼、登机口、状态、延误
```

跑道取决于航班数据源是否提供。当前 Aviationstack provider 会读取 `aircraft.registration`、`aircraft.iata/icao`、`aircraft.icao24`、`departure.actual_runway/estimated_runway` 和 `arrival.actual_runway/estimated_runway`；当注册号或机型为空时，会尝试用 adsbdb 按 `icao24` 补全。

## 准备

1. 在 Telegram 找 `@BotFather` 创建机器人，拿到 bot token。
2. 复制环境变量文件：

```bash
cp .env.example .env
```

3. 编辑 `.env`：

```bash
TELEGRAM_BOT_TOKEN=你的_telegram_bot_token
FLIGHT_PROVIDER=aviationstack
AVIATIONSTACK_API_KEY=你的_aviationstack_key
DAILY_PUSH_TIME=08:30
DAILY_SUMMARY_LIMIT=4
DAILY_INCLUDE_PHOTOS=false
LIVE_REFRESH_SECONDS=300
AIRCRAFT_ENRICH_PROVIDER=adsbdb
AIRCRAFT_ENRICH_CACHE_SECONDS=86400
PHOTO_PROVIDER=auto
PHOTO_CACHE_SECONDS=86400
PHOTO_LIMIT=3
SUBSCRIPTION_STORE=subscriptions.json
```

没有 Aviationstack key 时，可以先这样跑通机器人：

```bash
FLIGHT_PROVIDER=demo
```

## 运行

```bash
python3 bot.py
```

## 常用命令

```text
/start
/today
/subscribe 08:30
/unsubscribe
/settings
/pvg
/sha
/departures PVG
/arrivals SHA
/photos PVG
/help
```

机场也支持中文：

```text
/departures 浦东
/arrivals 虹桥
浦东起飞
虹桥到达
浦东起飞图
虹桥到达照片
今日总览
订阅日报
```

## 输入输出设计

文字查询输入：

```text
浦东起飞
```

文字输出会包含：

```text
航班号 / 航司
机型 / 注册号
起飞时间 / 到达时间
状态 / 延误 / 航站楼 / 登机口
起飞跑道 / 到达跑道（如有）
目的地或出发地
涂装图库入口
```

图片查询输入：

```text
浦东起飞图
/photos SHA
```

图片输出会发送 1 到 `PHOTO_LIMIT` 张飞机照片，每张图的 caption 包含航班号、航司、注册号、机型、时间、航点和图片来源。

## 交互逻辑

推荐主路径：

```text
/start
按钮：今日总览 / 订阅日报 / 浦东 PVG / 虹桥 SHA
```

机场详情页：

```text
起飞 / 到达 / 起飞图 / 到达图 / 今日总览 / 订阅日报
```

每日推送：

```text
用户或群聊发送 /subscribe 08:30
机器人每天 08:30 自动发送 PVG + SHA 概述
发送成功后记录当天日期、message_id 和内容 hash，避免重复发送
每隔 LIVE_REFRESH_SECONDS 秒检查一次当天日报，如果内容变化就编辑原消息
用户发送 /unsubscribe 后停止推送
```

订阅信息默认写入 `subscriptions.json`，这个文件已在 `.gitignore` 中忽略。

刷新逻辑：

```text
今日总览消息带“刷新”按钮
点击刷新时编辑当前消息，不重复发送新消息
如果当前消息是图片消息而不能编辑文本，机器人会退回为发送一条新的文字总览
```

## 部署建议

- 本地长期运行：`tmux` / `screen`
- 服务器运行：systemd、Docker 或任意 Python 进程管理器
- 如果你改用 webhook，要先停止长轮询，并在 Telegram 侧设置 webhook

## 限制

航班实时性、延误字段和免费额度取决于你选择的数据供应商。这个项目把数据读取集中在 `flight_provider.py`，后续替换为 FlightAware AeroAPI、机场官方接口或其他商用 API 时，主要改这一层。

图片命中率取决于航班数据是否包含 `icao24` 或注册号，以及图片源是否收录该飞机。当前优先链路是：Aviationstack 返回 `aircraft.icao24`，adsbdb 补注册号/机型/照片；如果 adsbdb 没照片，再用注册号去 Planespotters 查图。
