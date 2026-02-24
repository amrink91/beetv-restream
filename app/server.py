#!/usr/bin/env python3
"""
BeeTV Restream Server
Flask app: веб-панель + HTTP потоки + API управления
"""

import json
import logging
import os
import queue
import signal
import sys
import time
from flask import Flask, Response, render_template, jsonify, request, send_from_directory

from restreamer import ChannelManager

# ============================================================
# Конфигурация
# ============================================================
M3U_PATH = os.environ.get("M3U_PATH", "/data/beetv_playlist.m3u")
VIDEO_BW = int(os.environ.get("VIDEO_BW", "1087600"))  # 480x384
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
app = Flask(__name__, 
            template_folder="/app/templates",
            static_folder="/app/static")

manager = ChannelManager()


# ============================================================
# Веб-панель
# ============================================================
@app.route("/")
def index():
    """Главная страница — веб-панель"""
    return render_template("index.html")


# ============================================================
# API
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


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Перезагрузить список каналов из M3U"""
    count = manager.load_from_m3u(M3U_PATH, autostart=AUTOSTART, video_bw=VIDEO_BW)
    return jsonify({"loaded": count})


# ============================================================
# HTTP Stream endpoint
# ============================================================
@app.route("/stream/<channel_id>")
def stream(channel_id):
    """
    HTTP поток канала в формате fMP4 сегментов.
    ffmpeg -i http://server:8080/stream/000001514 -c copy out.mp4
    """
    ch = manager.get_channel(channel_id)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    
    if not ch.running:
        return jsonify({"error": "channel not running"}), 503
    
    def generate():
        q = ch.subscribe()
        try:
            # Отправляем init сегменты если есть
            if ch.video_init:
                yield ch.video_init
            if ch.audio_init:
                yield ch.audio_init
            
            while ch.running:
                try:
                    data = q.get(timeout=5)
                    yield data
                except queue.Empty:
                    # Keepalive — пустой пакет
                    continue
                except GeneratorExit:
                    break
        finally:
            ch.unsubscribe(q)
    
    return Response(
        generate(),
        mimetype="video/mp4",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Channel-Id": channel_id,
            "X-Channel-Name": ch.name,
        }
    )


@app.route("/stream/<channel_id>/mpegts")
def stream_mpegts(channel_id):
    """
    HTTP поток в MPEG-TS формате (через ffmpeg транскодинг на лету).
    Совместим с ffmpeg -i, VLC, и стандартными скриптами записи.
    """
    ch = manager.get_channel(channel_id)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    
    if not ch.running:
        return jsonify({"error": "channel not running"}), 503
    
    # TODO: запускать ffmpeg subprocess для конвертации fMP4 → MPEG-TS
    # Пока отдаём сырые fMP4 сегменты
    return stream(channel_id)


# ============================================================
# Startup
# ============================================================
def start_server():
    """Запуск сервера"""
    log.info("=" * 60)
    log.info("BeeTV Restream Server")
    log.info(f"M3U: {M3U_PATH}")
    log.info(f"Video BW: {VIDEO_BW}")
    log.info(f"Autostart: {AUTOSTART}")
    log.info("=" * 60)
    
    # Загружаем каналы
    if os.path.exists(M3U_PATH):
        manager.load_from_m3u(M3U_PATH, autostart=AUTOSTART, video_bw=VIDEO_BW)
    else:
        log.warning(f"M3U file not found: {M3U_PATH}")
        log.info("Use API POST /api/reload after placing M3U file")
    
    # Graceful shutdown
    def shutdown(signum, frame):
        log.info("Shutting down...")
        manager.stop_all()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    # Запуск Flask
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    start_server()
