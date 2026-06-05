# Mirako Recall Tools

A small Recall.ai integration service. Given a Mirako session id, a meeting provider, an optional meeting URL, and a media mode, it creates a Recall bot that joins the meeting and opens `/bridge/{session_id}` as its webpage media output.

## Role

`recall_tools` does not join Zoom directly and does not generate the avatar. It creates the Recall.ai bot, serves the bridge page, passes the business session id to `live-stream-gateway`, and cleans up both the Recall bot and the gateway session when the session is closed.

## Project Structure

```text
app/
  main.py                # FastAPI entrypoint
  api/                   # Routes
  clients/               # Recall / Zoom client wrappers
  core/                  # Config and paths
  models/                # Runtime data structures
  schemas/               # Request / response models
  services/              # Session orchestration
static/bridge.html       # Webpage opened by the Recall bot
```

## Media Flow

Overall architecture:

```text
Client / Backend
  |  POST /api/sessions
  |  x-api-key: SERVICE_API_KEY
  v
recall_tools (FastAPI)
  |  Create Bot + output_media.camera.kind=webpage
  v
Recall.ai
  |  bot joins meeting
  |  bot browser opens PUBLIC_BASE_URL/bridge/{session_id}
  v
Zoom Meeting <-------------------------------+
  | participant audio                         |
  v                                           |
Recall bot output-media browser              |
  | getUserMedia({ audio: true })             |
  | WS PCM16 audio chunks + VAD events        |
  v                                           |
live-stream-gateway                          |
  | api_key = mirako_session_id               |
  | talks to Mirako / Metis backend           |
  | returns agent audio/video over WebRTC     |
  v                                           |
bridge.html playback ------------------------+
  | Recall captures webpage media
  v
Zoom bot microphone/camera output
```

Control flow:

```text
POST /api/sessions
  -> recall_tools validates x-api-key
  -> recall_tools creates an in-memory session
  -> recall_tools calls Recall.ai Create Bot
  -> Recall.ai bot joins Zoom and opens bridge_url
  -> bridge.html calls LIVE_STREAM_GATEWAY_URL/api/sessions
  -> bridge.html connects LIVE_STREAM_GATEWAY_URL/ws/{mirako_session_id}

POST /api/sessions/{session_id}/close
  -> recall_tools validates x-api-key
  -> Recall bot leave_call
  -> live-stream-gateway stop session
  -> optionally end Zoom meeting created by this service
```

Video mode:

```text
Zoom participant audio
  -> Recall output-media browser getUserMedia({ audio: true })
  -> bridge WebSocket PCM16 chunks
  -> live-stream-gateway

live-stream-gateway audio/video
  -> bridge WebRTC remote tracks
  -> single <video> playback for synchronized audio/video
  -> Recall output-media capture
  -> Zoom bot audio/camera
```

Audio mode:

```text
Zoom participant audio
  -> Recall output-media browser getUserMedia({ audio: true })
  -> bridge WebSocket PCM16 chunks
  -> live-stream-gateway

live-stream-gateway audio only
  -> bridge <audio> playback
  -> Recall output-media capture
  -> Zoom bot audio
```

## API

Interactive API docs are available after starting the service:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

Only the business APIs are included in the generated docs. `/bridge/{session_id}` is intentionally hidden from OpenAPI because it is opened directly by the Recall bot browser.

- `POST /api/sessions`
- `POST /api/sessions/{session_id}/close`
- `GET /api/meeting-records`

### Create Session

```bash
curl -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your_service_api_key' \
  -d '{
    "mirako_session_id": "mirako-session-id",
    "meeting_provider": "zoom",
    "meeting_url": "https://zoom.us/j/...",
    "mode": "video"
  }'
```

`gateway_url` is not accepted as an API parameter. It is always read from the `LIVE_STREAM_GATEWAY_URL` environment variable. `mirako_session_id` is sent to `live-stream-gateway` as the gateway `api_key` and becomes the gateway session id.

`memory_user` is optional. When omitted, transcript memory inserts use `METIS_MEMORY_USER`.

`mode` values:

- `video`: the gateway sends audio and video into the meeting through the Recall bot.
- `audio`: the gateway sends audio only; the bridge page shows a black placeholder and does not require video output.

`meeting_provider` values:

- `zoom`: implemented. If `meeting_url` is omitted, this service creates a Zoom meeting using the configured Zoom OAuth credentials.
- `google_meet`: reserved for a future strategy implementation. Recall.ai supports Google Meet, but this service does not implement the Google Meet meeting strategy yet.

Use `audio` mode if you only need voice. It is the lightest mode and does not require a GPU.

If `meeting_url` is provided, the service asks the Recall bot to join that existing meeting URL. If `meeting_url` is omitted with `meeting_provider: "zoom"`, the service creates a Zoom meeting and then asks the Recall bot to join it.

### Close Session

```bash
curl -X POST http://localhost:8000/api/sessions/$SESSION_ID/close \
  -H 'x-api-key: your_service_api_key'
```

Closing a session performs these actions:

- Ask the Recall bot to leave the meeting.
- Call `LIVE_STREAM_GATEWAY_URL/api/sessions/{mirako_session_id}/stop` to stop the gateway session.
- End the Zoom meeting if it was created by this service.

### Get Meeting Records

Recall.ai transcript webhooks are stored as meeting records in SQLite at `RECALL_DATA_DB_PATH`, which defaults to `data/recall_tools.sqlite3`.

```bash
curl 'http://localhost:8000/api/meeting-records?session_id=recall-tools-session-id' \
  -H 'x-api-key: your_service_api_key'
```

You can also query by the Mirako session id:

