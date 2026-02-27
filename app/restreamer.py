#!/usr/bin/env python3
"""
BeeTV DASH + HLS Restreamer
Скачивает DASH/HLS сегменты через edge-токен (307 redirect),
отдаёт по HTTP.
"""

import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List
from urllib.parse import urljoin
import urllib.request
import urllib.error

log = logging.getLogger("restreamer")

# ============================================================
# Конфигурация
# ============================================================
PROXY = os.environ.get("HTTP_PROXY", "http://192.168.30.63:3129")
BASE_URL_TPL = "https://ucdn.beetv.kz/bpk-tv/{channel_id}/tve/{index_file}"
MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
TOKEN_REFRESH_SEC = 80
SEGMENT_BUFFER_SIZE = 30
SEGMENT_SKIP_FROM_START = 3
SEGMENT_SKIP_FROM_END = 2


def ns(tag):
    return f"{{{MPD_NS}}}{tag}"


# ============================================================
# HTTP с прокси
# ============================================================
_opener = None


def get_opener():
    global _opener
    if _opener is None:
        proxy_handler = urllib.request.ProxyHandler({
            "http": PROXY,
            "https": PROXY,
        })
        _opener = urllib.request.build_opener(proxy_handler)
    return _opener


def http_get(url: str, timeout: int = 15) -> Tuple[bytes, str]:
    """HTTP GET через прокси. Возвращает (data, final_url)."""
    req = urllib.request.Request(url, headers={"User-Agent": "BeeTV-Restreamer/1.0"})
    resp = get_opener().open(req, timeout=timeout)
    return resp.read(), resp.geturl()


def http_get_data(url: str, timeout: int = 10) -> Optional[bytes]:
    """Скачать данные, None при ошибке."""
    try:
        data, _ = http_get(url, timeout=timeout)
        return data
    except Exception as e:
        log.warning(f"Download failed: {str(e)[:100]}")
        return None


# ============================================================
# Базовые классы
# ============================================================
@dataclass
class SegmentData:
    """Один загруженный сегмент"""
    timestamp: int
    data: bytes
    duration: float


class BaseChannel:
    """Общий интерфейс для DASH и HLS каналов"""

    def __init__(self, channel_id: str, name: str = "", stream_type: str = "dash"):
        self.channel_id = channel_id
        self.name = name or channel_id
        self.stream_type = stream_type
        self.running = False
        self.video_init: Optional[bytes] = None
        self.audio_init: Optional[bytes] = None
        self._subscribers: List = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.stats = {
            "segments_downloaded": 0,
            "errors": 0,
            "last_error": "",
            "started_at": 0,
            "clients": 0,
        }

    def start(self):
        if self.running:
            return
        self.running = True
        self.stats["started_at"] = time.time()
        self._thread = threading.Thread(
            target=self._download_loop, daemon=True, name=f"ch-{self.channel_id}"
        )
        self._thread.start()
        log.info(f"[{self.name}] Started ({self.stream_type})")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)
        log.info(f"[{self.name}] Stopped")

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=60)
        with self._lock:
            self._subscribers.append(q)
            self.stats["clients"] = len(self._subscribers)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
            self.stats["clients"] = len(self._subscribers)

    def _broadcast(self, data: bytes):
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
            self.stats["clients"] = len(self._subscribers)

    def _download_loop(self):
        raise NotImplementedError

    def get_status(self) -> dict:
        uptime = 0
        if self.stats["started_at"]:
            uptime = int(time.time() - self.stats["started_at"])
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "running": self.running,
            "stream_type": self.stream_type,
            "video_repr": "",
            "audio_repr": "",
            "segments": self.stats["segments_downloaded"],
            "errors": self.stats["errors"],
            "last_error": self.stats["last_error"],
            "clients": self.stats["clients"],
            "uptime_sec": uptime,
            "buffer_size": 0,
        }


# ============================================================
# DASH канал
# ============================================================
@dataclass
class TrackInfo:
    repr_id: str = ""
    timescale: int = 0
    seg_duration: int = 0
    start_t: int = 0
    seg_count: int = 0
    init_template: str = ""
    media_template: str = ""
    channel_id: str = ""

    def init_url(self, base: str) -> str:
        name = self.init_template.replace("$RepresentationID$", self.repr_id)
        return f"{base}/dash/{name}"

    def segment_url(self, base: str, t: int) -> str:
        name = self.media_template.replace("$RepresentationID$", self.repr_id)
        name = name.replace("$Time$", str(t))
        return f"{base}/dash/{name}"


