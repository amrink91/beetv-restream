#!/usr/bin/env python3
"""
BeeTV.kz IPTV M3U Parser
Парсит список каналов с beetv.kz и создает M3U плейлист
"""

import requests
import json
import time
from typing import List, Dict, Optional
from urllib.parse import urljoin

class BeeTVParser:
    def __init__(self, access_token: str, device_token: str):
        """
        Инициализация парсера
        
        Args:
            access_token: JWT токен для авторизации
            device_token: Токен устройства
        """
        self.base_url = "https://api.beetv.kz"
        self.access_token = access_token
        self.device_token = device_token
        
        # Базовые параметры для всех запросов
        self.base_params = {
            "client_id": "3e28685c-fce0-4994-9d3a-1dad2776e16a",
            "client_version": "4.4.6.311",
            "locale": "ru-KZ",
            "timezone": "18000"
        }
        
        # Заголовки для запросов
        self.headers = {
            "Accept": "application/json",
            "Accept-Language": "en,ru-RU;q=0.9,ru;q=0.8,en-US;q=0.7",
            "Access-Token": self.access_token,
            "Device-Token": self.device_token,
            "Origin": "https://beetv.kz",
            "Referer": "https://beetv.kz/",
            "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        }
        
    def get_all_channels(self) -> List[Dict]:
        """
        Получить список всех каналов
        
        Returns:
            Список словарей с информацией о каналах
        """
        url = f"{self.base_url}/v3/channels.json"
        params = {
            **self.base_params,
            "page[limit]": 500  # Максимум каналов за один запрос
        }
        
        try:
            print(f"📡 Запрашиваю список каналов...")
            response = requests.get(url, params=params, headers=self.headers, timeout=30)
            
            print(f"   Статус ответа: {response.status_code}")
            
            response.raise_for_status()
            
            # response.json() автоматически распакует gzip
            data = response.json()
            channels = data.get("data", [])
            
            print(f"✅ Получено {len(channels)} каналов")
            return channels
            
        except Exception as e:
            print(f"❌ Ошибка при получении списка каналов: {e}")
            print(f"   Content-Type: {response.headers.get('Content-Type', 'unknown')}")
            print(f"   Content-Encoding: {response.headers.get('Content-Encoding', 'none')}")
            return []
    
    def get_channel_stream_url(self, channel_id: str) -> Optional[str]:
        """
        Получить URL потока для конкретного канала
        
        Args:
            channel_id: ID канала
            
        Returns:
            URL .mpd потока или None в случае ошибки
        """
        url = f"{self.base_url}/v1/channels/{channel_id}/stream.json"
        params = {
            **self.base_params,
            "audio_codec": "mp4a",
            "video_codec": "h264",
            "protocol": "dash",
            "drm": "spbtvcas",
            "device_token": self.device_token,
            "screen_height": 911,
            "screen_width": 912
        }
        
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # URL находится в data.url
            stream_data = data.get("data", {})
            stream_url = stream_data.get("url")
            
            if stream_url:
                return stream_url
            else:
                print(f"⚠️  Канал {channel_id}: URL потока не найден в ответе")
                return None
                
        except requests.exceptions.RequestException as e:
            print(f"⚠️  Канал {channel_id}: Ошибка получения потока - {e}")
            return None
    
    def create_m3u_playlist(self, channels: List[Dict], output_file: str = "beetv_playlist.m3u"):
        """
        Создать M3U плейлист из списка каналов
        
        Args:
            channels: Список каналов
            output_file: Имя выходного файла
        """
        print(f"\n🎬 Начинаю создание M3U плейлиста...")
        
        # Открываем файл для записи
        with open(output_file, 'w', encoding='utf-8') as f:
            # Записываем заголовок M3U
            f.write("#EXTM3U\n")
            
            successful = 0
            failed = 0
            
            for i, channel in enumerate(channels, 1):
                channel_id = channel.get("id")
                channel_name = channel.get("name", "Неизвестный канал")
                channel_slug = channel.get("slug", "")
                is_free = channel.get("free", False)
                
                print(f"[{i}/{len(channels)}] Обрабатываю: {channel_name} {'🆓' if is_free else '💰'}...", end=" ")
                
                # Получаем URL потока
                stream_url = self.get_channel_stream_url(channel_id)
                
                if stream_url:
                    # Записываем информацию о канале в M3U формате
                    f.write(f'#EXTINF:-1 tvg-id="{channel_slug}" tvg-name="{channel_name}", {channel_name}\n')
                    f.write(f'{stream_url}\n')
                    
                    print(f"✅")
                    successful += 1
                else:
                    print(f"❌")
                    failed += 1
                
                # Небольшая задержка между запросами
                time.sleep(0.5)
        
        print(f"\n📊 Статистика:")
        print(f"   ✅ Успешно: {successful}")
        print(f"   ❌ Ошибок: {failed}")
        print(f"   📁 Файл сохранён: {output_file}")
    
    def parse_and_create_playlist(self, output_file: str = "beetv_playlist.m3u"):
        """
        Главная функция: парсинг и создание плейлиста
        
        Args:
            output_file: Имя выходного файла
        """
        print("=" * 60)
        print("🐝 BeeTV.kz IPTV Parser")
        print("=" * 60)
        
        # Получаем список каналов
        channels = self.get_all_channels()
        
        if not channels:
            print("❌ Не удалось получить список каналов")
            return
        
        # Создаём плейлист
        self.create_m3u_playlist(channels, output_file)
        
        print("\n✨ Готово!")


def main():
    """
    Главная функция для запуска парсера
    """
    # ВАЖНО: Замените эти значения на ваши токены из браузера
    ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJybmQiOiIxNjgwNmE4ZGVhM2ViMGQxZTc3MzRjZWJiMTVmNjRlNiIsInVzZXJfaWQiOiJmNjRlYzFmMi1mY2FiLTRiMjktYTJiNi01ZDFmOTc4MmI2OTYiLCJleHAiOjE3NjUyNzQyMjIsInRva2VuX2lkIjoiODNjZmNkZjAtZGNhMy00OGI1LTk5Y2UtNjk1YWZlMDlhY2JmIn0.t5ljnq1giXiqpXpsoeBIrS_nCDQPfhhQm7ZkbmZtr-c"
    DEVICE_TOKEN = "6dfba19a-024f-4c3d-85be-36dd5f819ea9"
    
    # Создаём парсер
    parser = BeeTVParser(
        access_token=ACCESS_TOKEN,
        device_token=DEVICE_TOKEN
    )
    
    # Запускаем парсинг
    parser.parse_and_create_playlist(output_file="beetv_playlist.m3u")


if __name__ == "__main__":
    main()
