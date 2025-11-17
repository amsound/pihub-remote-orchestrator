
# PiHub Orchestrator (single process)

- Replaces Home Assistant integration with in-process orchestrator.
- FastAPI HTTP + SSE on port 8000 (LAN-only).
- Activity FSM: OFF, WATCH, LISTEN.
- Adapters: Samsung TV monitor (poll-based), KEF LSX (sim backend), Music Assistant (mock).
- Apple TV BLE/HID macros preserved.

## Endpoints
See `/api/*`: activity, volume, media, radio, source, config, state, events, healthz.

## Run
```bash
docker build -t pihub-orchestrator .
docker run --net=host -e TV_HOST=192.168.1.50 -e ROOM_NAME=living_room -v $(pwd)/data:/data pihub-orchestrator
```
