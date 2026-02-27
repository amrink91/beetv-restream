# BeeTV Restream Server

DASH restreamer for BeeTV.kz - downloads DASH segments via edge-token (307 redirect), serves as HTTP streams with a web panel.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/amrink91/beetv-restream.git
cd beetv-restream

# 2. Generate playlist (requires tokens from browser)
python3 data/beetv_parser.py

# 3. Run
docker-compose up -d

# 4. Open web panel
# http://localhost:8080
```

## Architecture

```
BeeTV CDN (ucdn.beetv.kz)
  |
  | 307 redirect -> edge with token
  v
Python DASH Downloader
  | downloads video+audio segments
  | refreshes token every 80s
  v
Flask HTTP Server (:8080)
  |
  |-- /                         -> Web Panel
  |-- /stream/{channel_id}      -> HTTP stream (fMP4)
  |-- /playlist.m3u             -> M3U playlist of all restreams
  |-- /api/channels             -> Channel list
  |-- /api/channels/{id}/start  -> Start channel
  |-- /api/channels/{id}/stop   -> Stop channel
  |-- /api/channels/{id}/status -> Channel status
  |-- /api/stats                -> Global stats
  |-- /api/start-all            -> Start all channels
  |-- /api/stop-all             -> Stop all channels
  |-- /api/reload               -> Reload M3U playlist
```

## Web Panel Features

- Channel list with real-time LIVE/OFF status
- One-click stream URL copy button
- Built-in video player
- Search and filter (All / Live / Off)
- Start All / Stop All / Reload controls
- M3U playlist export for all restreams
- Auto-refresh every 4 seconds
- Uptime, segments, errors, clients stats per channel

## Usage

### Watch in VLC
```bash
vlc http://localhost:8080/stream/000001514
```

### Record with ffmpeg
```bash
ffmpeg -i "http://localhost:8080/stream/000001514" -c copy output.mp4
```

### Download M3U playlist
```bash
curl -o restream.m3u http://localhost:8080/playlist.m3u
```

## Configuration (docker-compose.yml)

| Variable | Default | Description |
|----------|---------|-------------|
| HTTP_PROXY | http://192.168.30.63:3129 | Proxy for BeeTV access |
| M3U_PATH | /data/beetv_playlist.m3u | Path to source playlist |
| VIDEO_BW | 1087600 | Video quality (bandwidth) |
| AUTOSTART | false | Auto-start all channels |
| PORT | 8080 | Server port |

### Video Quality (VIDEO_BW)
- `562400` - 300x240 (minimum)
- `1087600` - 480x384 (recommended)
- `1612400` - 576x460
- `2137600` - 720x576 (maximum)

## Updating Playlist

1. Get fresh tokens from browser (F12 -> Network -> beetv.kz)
2. Edit `data/beetv_parser.py` - insert access_token and device_token
3. Run: `python3 data/beetv_parser.py`
4. Reload: `curl -X POST http://localhost:8080/api/reload`

## Requirements
- Docker + Docker Compose
- Access to proxy
- ~4GB RAM for 199 channels