class DASHChannel(BaseChannel):
    """DASH канал — скачивает fMP4 сегменты"""

    def __init__(self, channel_id: str, name: str = "", video_bw: int = 1087600):
        super().__init__(channel_id, name, stream_type="dash")
        self.video_bw = video_bw
        self.edge_base_url = ""
        self.token_time = 0.0
        self.video = TrackInfo()
        self.audio = TrackInfo()
        self.video_segments: deque = deque(maxlen=SEGMENT_BUFFER_SIZE)
        self.audio_segments: deque = deque(maxlen=SEGMENT_BUFFER_SIZE)
        self.last_video_t = 0
        self.last_audio_t = 0

    def _refresh_token(self):
        try:
            url = BASE_URL_TPL.format(channel_id=self.channel_id, index_file="index.mpd")
            _, final_url = http_get(url)
            self.edge_base_url = final_url.rsplit("/index.mpd", 1)[0]
            self.token_time = time.time()
            log.debug(f"[{self.name}] Token refreshed: {self.edge_base_url[:80]}...")
            return True
        except Exception as e:
            log.error(f"[{self.name}] Token refresh failed: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)[:200]
            return False

    def _parse_mpd(self) -> bool:
        try:
            data = http_get_data(f"{self.edge_base_url}/index.mpd")
            if not data:
                return False
            if data[:3] == b'\xef\xbb\xbf':
                data = data[3:]

            root = ET.fromstring(data)

            for adapt in root.findall(f".//{ns('AdaptationSet')}"):
                content_type = adapt.get("contentType", "")
                seg_tpl = adapt.find(ns("SegmentTemplate"))
                if seg_tpl is None:
                    continue

                timescale = int(seg_tpl.get("timescale", "1"))
                init_tpl = seg_tpl.get("initialization", "")
                media_tpl = seg_tpl.get("media", "")

                timeline = seg_tpl.find(ns("SegmentTimeline"))
                if timeline is None:
                    continue

                s_elem = timeline.find(ns("S"))
                if s_elem is None:
                    continue

                start_t = int(s_elem.get("t", "0"))
                duration = int(s_elem.get("d", "0"))
                repeat = int(s_elem.get("r", "0"))

                if content_type == "audio":
                    repr_elem = adapt.find(ns("Representation"))
                    self.audio = TrackInfo(
                        repr_id=repr_elem.get("id", "") if repr_elem is not None else "",
                        timescale=timescale,
                        seg_duration=duration,
                        start_t=start_t,
                        seg_count=repeat + 1,
                        init_template=init_tpl,
                        media_template=media_tpl,
                        channel_id=self.channel_id,
                    )
                elif content_type == "video":
                    chosen_repr = None
                    for r in adapt.findall(ns("Representation")):
                        bw = int(r.get("bandwidth", "0"))
                        if bw <= self.video_bw:
                            chosen_repr = r
                    if chosen_repr is None:
                        chosen_repr = adapt.find(ns("Representation"))

                    self.video = TrackInfo(
                        repr_id=chosen_repr.get("id", "") if chosen_repr is not None else "",
                        timescale=timescale,
                        seg_duration=duration,
                        start_t=start_t,
                        seg_count=repeat + 1,
                        init_template=init_tpl,
                        media_template=media_tpl,
                        channel_id=self.channel_id,
                    )

            return bool(self.video.repr_id and self.audio.repr_id)
        except Exception as e:
            log.error(f"[{self.name}] MPD parse error: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)[:200]
            return False

    def _download_init_segments(self) -> bool:
        try:
            self.video_init = http_get_data(self.video.init_url(self.edge_base_url))
            self.audio_init = http_get_data(self.audio.init_url(self.edge_base_url))
            if self.video_init and self.audio_init:
                log.info(f"[{self.name}] Init segments: video={len(self.video_init)}b audio={len(self.audio_init)}b")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] Init download failed: {e}")
            return False

    def _download_loop(self):
        while self.running:
            try:
                if not self._refresh_token():
                    time.sleep(5)
                    continue
                if not self._parse_mpd():
                    time.sleep(5)
                    continue
                if not self.video_init or not self.audio_init:
                    if not self._download_init_segments():
                        time.sleep(5)
                        continue

                v_start_idx = SEGMENT_SKIP_FROM_START
                v_end_idx = self.video.seg_count - SEGMENT_SKIP_FROM_END
                live_idx = max(v_start_idx, v_end_idx - 5)

                for seg_idx in range(live_idx, v_end_idx):
                    if not self.running:
                        break

                    if time.time() - self.token_time > TOKEN_REFRESH_SEC:
                        if not self._refresh_token():
                            break
                        if not self._parse_mpd():
                            break
                        v_end_idx = self.video.seg_count - SEGMENT_SKIP_FROM_END

                    vt = self.video.start_t + seg_idx * self.video.seg_duration
                    at = self.audio.start_t + seg_idx * self.audio.seg_duration

                    if vt <= self.last_video_t:
                        continue

                    v_data = http_get_data(self.video.segment_url(self.edge_base_url, vt))
                    a_data = http_get_data(self.audio.segment_url(self.edge_base_url, at))

                    if v_data and a_data:
                        self.last_video_t = vt
                        self.last_audio_t = at
                        seg_dur = self.video.seg_duration / self.video.timescale
                        self.video_segments.append(SegmentData(vt, v_data, seg_dur))
                        self.audio_segments.append(SegmentData(at, a_data, seg_dur))
                        self._broadcast(v_data + a_data)
                        self.stats["segments_downloaded"] += 1
                        time.sleep(seg_dur * 0.8)
                    else:
                        self.stats["errors"] += 1
                        time.sleep(0.5)

                log.debug(f"[{self.name}] Timeline exhausted, refreshing...")
                time.sleep(1)

            except Exception as e:
                log.error(f"[{self.name}] Download loop error: {e}")
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:200]
                time.sleep(5)

    def get_status(self) -> dict:
        status = super().get_status()
        status["video_repr"] = self.video.repr_id
        status["audio_repr"] = self.audio.repr_id
        status["buffer_size"] = len(self.video_segments)
        return status


