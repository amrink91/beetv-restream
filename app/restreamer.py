#!/usr/bin/env python3
"""
BeeTV DASH Restreamer
Скачивает DASH сегменты через edge-токен (307 redirect),
отдаёт по HTTP как MPEG-TS поток.
"""

import io
import logging
import os
import struct
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List
from urllib.parse import urljoin
import urllib.request
import urllib.error

log = logging.getLogger("restreamer")

# ============================================================
# Конфигурация
# ============================================================
PROXY = os.environ.get("HTTP_PROXY", "http://192.168.30.63:3129")
BASE_MPD_URL = "https://ucdn.beetv.kz/bpk-tv/{channel_id}/tve/index.mpd"
MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
TOKEN_REFRESH_SEC = 80
SEGMENT_BUFFER_SIZE = 30  # Хранить последние N сегментов в памяти
SEGMENT_SKIP_FROM_START = 3  # Пропускать первые N от начала timeline
SEGMENT_SKIP_FROM_END = 2  # Пропускать последние N (ещё не готовы)


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
# Простой MPEG-TS мультиплексор
# ============================================================
class SimpleTSMuxer:
    """
    Минимальный MPEG-TS мультиплексор.
    Оборачивает fMP4 сегменты (video H.264 + audio AAC) в MPEG-TS пакеты.
    
    Для простоты — используем ffmpeg subprocess для конвертации.
    """
    pass


# ============================================================
# DASH Stream — один канал
# ============================================================
@dataclass
class SegmentData:
    """Один загруженный сегмент"""
    timestamp: int
    data: bytes
    duration: float  # в секундах


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


class DASHChannel:
    """Один DASH канал — скачивает сегменты, буферизирует, отдаёт клиентам"""
    
    def __init__(self, channel_id: str, name: str = "", video_bw: int = 1087600):
        self.channel_id = channel_id
        self.name = name or channel_id
        self.video_bw = video_bw  # Качество видео
        
        # Состояние
        self.running = False
        self.edge_base_url = ""
        self.token_time = 0.0
        self.video = TrackInfo()
        self.audio = TrackInfo()
        self.video_init: Optional[bytes] = None
        self.audio_init: Optional[bytes] = None
        
        # Буфер сегментов (deque с ограничением)
        self.video_segments: deque = deque(maxlen=SEGMENT_BUFFER_SIZE)
        self.audio_segments: deque = deque(maxlen=SEGMENT_BUFFER_SIZE)
        self.last_video_t = 0
        self.last_audio_t = 0
        
        # Подписчики (клиенты слушающие поток)
        self._subscribers: List = []
        self._lock = threading.Lock()
        
        # Статистика
        self.stats = {
            "segments_downloaded": 0,
            "errors": 0,
            "last_error": "",
            "started_at": 0,
            "clients": 0,
        }
        
        self._thread: Optional[threading.Thread] = None
    
    def start(self):
        """Запустить загрузку канала"""
        if self.running:
            return
        self.running = True
        self.stats["started_at"] = time.time()
        self._thread = threading.Thread(target=self._download_loop, daemon=True, name=f"ch-{self.channel_id}")
        self._thread.start()
        log.info(f"[{self.name}] Started")
    
    def stop(self):
        """Остановить загрузку"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)
        log.info(f"[{self.name}] Stopped")
    
    def subscribe(self):
        """Подписаться на поток. Возвращает queue для получения сегментов."""
        import queue
        q = queue.Queue(maxsize=60)
        with self._lock:
            self._subscribers.append(q)
            self.stats["clients"] = len(self._subscribers)
        return q
    
    def unsubscribe(self, q):
        """Отписаться от потока."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
            self.stats["clients"] = len(self._subscribers)
    
    def _broadcast(self, data: bytes):
        """Отправить данные всем подписчикам"""
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
            self.stats["clients"] = len(self._subscribers)
    
    def _refresh_token(self):
        """Получить свежий edge URL с токеном"""
        try:
            mpd_url = BASE_MPD_URL.format(channel_id=self.channel_id)
            _, final_url = http_get(mpd_url)
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
        """Скачать и распарсить MPD"""
        try:
            mpd_url = f"{self.edge_base_url}/index.mpd"
            data = http_get_data(mpd_url)
            if not data:
                return False
            
            # Убираем BOM если есть
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
                    # Выбираем нужное качество
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
        """Скачать init-сегменты"""
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
        """Основной цикл загрузки сегментов"""
        while self.running:
            try:
                # Обновляем токен
                if not self._refresh_token():
                    time.sleep(5)
                    continue
                
                # Парсим MPD
                if not self._parse_mpd():
                    time.sleep(5)
                    continue
                
                # Скачиваем init если нужно
                if not self.video_init or not self.audio_init:
                    if not self._download_init_segments():
                        time.sleep(5)
                        continue
                
                # Определяем какие сегменты качать
                # Берём от start_t + skip до start_t + count - skip_end
                v_start_idx = SEGMENT_SKIP_FROM_START
                v_end_idx = self.video.seg_count - SEGMENT_SKIP_FROM_END
                
                a_start_idx = SEGMENT_SKIP_FROM_START  
                a_end_idx = self.audio.seg_count - SEGMENT_SKIP_FROM_END
                
                # Начинаем с середины timeline чтобы быть ближе к live edge
                # но не слишком близко (чтобы не получить 404)
                live_idx = max(v_start_idx, v_end_idx - 5)
                
                for seg_idx in range(live_idx, v_end_idx):
                    if not self.running:
                        break
                    
                    # Проверяем нужно ли обновить токен
                    if time.time() - self.token_time > TOKEN_REFRESH_SEC:
                        if not self._refresh_token():
                            break
                        if not self._parse_mpd():
                            break
                        # Пересчитываем индексы
                        v_end_idx = self.video.seg_count - SEGMENT_SKIP_FROM_END
                        a_end_idx = self.audio.seg_count - SEGMENT_SKIP_FROM_END
                    
                    vt = self.video.start_t + seg_idx * self.video.seg_duration
                    at = self.audio.start_t + seg_idx * self.audio.seg_duration
                    
                    # Пропускаем если уже скачали
                    if vt <= self.last_video_t:
                        continue
                    
                    # Скачиваем видео сегмент
                    v_url = self.video.segment_url(self.edge_base_url, vt)
                    v_data = http_get_data(v_url)
                    
                    # Скачиваем аудио сегмент
                    a_url = self.audio.segment_url(self.edge_base_url, at)
                    a_data = http_get_data(a_url)
                    
                    if v_data and a_data:
                        self.last_video_t = vt
                        self.last_audio_t = at
                        
                        # Сохраняем в буфер
                        seg_dur = self.video.seg_duration / self.video.timescale
                        self.video_segments.append(SegmentData(vt, v_data, seg_dur))
                        self.audio_segments.append(SegmentData(at, a_data, seg_dur))
                        
                        # Рассылаем подписчикам (сырые fMP4 сегменты)
                        self._broadcast(v_data + a_data)
                        
                        self.stats["segments_downloaded"] += 1
                        
                        # Ждём примерно длительность сегмента
                        time.sleep(seg_dur * 0.8)
                    else:
                        if not v_data:
                            log.warning(f"[{self.name}] Video 404: t={vt}")
                        if not a_data:
                            log.warning(f"[{self.name}] Audio 404: t={at}")
                        self.stats["errors"] += 1
                        time.sleep(0.5)
                
                # Дошли до конца timeline — обновляем MPD
                log.debug(f"[{self.name}] Timeline exhausted, refreshing...")
                time.sleep(1)
                
            except Exception as e:
                log.error(f"[{self.name}] Download loop error: {e}")
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:200]
                time.sleep(5)
    
    def get_status(self) -> dict:
        """Статус канала"""
        uptime = 0
        if self.stats["started_at"]:
            uptime = int(time.time() - self.stats["started_at"])
        
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "running": self.running,
            "video_repr": self.video.repr_id,
            "audio_repr": self.audio.repr_id,
            "segments": self.stats["segments_downloaded"],
            "errors": self.stats["errors"],
            "last_error": self.stats["last_error"],
            "clients": self.stats["clients"],
            "uptime_sec": uptime,
            "buffer_size": len(self.video_segments),
        }


