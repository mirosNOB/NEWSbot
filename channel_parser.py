from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
import os
import json
from datetime import datetime, timedelta
import asyncio
from config import CHANNELS_DIR

# Данные для подключения
API_ID = 28844154
API_HASH = '8051adc2a5f8ea73f44dba3b4cadd44c'
SESSION_NAME = 'channel_parser'

class ChannelParser:
    def __init__(self):
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        self.channels = self._load_channels()
        self.request_delay = 1  # Задержка между запросами в секундах
        
    def _load_channels(self):
        """Загрузка списка каналов из файла"""
        channels_file = os.path.join(CHANNELS_DIR, 'channels.json')
        if os.path.exists(channels_file):
            with open(channels_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def save_channels(self):
        """Сохранение списка каналов в файл"""
        channels_file = os.path.join(CHANNELS_DIR, 'channels.json')
        with open(channels_file, 'w', encoding='utf-8') as f:
            json.dump(self.channels, f, ensure_ascii=False, indent=2)

    async def add_channel(self, channel_link):
        """Добавление нового канала для мониторинга"""
        try:
            if not self.client.is_connected():
                await self.start()
                
            channel = await self.client.get_entity(channel_link)
            self.channels[str(channel.id)] = {
                'title': channel.title,
                'username': channel.username,
                'link': channel_link,
                'last_parsed': None
            }
            self.save_channels()
            return True, f"Канал {channel.title} успешно добавлен"
        except Exception as e:
            return False, f"Ошибка при добавлении канала: {str(e)}"

    async def remove_channel(self, channel_id):
        """Удаление канала из мониторинга"""
        if channel_id in self.channels:
            channel_info = self.channels.pop(channel_id)
            self.save_channels()
            return True, f"Канал {channel_info['title']} удален"
        return False, "Канал не найден"

    async def parse_channel(self, channel_id, days=1):
        """Парсинг сообщений из канала за последние N дней"""
        if channel_id not in self.channels:
            return False, "Канал не найден"

        try:
            if not self.client.is_connected():
                await self.start()
                
            channel = await self.client.get_entity(self.channels[channel_id]['link'])
            
            # Получаем дату, с которой начинаем парсинг
            date_from = datetime.now() - timedelta(days=days)
            
            # Создаем директорию для сохранения данных канала
            channel_dir = os.path.join(CHANNELS_DIR, str(channel_id))
            os.makedirs(channel_dir, exist_ok=True)
            
            messages = []
            try:
                async for message in self.client.iter_messages(channel, offset_date=date_from, limit=100):
                    if message.text:
                        messages.append({
                            'id': message.id,
                            'date': message.date.isoformat(),
                            'text': message.text,
                            'views': getattr(message, 'views', 0),
                            'forwards': getattr(message, 'forwards', 0)
                        })
                    # Добавляем задержку между запросами
                    await asyncio.sleep(self.request_delay)
            except Exception as e:
                if "flood wait" in str(e).lower():
                    # Если получили флуд-контроль, увеличиваем задержку
                    wait_time = int(str(e).split('of ')[1].split(' ')[0])
                    self.request_delay = min(self.request_delay * 2, 5)  # Увеличиваем задержку, но не больше 5 секунд
                    return False, f"Слишком много запросов. Попробуйте через {wait_time} секунд"
                else:
                    raise e

            # Сохраняем результаты
            output_file = os.path.join(channel_dir, f"messages_{datetime.now().strftime('%Y%m%d')}.json")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

            # Обновляем время последнего парсинга
            self.channels[channel_id]['last_parsed'] = datetime.now().isoformat()
            self.save_channels()

            return True, f"Собрано {len(messages)} сообщений из канала {self.channels[channel_id]['title']}"

        except Exception as e:
            return False, f"Ошибка при парсинге канала: {str(e)}"

    async def get_channel_stats(self, channel_id):
        """Получение статистики по каналу"""
        if channel_id not in self.channels:
            return False, "Канал не найден"

        channel_dir = os.path.join(CHANNELS_DIR, str(channel_id))
        if not os.path.exists(channel_dir):
            return False, "Данные канала не найдены"

        stats = {
            'total_messages': 0,
            'total_views': 0,
            'total_forwards': 0,
            'average_views': 0,
            'average_forwards': 0
        }

        # Собираем статистику из всех файлов канала
        for filename in os.listdir(channel_dir):
            if filename.endswith('.json'):
                with open(os.path.join(channel_dir, filename), 'r', encoding='utf-8') as f:
                    messages = json.load(f)
                    stats['total_messages'] += len(messages)
                    for msg in messages:
                        stats['total_views'] += msg.get('views', 0)
                        stats['total_forwards'] += msg.get('forwards', 0)

        if stats['total_messages'] > 0:
            stats['average_views'] = stats['total_views'] / stats['total_messages']
            stats['average_forwards'] = stats['total_forwards'] / stats['total_messages']

        return True, stats

    async def start(self):
        """Запуск клиента"""
        if not self.client.is_connected():
            await self.client.start()

    async def stop(self):
        """Остановка клиента"""
        if self.client.is_connected():
            await self.client.disconnect() 