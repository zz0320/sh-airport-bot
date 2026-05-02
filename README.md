# 上海机场起降 Telegram 机器人

一个零依赖 Python Telegram bot，用来查询上海浦东机场（PVG）和上海虹桥机场（SHA）的航班起降信息。

## 功能

- `/today` 立即发送浦东和虹桥机场 v2 每日情报
- `/spotting` 查看今日值得看的宽体、少见航司、新见注册号和有图航班
- `/changes` 对比上次快照，查看状态、时间、登机口、跑道、注册号变化
- `/detail MU5101` 按航班号查询详情，命中图片时直接返回涂装图
- `/watch MU5101` 或“关注 MU5101”关注某一班变化
- `/subscribe 08:30` 订阅每日定时推送，时间使用 Asia/Shanghai
- `/unsubscribe` 取消每日推送
- `/settings` 查看订阅状态
- 主菜单入口：今日总览、今日看点、实时起降、涂装图片、最近变化、订阅设置
- 航班列表支持详情、涂装图片和关注航班
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
POLL_TIMEOUT_SECONDS=30
TELEGRAM_HTTP_TIMEOUT_SECONDS=40
DAILY_PUSH_TIME=08:30
DAILY_SUMMARY_LIMIT=4
DAILY_INCLUDE_PHOTOS=true
DEFAULT_DAILY_MODE=spotter
LIVE_REFRESH_SECONDS=300
WATCH_REFRESH_SECONDS=180
AIRCRAFT_ENRICH_PROVIDER=adsbdb
AIRCRAFT_ENRICH_CACHE_SECONDS=86400
PHOTO_PROVIDER=auto
PHOTO_CACHE_SECONDS=86400
PHOTO_LIMIT=3
SUBSCRIPTION_STORE=subscriptions.json
FLIGHT_MEMORY_STORE=flight_memory.json
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
/spotting
/changes
/detail MU5101
/watch MU5101
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
今日看点
最近变化
MU5101
关注 MU5101
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

图片输出会发送 1 到 `PHOTO_LIMIT` 张飞机照片，每张图的 caption 包含航班号、航司、注册号、机型、时间、航点和图片来源。图片会按“值得看”排序：宽体、有图、新见注册号、少见航司、跑道/登机口信息和异常延误会优先。`DAILY_INCLUDE_PHOTOS=true` 时，`/today` 和每日订阅会在文字总览后自动发送浦东/虹桥起飞与到达涂装图片。

多张图片会合并为 Telegram 相册发送，减少刷屏。

## 交互逻辑

推荐主路径：

```text
/start
按钮：今日总览 / 今日看点 / 实时起降 / 涂装图片 / 最近变化 / 订阅设置
```

机场详情页：

```text
起飞 / 到达 / 刷新 / 起飞图 / 到达图 / 今日总览
航班列表里可点：详情 / 图片 / 关注
```

每日推送：

```text
用户或群聊发送 /subscribe 08:30
机器人每天 08:30 自动发送 PVG + SHA v2 情报
可以在 /settings 里切换日报模式：精简 / 飞友 / 完整
如果 `DAILY_INCLUDE_PHOTOS=true`，总览后自动发送起飞和到达涂装图片
发送成功后记录当天日期、message_id 和内容 hash，避免重复发送
每隔 LIVE_REFRESH_SECONDS 秒检查一次当天日报，如果内容变化就编辑原消息
用户发送 /unsubscribe 后停止推送
```

关注航班：

```text
进入起飞或到达列表
点击某条航班的“关注”
也可以直接发送：关注 MU5101
机器人每隔 WATCH_REFRESH_SECONDS 秒检查一次
状态、时间、登机口、跑道或注册号变化时提醒
```

v2 记忆：

```text
/changes 会把这次查询结果和上一次快照对比
/spotting 会用历史见过的注册号判断“新见飞机”
记忆默认写入 FLIGHT_MEMORY_STORE
Docker 部署时建议设为 /app/data/flight_memory.json
```

头像：

```text
assets/sh-airport-bot-avatar.png
```

可以在 BotFather 里使用 `/setuserpic` 上传。

订阅信息默认写入 `subscriptions.json`，航班变化记忆默认写入 `flight_memory.json`，这两个文件已在 `.gitignore` 中忽略。

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
