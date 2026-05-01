# 群晖 NAS Docker 部署

这个机器人不需要对外开放端口。它通过 Telegram Bot API 长轮询收消息，所以 NAS 只需要能访问外网。

## 需要准备

- 群晖 DSM 7.2 或更新版本建议安装 `Container Manager`
- 旧 DSM 版本可能叫 `Docker`
- Telegram Bot Token
- Aviationstack API Key

## 推荐目录

在群晖上创建：

```text
/volume1/docker/sh-airport-bot
```

把本项目文件上传到这个目录。不要把 `.env` 上传到公开位置。

## 配置 `.env`

复制模板：

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
TELEGRAM_BOT_TOKEN=你的_telegram_bot_token
FLIGHT_PROVIDER=aviationstack
AVIATIONSTACK_API_KEY=你的_aviationstack_key

DAILY_PUSH_TIME=08:30
DAILY_SUMMARY_LIMIT=4
DAILY_INCLUDE_PHOTOS=false
LIVE_REFRESH_SECONDS=300
SUBSCRIPTION_STORE=/app/data/subscriptions.json

AIRCRAFT_ENRICH_PROVIDER=adsbdb
AIRCRAFT_ENRICH_CACHE_SECONDS=86400

PHOTO_PROVIDER=auto
PHOTO_CACHE_SECONDS=86400
PHOTO_LIMIT=3
```

## 用 Container Manager 界面部署

1. 打开 DSM 的 `套件中心`，安装 `Container Manager`。
2. 打开 `File Station`，把项目上传到 `/volume1/docker/sh-airport-bot`。
3. 打开 `Container Manager`。
4. 进入 `项目`。
5. 点 `新增`。
6. 项目名称填 `sh-airport-bot`。
7. 路径选择 `/volume1/docker/sh-airport-bot`。
8. 选择使用已有 `docker-compose.yml`。
9. 创建并启动项目。

启动后在 `Container Manager -> 容器 -> sh-airport-bot` 看日志，正常会看到：

```text
Shanghai flight Telegram bot is running. Press Ctrl+C to stop.
```

## 用 SSH 部署

如果你开启了 SSH，也可以这样：

```bash
cd /volume1/docker/sh-airport-bot
docker compose up -d --build
docker compose logs -f
```

停止：

```bash
docker compose down
```

重启：

```bash
docker compose restart
```

更新代码后重新构建：

```bash
docker compose up -d --build
```

## 验证

在 Telegram 对机器人发送：

```text
/start
/today
浦东起飞图
/subscribe 08:30
```

## 数据持久化

订阅信息写入：

```text
/volume1/docker/sh-airport-bot/data/subscriptions.json
```

这个文件来自 compose 的 volume 映射，容器重建后不会丢。

## 注意

- 不需要端口映射。
- 不能同时运行两个同 token 的机器人实例，否则 Telegram 会返回 `409 Conflict`。
- `.env` 里有 token，不要提交到公开仓库。
