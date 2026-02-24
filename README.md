# BeeTV Restream Server

DASH рестример для BeeTV.kz — скачивает DASH сегменты через edge-токен (307 redirect), отдаёт по HTTP.

## Быстрый старт

```bash
# 1. Клонируем
git clone https://github.com/YOUR_REPO/beetv-restream.git
cd beetv-restream

# 2. Кладём плейлист
mkdir -p data
cp /path/to/beetv_playlist.m3u data/

# 3. Запускаем
docker-compose up -d

# 4. Открываем панель
# http://192.168.30.3:8080
```

## Архитектура

```
BeeTV CDN (ucdn.beetv.kz)
  │
  │ 307 redirect → edge с токеном
  │
  ▼
Python DASH Downloader
  │ скачивает video+audio сегменты
  │ обновляет токен каждые 80с
  │
  ▼
Flask HTTP Server (:8080)
  │
  ├── /                      → Веб-панель
  ├── /stream/{channel_id}   → HTTP поток (fMP4)
  ├── /api/channels          → Список каналов
  ├── /api/channels/{id}/start  → Запустить канал
  ├── /api/channels/{id}/stop   → Остановить канал
  └── /api/stats             → Статистика
```

## Использование

### Веб-панель
Открыть `http://192.168.30.3:8080` — поиск каналов, просмотр, копирование URL.

### Запись через ffmpeg
```bash
ffmpeg -i "http://192.168.30.3:8080/stream/000001514" \
  -c copy -y output.mp4
```

### Запись с drawtext (как для остальных каналов)
```bash
ffmpeg -i "http://192.168.30.3:8080/stream/000001514" \
  -vf "drawtext=fontfile=/usr/share/fonts/Vera.ttf:text='%{localtime}':fontsize=17:x=(w-text_w)/2:y=h-line_h:box=1:boxcolor=white@0.5:boxborderw=5" \
  -vcodec libx264 -preset veryfast -r 25 -b:v 320k -s 480x360 \
  -acodec mp3 -b:a 60k \
  output.mp4
```

## Конфигурация (docker-compose.yml)

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| HTTP_PROXY | http://192.168.30.63:3129 | Прокси для доступа к BeeTV |
| M3U_PATH | /data/beetv_playlist.m3u | Путь к плейлисту |
| VIDEO_BW | 1087600 | Качество видео (bandwidth) |
| AUTOSTART | false | Автозапуск всех каналов |
| PORT | 8080 | Порт сервера |

### Качество видео (VIDEO_BW)
- `562400` — 300x240 (минимум)
- `1087600` — 480x384 (рекомендуется)
- `1612400` — 576x460
- `2137600` — 720x576 (максимум)

## Обновление плейлиста

1. Получить свежие токены из браузера (F12 → Network → beetv.kz)
2. Отредактировать `data/beetv_parser.py` — вставить access_token и device_token
3. Запустить: `python3 data/beetv_parser.py`
4. API: `curl -X POST http://localhost:8080/api/reload`

## Требования
- Docker + Docker Compose
- Доступ к прокси 192.168.30.63:3129
- ~4GB RAM для 199 каналов
