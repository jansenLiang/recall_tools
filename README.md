# Mirako Recall Tools

一个对接 Recall.ai 的小服务：传入 Mirako session id、会议链接和模式，Recall bot 会进会并把 `/bridge/{session_id}` 当作网页音视频输出。

## 结构

```text
app/
  main.py                # FastAPI 装配入口
  api/                   # 路由
  clients/               # Recall / Zoom SDK 封装
  core/                  # 配置和路径
  models/                # 运行时数据结构
  schemas/               # 请求响应模型
  services/              # 会话编排
static/bridge.html       # 给 Recall bot 打开的网页
```

## 媒体链路

video 模式:

```text
Zoom participant audio
  -> Recall output-media browser getUserMedia({ audio: true })
  -> bridge WebRTC audio track
  -> live-stream-gateway

live-stream-gateway audio/video
  -> bridge WebRTC remote tracks
  -> <audio>/<video> playback
  -> Recall output-media capture
  -> Zoom bot audio/camera
```

audio 模式:

```text
Zoom participant audio
  -> Recall output-media browser getUserMedia({ audio: true })
  -> bridge WebRTC audio track
  -> live-stream-gateway

live-stream-gateway audio only
  -> bridge <audio> playback
  -> Recall output-media capture
  -> Zoom bot audio
```

## API

- `POST /api/sessions`
- `POST /api/sessions/{session_id}/close`

### 创建

```bash
curl -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your_service_api_key' \
  -d '{
    "mirako_session_id": "mirako-session-id",
    "meeting_url": "https://zoom.us/j/...",
    "mode": "video"
  }'
```

`gateway_url` 不作为 API 参数传入，统一从环境变量 `LIVE_STREAM_GATEWAY_URL` 读取。`mirako_session_id` 会作为 live-stream-gateway 的 `api_key` 创建 session。

`mode` 取值：

- `video`: 让 gateway 输出带画面的音视频，Recall bot 在 Zoom 里作为视频/音频输出。
- `audio`: 只做纯语音进会，bridge 页面显示黑屏占位，不依赖视频输出。

如果你只想做纯语音进会，`audio` 模式就是最轻的方案，不需要 GPU。

不传 `meeting_url` 时会用 Zoom OAuth 配置创建一个会议，再让 Recall bot 加入。

### 关闭

```bash
curl -X POST http://localhost:8000/api/sessions/$SESSION_ID/close \
  -H 'x-api-key: your_service_api_key'
```

关闭时会同时：

- 让 Recall bot 离开会议。
- 调用 `LIVE_STREAM_GATEWAY_URL/api/sessions/{mirako_session_id}/stop` 停掉 gateway session。
- 如果是本服务创建的 Zoom meeting，则结束 Zoom meeting。

## 本地跑

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`../recall_demo/.env` 会被自动读取，所以你可以直接复用那边的 Recall / Zoom 配置。

## RunPod 跑

这个仓库已经带了 `Dockerfile`，适合直接做 RunPod Pod 镜像。

最省事的方式：

1. 用当前仓库构建镜像。
2. 在 RunPod 里把容器端口设成 `8000`。
3. 注入这些环境变量：
   - `RECALL_API_KEY`
   - `RECALL_BASE_URL`
   - `PUBLIC_BASE_URL`
   - `LIVE_STREAM_GATEWAY_URL`
   - `SERVICE_API_KEY`
   - `ZOOM_OAUTH_CLIENT_ID`
   - `ZOOM_OAUTH_CLIENT_SECRET`
   - `ZOOM_OAUTH_ACCOUNT_ID`
4. 启动命令保持默认：`python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

如果你用的是 RunPod 提供的公开地址，就把它填到 `PUBLIC_BASE_URL`；如果你用 cloudflared/ngrok 等隧道，也直接把隧道 HTTPS 地址填到 `PUBLIC_BASE_URL`。

## Recall.ai 对接点

- Create Bot: `POST /api/v1/bot/`
- Output Media: `output_media` 参数随 Create Bot 一起传入
- Leave Call: `POST /api/v1/bot/{id}/leave_call/`

文档：

- https://docs.recall.ai/reference/bot_create
- https://docs.recall.ai/docs/stream-media
- https://docs.recall.ai/v1.10/reference/bot_leave_call_create

## 注意

`LIVE_STREAM_GATEWAY_URL` 必须是 Recall bot 浏览器可以访问的公网 HTTPS API 根地址，且 gateway 返回的 `ws_url` 也必须是公网可访问的 `wss://` 地址。

`SERVICE_API_KEY` 用于保护业务 API。设置后，`POST /api/sessions` 和 `POST /api/sessions/{session_id}/close` 必须带 `x-api-key` header；`/bridge/{session_id}` 不校验该 header，因为 Recall bot 需要直接打开这个网页。

Bridge 页面优先使用 `@ricky0123/vad-web` 发送 gateway 需要的 `audio-start` / `audio-end` 事件；如果 CDN 或模型加载失败，会自动回退到简单 RMS VAD。