# ============================================================
# Channel Manager
# ============================================================
class ChannelManager:
    """Управление всеми каналами"""
    
    def __init__(self):
        self.channels: Dict[str, DASHChannel] = {}
        self._lock = threading.Lock()
    
    def add_channel(self, channel_id: str, name: str = "", video_bw: int = 1087600, autostart: bool = True):
        """Добавить канал"""
        with self._lock:
            if channel_id not in self.channels:
                ch = DASHChannel(channel_id, name, video_bw)
                self.channels[channel_id] = ch
                if autostart:
                    ch.start()
                return ch
            return self.channels[channel_id]
    
    def remove_channel(self, channel_id: str):
        """Удалить канал"""
        with self._lock:
            ch = self.channels.pop(channel_id, None)
            if ch:
                ch.stop()
    
    def get_channel(self, channel_id: str) -> Optional[DASHChannel]:
        return self.channels.get(channel_id)
    
    def get_all_status(self) -> List[dict]:
        return [ch.get_status() for ch in self.channels.values()]
    
    def stop_all(self):
        for ch in self.channels.values():
            ch.stop()
    
    def load_from_m3u(self, m3u_path: str, autostart: bool = True, video_bw: int = 1087600):
        """Загрузить каналы из M3U плейлиста"""
        count = 0
        try:
            with open(m3u_path, "r", encoding="utf-8") as f:
                name = ""
                for line in f:
                    line = line.strip()
                    if line.startswith("#EXTINF:"):
                        # #EXTINF:-1 tvg-id="xxx" tvg-name="Имя", Имя
                        parts = line.split(",", 1)
                        name = parts[1].strip() if len(parts) > 1 else ""
                    elif line.startswith("https://"):
                        # https://ucdn.beetv.kz/bpk-tv/000001514/tve/index.mpd
                        channel_id = line.split("/bpk-tv/")[1].split("/")[0] if "/bpk-tv/" in line else ""
                        if channel_id:
                            self.add_channel(channel_id, name, video_bw, autostart=autostart)
                            count += 1
                        name = ""
            log.info(f"Loaded {count} channels from {m3u_path}")
        except Exception as e:
            log.error(f"Failed to load M3U: {e}")
        return count