```bash
curl 'http://localhost:8000/api/meeting-records?mirako_session_id=mirako-session-id' \
  -H 'x-api-key: your_service_api_key'
```

Pagination is optional. If `limit` is omitted, all matching records are returned.

```bash
curl 'http://localhost:8000/api/meeting-records?mirako_session_id=mirako-session-id&limit=50&offset=0' \
  -H 'x-api-key: your_service_api_key'
```

Each record contains the speaker, content, participant metadata, word-level payload when available, and start/end time fields extracted from Recall.ai transcript payloads.

## Local Development

Create `.env`:

```bash
cp .env.example .env
```

Minimum required configuration:

```env
RECALL_API_KEY=your_recall_api_key
RECALL_BASE_URL=https://ap-northeast-1.recall.ai/api/v1
PUBLIC_BASE_URL=https://your-recall-tools-public-url.example.com
LIVE_STREAM_GATEWAY_URL=https://your-live-stream-gateway-public-url.example.com
SERVICE_API_KEY=your_service_api_key
```

`PUBLIC_BASE_URL` is the public HTTPS base URL that the Recall bot browser uses to open this service's bridge page. `LIVE_STREAM_GATEWAY_URL` is the public HTTPS base URL that the Recall bot browser uses to reach `live-stream-gateway`.

Start `recall_tools`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For development, use auto reload:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

For local testing, expose the service through a public HTTPS tunnel such as cloudflared, ngrok, or RunPod proxy, then set `PUBLIC_BASE_URL` to that public URL. Example:

```bash
cloudflared tunnel --url http://localhost:8000
```

Create a bot:

```bash
curl -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your_service_api_key' \
  -d '{
    "mirako_session_id": "dev",
    "meeting_provider": "zoom",
    "meeting_url": "https://zoom.us/j/...",
    "mode": "video"
  }'
```

To create a Zoom meeting automatically, omit `meeting_url`:

```bash
curl -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your_service_api_key' \
  -d '{
    "mirako_session_id": "dev",
    "meeting_provider": "zoom",
    "mode": "video"
  }'
```

Close a bot:

```bash
curl -X POST http://localhost:8000/api/sessions/$SESSION_ID/close \
  -H 'x-api-key: your_service_api_key'
```

`../recall_demo/.env` is also loaded automatically, so you can reuse Recall and Zoom configuration from that project when running locally.

## RunPod Deployment

This repository includes a `Dockerfile` and can be deployed as a RunPod Pod image.

Recommended setup:

1. Build an image from this repository.
2. Set the container port to `8000` in RunPod.
3. Inject these environment variables:
   - `RECALL_API_KEY`
   - `RECALL_BASE_URL`
   - `PUBLIC_BASE_URL`
   - `LIVE_STREAM_GATEWAY_URL`
   - `SERVICE_API_KEY`
   - `ZOOM_OAUTH_CLIENT_ID`
   - `ZOOM_OAUTH_CLIENT_SECRET`
   - `ZOOM_OAUTH_ACCOUNT_ID`
4. Use the default start command: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

If you use the public URL provided by RunPod, set it as `PUBLIC_BASE_URL`. If you use cloudflared, ngrok, or another tunnel, set `PUBLIC_BASE_URL` to that HTTPS tunnel URL instead.

## Recall.ai Integration Points

- Create Bot: `POST /api/v1/bot/`
- Output Media: the `output_media` parameter is sent in the Create Bot request
- Real-time transcript webhook: `recording_config.realtime_endpoints[].url = PUBLIC_BASE_URL/api/recall/transcript`
- Leave Call: `POST /api/v1/bot/{id}/leave_call/`

## Meeting Memory

When `RECALL_TRANSCRIPT_ENABLED=true`, new bots are created with Recall real-time transcription enabled. Final `transcript.data` events are received at `/api/recall/transcript`, matched back to the in-memory session through Recall endpoint/bot metadata, stored on the runtime session, and sent to Metis memory through:

```text
POST {METIS_MEMORY_BASE_URL}{METIS_MEMORY_INSERT_PATH}
```

The request body follows this shape:

```json
{
  "session_id": "mirako-session-id",
  "content": "Duncan prefers the weekly meeting summary to be short and action-oriented.",
  "user": "duncan",
  "speaker": "Duncan",
  "role": "meeting_note",
  "source": "recall.ai",
  "metadata": {
    "topic": "meeting",
    "importance": "high",
    "recall_session_id": "recall-tools-session-id",
    "recall_bot_id": "recall-bot-id",
    "participant": {}
  }
}
```

Set `RECALL_WEBHOOK_SECRET` to the Recall workspace secret so incoming transcript webhooks are verified. If it is empty, webhook signature verification is skipped, which is useful only for local testing.

Docs:

- https://docs.recall.ai/reference/bot_create
- https://docs.recall.ai/docs/stream-media
- https://docs.recall.ai/docs/bot-real-time-transcription
- https://docs.recall.ai/docs/authenticating-requests-from-recallai
- https://docs.recall.ai/v1.10/reference/bot_leave_call_create

## Notes

`LIVE_STREAM_GATEWAY_URL` must be a public HTTPS API base URL reachable from the Recall bot browser. The `ws_url` returned by the gateway must also be publicly reachable over `wss://`.

`SERVICE_API_KEY` protects the business API. When it is set, `POST /api/sessions` and `POST /api/sessions/{session_id}/close` must include the `x-api-key` header. `/bridge/{session_id}` does not require this header because the Recall bot needs to open it directly.

The bridge page first tries to use `@ricky0123/vad-web` to send the gateway's required `audio-start` and `audio-end` events. If the CDN or model loading fails, it falls back to a simple RMS-based VAD.