# ============================================================
# HLS канал
# ============================================================
class HLSChannel(BaseChannel):
    """HLS канал — скачивает TS сегменты из m3u8 плейлиста"""

    def __init__(self, channel_id: str, name: str = ""):
        super().__init__(channel_id, name, stream_type="hls")
        self.edge_base_url = ""
        self.token_time = 0.0
        self.last_seq = -1
        self.segments: deque = deque(maxlen=SEGMENT_BUFFER_SIZE)
        self.target_duration = 6.0

    def _refresh_token(self) -> bool:
        """Получить свежий edge URL с токеном через 307 redirect"""
        try:
            url = BASE_URL_TPL.format(channel_id=self.channel_id, index_file="index.m3u8")
            data, final_url = http_get(url)
            self.edge_base_url = final_url.rsplit("/index.m3u8", 1)[0]
            self.token_time = time.time()
            log.debug(f"[{self.name}] HLS token refreshed: {self.edge_base_url[:80]}...")
            return True
        except Exception as e:
            log.error(f"[{self.name}] HLS token refresh failed: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)[:200]
            return False

    def _parse_m3u8(self) -> list:
        """Скачать и распарсить m3u8 плейлист. Возвращает список (seq, duration, url)."""
        try:
            m3u8_url = f"{self.edge_base_url}/index.m3u8"
            data = http_get_data(m3u8_url)
            if not data:
                return []

            text = data.decode("utf-8", errors="replace")
            lines = text.strip().splitlines()

            # Ищем медиа-sequence
            media_seq = 0
            target_dur = 6.0
            segments = []
            cur_duration = 0.0

            for line in lines:
                line = line.strip()
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    media_seq = int(line.split(":")[1])
                elif line.startswith("#EXT-X-TARGETDURATION:"):
                    target_dur = float(line.split(":")[1])
                    self.target_duration = target_dur
                elif line.startswith("#EXTINF:"):
                    # #EXTINF:6.000, or #EXTINF:6.000
                    dur_str = line.split(":")[1].rstrip(",")
                    try:
                        cur_duration = float(dur_str)
                    except ValueError:
                        cur_duration = target_dur
                elif line and not line.startswith("#"):
                    # Это URL сегмента (может быть относительным)
                    if line.startswith("http"):
                        seg_url = line
                    else:
                        seg_url = f"{self.edge_base_url}/{line}"
                    segments.append((media_seq, cur_duration, seg_url))
                    media_seq += 1
                    cur_duration = 0.0

            # Если это master playlist (содержит #EXT-X-STREAM-INF) — нужно выбрать вариант
            if not segments:
                for line in lines:
                    if line.strip() and not line.strip().startswith("#"):
                        # Это вложенный плейлист — качество
                        variant_url = line.strip()
                        if not variant_url.startswith("http"):
                            variant_url = f"{self.edge_base_url}/{variant_url}"
                        # Рекурсивно парсим вариант
                        return self._parse_variant_m3u8(variant_url)

            return segments
        except Exception as e:
            log.error(f"[{self.name}] m3u8 parse error: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)[:200]
            return []

    def _parse_variant_m3u8(self, url: str) -> list:
        """Парсим вложенный m3u8 (variant/media playlist)"""
        try:
            data = http_get_data(url)
            if not data:
                return []
            text = data.decode("utf-8", errors="replace")
            lines = text.strip().splitlines()

            base_url = url.rsplit("/", 1)[0]
            media_seq = 0
            segments = []
            cur_duration = 0.0

            for line in lines:
                line = line.strip()
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    media_seq = int(line.split(":")[1])
                elif line.startswith("#EXT-X-TARGETDURATION:"):
                    self.target_duration = float(line.split(":")[1])
                elif line.startswith("#EXTINF:"):
                    dur_str = line.split(":")[1].rstrip(",")
                    try:
                        cur_duration = float(dur_str)
                    except ValueError:
                        cur_duration = self.target_duration
                elif line and not line.startswith("#"):
                    if line.startswith("http"):
                        seg_url = line
                    else:
                        seg_url = f"{base_url}/{line}"
                    segments.append((media_seq, cur_duration, seg_url))
                    media_seq += 1
                    cur_duration = 0.0

            return segments
        except Exception as e:
            log.error(f"[{self.name}] Variant m3u8 parse error: {e}")
            return []

    def _download_loop(self):
        """Основной цикл загрузки HLS сегментов"""
        while self.running:
            try:
                # Обновляем токен
                if time.time() - self.token_time > TOKEN_REFRESH_SEC or not self.edge_base_url:
                    if not self._refresh_token():
                        time.sleep(5)
                        continue

                # Парсим m3u8
                segments = self._parse_m3u8()
                if not segments:
                    time.sleep(3)
                    continue

                # Скачиваем только новые сегменты
                new_count = 0
                for seq, duration, seg_url in segments:
                    if not self.running:
                        break

                    # Пропускаем уже скачанные
                    if seq <= self.last_seq:
                        continue

                    seg_data = http_get_data(seg_url, timeout=15)
                    if seg_data:
                        self.last_seq = seq
                        self.segments.append(SegmentData(seq, seg_data, duration))
                        self._broadcast(seg_data)
                        self.stats["segments_downloaded"] += 1
                        new_count += 1
                    else:
                        self.stats["errors"] += 1
                        log.warning(f"[{self.name}] HLS segment failed: seq={seq}")

                # Ждём перед следующим fetch
                # Если скачали сегменты — ждём ~target_duration
                # Если нет новых — ждём половину target_duration
                if new_count > 0:
                    time.sleep(self.target_duration * 0.8)
                else:
                    time.sleep(self.target_duration * 0.4)

            except Exception as e:
                log.error(f"[{self.name}] HLS download error: {e}")
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:200]
                time.sleep(5)

    def get_status(self) -> dict:
        status = super().get_status()
        status["buffer_size"] = len(self.segments)
        return status


# ============================================================
# Channel Manager
# ============================================================
class ChannelManager:
    """Управление всеми каналами (DASH + HLS)"""

    def __init__(self):
        self.channels: Dict[str, BaseChannel] = {}
        self._lock = threading.Lock()

    def add_channel(self, channel_id: str, name: str = "", video_bw: int = 1087600,
                    autostart: bool = True, stream_type: str = "dash"):
        """Добавить канал (автодетект DASH или HLS)"""
        with self._lock:
            if channel_id not in self.channels:
                if stream_type == "hls":
                    ch = HLSChannel(channel_id, name)
                else:
                    ch = DASHChannel(channel_id, name, video_bw)
                self.channels[channel_id] = ch
                if autostart:
                    ch.start()
                return ch
            return self.channels[channel_id]

    def remove_channel(self, channel_id: str):
        with self._lock:
            ch = self.channels.pop(channel_id, None)
            if ch:
                ch.stop()

    def get_channel(self, channel_id: str) -> Optional[BaseChannel]:
        return self.channels.get(channel_id)

    def get_all_status(self) -> List[dict]:
        return [ch.get_status() for ch in self.channels.values()]

    def stop_all(self):
        for ch in self.channels.values():
            ch.stop()

    def load_from_m3u(self, m3u_path: str, autostart: bool = True, video_bw: int = 1087600):
        """Загрузить каналы из M3U плейлиста (автодетект DASH/HLS)"""
        count = 0
        try:
            with open(m3u_path, "r", encoding="utf-8") as f:
                name = ""
                for line in f:
                    line = line.strip()
                    if line.startswith("#EXTINF:"):
                        parts = line.split(",", 1)
                        name = parts[1].strip() if len(parts) > 1 else ""
                    elif line.startswith("https://"):
                        channel_id = line.split("/bpk-tv/")[1].split("/")[0] if "/bpk-tv/" in line else ""
                        if channel_id:
                            # Автодетект: .m3u8 = HLS, .mpd = DASH
                            if ".m3u8" in line:
                                stream_type = "hls"
                            else:
                                stream_type = "dash"
                            self.add_channel(channel_id, name, video_bw,
                                             autostart=autostart, stream_type=stream_type)
                            count += 1
                        name = ""
            log.info(f"Loaded {count} channels from {m3u_path}")
        except Exception as e:
            log.error(f"Failed to load M3U: {e}")
        return count
