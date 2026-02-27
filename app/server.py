#!/usr/bin/env python3
"""
BeeTV Restream Server
Flask app: веб-панель + HTTP потоки + API управления
"""

import logging
import os
import queue
import signal
import sys
import time
from flask import Flask, Response, render_template, jsonify, request

from restreamer import ChannelManager

# ============================================================
# Конфигурация
# ============================================================
M3U_PATH = os.environ.get("M3U_PATH", "/data/beetv_playlist.m3u")
VIDEO_BW = int(os.environ.get("VIDEO_BW", "1087600"))
AUTOSTART = os.environ.get("AUTOSTART", "true").lower() == "true"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("server")

# ============================================================
# Flask App
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))

manager = ChannelManager()


# ============================================================
# Веб-панель
# ============================================================
@app.route("/")
def index():
    """Главная страница — веб-панель"""
    return render_template("index.html")


# ============================================================
# API — Каналы
# ============================================================
@app.route("/api/channels")
def api_channels():
    """Список всех каналов со статусом"""
    return jsonify(manager.get_all_status())


@app.route("/api/channels/<channel_id>/start", methods=["POST"])
def api_start_channel(channel_id):
    """Запустить канал"""
    ch = manager.get_channel(channel_id)
    if ch:
        ch.start()
        return jsonify({"status": "started", "channel_id": channel_id})
    return jsonify({"error": "channel not found"}), 404


@app.route("/api/channels/<channel_id>/stop", methods=["POST"])
def api_stop_channel(channel_id):
    """Остановить канал"""
    ch = manager.get_channel(channel_id)
    if ch:
        ch.stop()
        return jsonify({"status": "stopped", "channel_id": channel_id})
    return jsonify({"error": "channel not found"}), 404


@app.route("/api/channels/<channel_id>/status")
def api_channel_status(channel_id):
    """Статус одного канала"""
    ch = manager.get_channel(channel_id)
    if ch:
        return jsonify(ch.get_status())
    return jsonify({"error": "channel not found"}), 404


@app.route("/api/stats")
def api_stats():
    """Общая статистика"""
    statuses = manager.get_all_status()
    total_segs = sum(s["segments"] for s in statuses)
    total_errors = sum(s["errors"] for s in statuses)
    running = sum(1 for s in statuses if s["running"])
    total_clients = sum(s["clients"] for s in statuses)
    return jsonify({
        "total_channels": len(statuses),
        "running_channels": running,
        "total_segments": total_segs,
        "total_errors": total_errors,
        "total_clients": total_clients,
    })


# ============================================================
# API — Управление
# ============================================================
@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Перезагрузить список каналов из M3U"""
    count = manager.load_from_m3u(M3U_PATH, autostart=AUTOSTART, video_bw=VIDEO_BW)
    return jsonify({"loaded": count})


@app.route("/api/start-all", methods=["POST"])
def api_start_all():
    """Запустить все каналы"""
    started = 0
    for ch in manager.channels.values():
        if not ch.running:
            ch.start()
            started += 1
    return jsonify({"started": started})


@app.route("/api/stop-all", methods=["POST"])
def api_stop_all():
    """Остановить все каналы"""
    stopped = 0
    for ch in manager.channels.values():
        if ch.running:
            ch.stop()
            stopped += 1
    return jsonify({"stopped": stopped})


# ============================================================
# M3U плейлист рестримов
# ============================================================
@app.route("/playlist.m3u")
def playlist_m3u():
    """Сгенерировать M3U плейлист со всеми рестрим-ссылками"""
    host = request.host
    scheme = request.scheme
    base = f"{scheme}://{host}"

    lines = ["#EXTM3U"]
    for ch in manager.channels.values():
        lines.append(f'#EXTINF:-1 tvg-id="{ch.channel_id}" tvg-name="{ch.name}",{ch.name}')
        lines.append(f"{base}/stream/{ch.channel_id}")

    return Response(
        "\n".join(lines) + "\n",
        mimetype="audio/x-mpegurl",
        headers={"Content-Disposition": "attachment; filename=beetv_restream.m3u"},
    )


# ============================================================
# HTTP Stream endpoint
# ============================================================
@app.route("/stream/<channel_id>")
def stream(channel_id):
    """
    HTTP поток канала.
    DASH каналы: fMP4 (video/mp4)
    HLS каналы: MPEG-TS (video/mp2t)
    ffmpeg -i http://server:8080/stream/000001514 -c copy out.mp4
    """
    ch = manager.get_channel(channel_id)
    if not ch:
        return jsonify({"error": "channel not found"}), 404

    if not ch.running:
        return jsonify({"error": "channel not running, start it first"}), 503

    # Выбираем mimetype в зависимости от типа потока
    is_hls = ch.stream_type == "hls"
    mime = "video/mp2t" if is_hls else "video/mp4"

    def generate():
        q = ch.subscribe()
        try:
            # Init сегменты только для DASH (fMP4)
            if not is_hls:
                if ch.video_init:
                    yield ch.video_init
                if ch.audio_init:
                    yield ch.audio_init

            while ch.running:
                try:
                    data = q.get(timeout=5)
                    yield data
                except queue.Empty:
                    continue
                except GeneratorExit:
                    break
        finally:
            ch.unsubscribe(q)

    return Response(
        generate(),
        mimetype=mime,
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Channel-Id": channel_id,
            "X-Channel-Name": ch.name,
            "X-Stream-Type": ch.stream_type,
        }
    )


# ============================================================
# Startup — загрузка каналов при импорте (для Gunicorn)
# ============================================================
def _load_channels():
    """Загрузить каналы из M3U при старте"""
    log.info("=" * 60)
    log.info("BeeTV Restream Server")
    log.info(f"M3U: {M3U_PATH}")
    log.info(f"Video BW: {VIDEO_BW}")
    log.info(f"Autostart: {AUTOSTART}")
    log.info("=" * 60)

    if os.path.exists(M3U_PATH):
        manager.load_from_m3u(M3U_PATH, autostart=AUTOSTART, video_bw=VIDEO_BW)
    else:
        log.warning(f"M3U file not found: {M3U_PATH}")
        log.info("Use API POST /api/reload after placing M3U file")


# Загружаем каналы сразу при импорте модуля (работает и с Gunicorn, и напрямую)
_load_channels()


# Graceful shutdown
def _shutdown(signum, frame):
    log.info("Shutting down...")
    manager.stop_all()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
