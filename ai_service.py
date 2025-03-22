import os
import json
import asyncio
import logging
import shutil
import tempfile
import random
import re
from typing import Dict, Optional, Iterator, AsyncGenerator, List, Tuple
from datetime import datetime, timedelta
from pathlib import Path
import time
import requests
import aiohttp

import openai
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Настраиваем логирование
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()

# API ключ OpenRouter
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Функция для фильтрации моделей по регулярному выражению
def filter_models_by_regex(models_list, pattern):
    """Фильтрует модели по регулярному выражению"""
    regex = re.compile(pattern, re.IGNORECASE)  # Добавляем игнорирование регистра
    return [model for model in models_list if regex.search(model.get("id", ""))]

# Функция для получения списка моделей через API OpenRouter
async def fetch_models_from_openrouter():
    """Получает список доступных моделей из OpenRouter API"""
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get("https://openrouter.ai/api/v1/models", headers=headers) as response:
                if response.status == 200:
                    models_data = await response.json()
                    logger.info(f"Получено {len(models_data['data'])} моделей от OpenRouter API")
                    return models_data['data']
                else:
                    logger.error(f"Ошибка получения моделей: {response.status}")
                    return []
    except Exception as e:
        logger.error(f"Ошибка при получении моделей: {str(e)}")
        return []

# Фильтр для моделей Claude
CLAUDE_MODEL_PATTERN = r"claude"

# Здесь будет храниться актуальный список моделей
AVAILABLE_MODELS = [
    # Базовые модели для начала работы, будет обновлено при запуске бота
    {"id": "anthropic/claude-3-haiku", "name": "Claude 3 Haiku", "context_length": 200000}
]

# Значение по умолчанию для модели - будет изменено после первого получения списка моделей
DEFAULT_MODEL = "anthropic/claude-3-haiku"

# URL для OpenRouter API
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Хранилище выбранных моделей
user_models: Dict[str, str] = {}

# Функция для обновления списка доступных моделей
async def update_available_models():
    """Обновляет список доступных моделей из API"""
    global AVAILABLE_MODELS, DEFAULT_MODEL
    
    models = await fetch_models_from_openrouter()
    if not models:
        logger.warning("Не удалось получить модели из API, используем предустановленные")
        return False
    
    # Фильтруем только модели Claude
    claude_models = filter_models_by_regex(models, CLAUDE_MODEL_PATTERN)
    if not claude_models:
        logger.warning("Не найдено моделей Claude в API")
        return False
    
    # Обновляем список моделей
    AVAILABLE_MODELS = claude_models
    
    # Обновляем модель по умолчанию - выбираем первую модель Claude 3 Haiku, если есть
    for model in claude_models:
        if "haiku" in model.get("id", "").lower():
            DEFAULT_MODEL = model.get("id")
            logger.info(f"Установлена модель по умолчанию: {DEFAULT_MODEL}")
            break
    else:
        # Если нет подходящей модели, берем первую доступную
        DEFAULT_MODEL = claude_models[0].get("id")
        logger.info(f"Установлена модель по умолчанию (первая в списке): {DEFAULT_MODEL}")
    
    return True

def get_available_models():
    """Получение списка доступных моделей"""
    return AVAILABLE_MODELS

def get_model_by_id(model_id: str):
    """Получение информации о модели по её ID"""
    for model in AVAILABLE_MODELS:
        if model.get("id") == model_id:
            return model
    
    # Если модель не найдена, возвращаем базовую информацию
    return {"id": model_id, "name": model_id.split("/")[-1], "context_length": 100000}

def get_error_message(e: Exception) -> str:
    """Получение текста ошибки"""
    return f"{type(e).__name__}: {e}"

def prepare_conversation_kwargs(message_text: str, conversation_id: str = None, user_id: int = None) -> dict:
    """Подготовка параметров для разговора"""
    kwargs = {}
    
    # Добавляем сообщение
    messages = []
    messages.append({
        "role": "user",
        "content": message_text
    })
    
    # Получаем модель пользователя если есть
    model = None
    if user_id:
        if str(user_id) in user_models:
            model = user_models[str(user_id)]
        else:
            # Если модель не выбрана, используем дефолтную
            model = DEFAULT_MODEL
            user_models[str(user_id)] = model
    
    # Возвращаем подготовленные параметры
    return {
        "model": model,  # Теперь всегда будет модель
        "messages": messages,
        "user_id": str(user_id) if user_id else None,  # Добавляем user_id для отслеживания
    }

async def create_response_stream(kwargs: Dict, user_id: str) -> AsyncGenerator[str, None]:
    """Создает поток ответов от OpenRouter API"""
    try:
        # Получаем модель из kwargs или используем модель по умолчанию
        model = kwargs.get("model", DEFAULT_MODEL)
        messages = kwargs.get("messages", [])
        
        # Проверяем наличие API ключа
        if not OPENROUTER_API_KEY:
            yield "❌ API ключ OpenRouter не настроен. Пожалуйста, добавьте OPENROUTER_API_KEY в .env файл."
            return
        
        # Запрос к API в потоковом режиме
        try:
            logger.info(f"Отправка запроса к OpenRouter API с моделью {model}")
            
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 2000,
                "stream": True
            }
            
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://telegram.org",  # Реферер (обычно ваш домен)
                "X-Title": "Telegram News Bot"          # Название вашего приложения
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(OPENROUTER_API_URL, json=payload, headers=headers) as response:
                    async for line in response.content:
                        if line:
                            line_text = line.decode('utf-8').strip()
                            if line_text.startswith('data:') and not line_text.startswith('data: [DONE]'):
                                json_data = json.loads(line_text[5:])
                                if 'choices' in json_data and json_data['choices']:
                                    if 'delta' in json_data['choices'][0] and 'content' in json_data['choices'][0]['delta']:
                                        content = json_data['choices'][0]['delta']['content']
                                        if content:
                                            yield content
                    
        except Exception as e:
            error_msg = f"❌ Ошибка при запросе к OpenRouter API: {str(e)}"
            logger.error(error_msg)
            yield error_msg
            
    except Exception as e:
        error_msg = f"❌ Ошибка: {str(e)}"
        logger.error(error_msg)
        yield error_msg

async def try_gpt_request(prompt: str, posts_text: str = "", user_id: int = None, bot=None, user_data: dict = None):
    """Отправка запроса к OpenRouter API через requests вместо OpenAI SDK"""
    try:
        # Получаем модель из user_data или используем модель по умолчанию
        selected_model_id = user_data.get('ai_settings', {}).get('model', DEFAULT_MODEL)
        
        # Информация о модели
        model_info = get_model_by_id(selected_model_id)
        
        # Отправляем сообщение о начале анализа
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {len(posts_text)} символов\n"
            f"Выбранная модель: {model_info.get('name', selected_model_id)}"
        )
        
        # Подготовка сообщений для API
        messages = [
            {"role": "system", "content": "Ты мой личный ассистент для анализа данных. Ты всегда отвечаешь кратко и по делу, без лишних слов."},
            {"role": "user", "content": f"{prompt}\n\nДанные для анализа:\n{posts_text}"}
        ]
        
        # Подготовка запроса к API
        try:
            logger.info(f"Отправка запроса к OpenRouter API с моделью {selected_model_id}")
            
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://telegram.org",  # Реферер
                "X-Title": "Telegram News Bot"          # Название вашего приложения
            }
            
            payload = {
                "model": selected_model_id,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 2000
            }
            
            # Отправляем асинхронный запрос
            async with aiohttp.ClientSession() as session:
                async with session.post(OPENROUTER_API_URL, json=payload, headers=headers) as response:
                    # Получаем ответ в формате JSON
                    response_json = await response.json()
                    
                    # Отладочная информация
                    logger.info(f"Структура ответа API: {str(response_json)}")
                    
                    # Проверка наличия ошибки в ответе
                    if "error" in response_json:
                        error_msg = f"Ошибка OpenRouter API: {response_json['error']['message']}"
                        if "metadata" in response_json.get("error", {}):
                            error_msg += f" ({response_json['error']['metadata'].get('provider_name', 'Unknown')})"
                        logger.error(error_msg)
                        raise Exception(error_msg)
                    
                    # Проверка структуры ответа
                    if "choices" not in response_json or not response_json["choices"]:
                        logger.error("Неверная структура ответа API: отсутствует поле choices или пустой массив")
                        raise Exception("Неверная структура ответа API: отсутствует поле choices")
                    
                    # Получаем ответ
                    assistant_response = response_json["choices"][0]["message"]["content"]
                    
                    # Если контент пустой или None, используем резервный ответ
                    if assistant_response is None or assistant_response == "":
                        logger.warning("Получен пустой content от API, использую резервный ответ")
                        assistant_response = "Извините, не удалось получить ответ от AI. Пожалуйста, попробуйте еще раз позже."
                    
                    # Удаляем статусное сообщение и возвращаем ответ
                    await status_message.delete()
                    return assistant_response
            
        except aiohttp.ClientError as e:
            error_str = str(e)
            logger.error(f"Ошибка при запросе к OpenRouter API: {error_str}")
            
            # Обновляем статусное сообщение с информацией об ошибке
            await status_message.edit_text(
                f"❌ Ошибка при запросе к API:\n{error_str}"
            )
            raise Exception(f"Ошибка при запросе к API: {error_str}")
            
    except Exception as e:
        error_msg = get_error_message(e)
        if 'status_message' in locals():
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)

# Функция для создания структуры DEFAULT_PROVIDERS из текущих доступных моделей
def update_default_providers():
    """Обновляет структуру DEFAULT_PROVIDERS на основе AVAILABLE_MODELS"""
    global DEFAULT_PROVIDERS
    
    # Группируем модели по провайдерам
    models_by_provider = {}
    
    for model in AVAILABLE_MODELS:
        model_id = model.get("id", "")
        provider = "Anthropic"  # Используем фиксированное имя провайдера для Claude
        
        if provider not in models_by_provider:
            models_by_provider[provider] = []
        
        models_by_provider[provider].append(model_id)
    
    # Создаем новую структуру DEFAULT_PROVIDERS
    new_providers = []
    for provider, models in models_by_provider.items():
        new_providers.append({
            'provider': provider,
            'models': models
        })
    
    DEFAULT_PROVIDERS = new_providers
    logger.info(f"Обновлены провайдеры моделей: {len(DEFAULT_PROVIDERS)} провайдеров")

# Конфигурация моделей для показа пользователю
DEFAULT_PROVIDERS = [
    {
        'provider': 'Anthropic',
        'models': ['anthropic/claude-3-haiku']  # Будет заполнено реальными моделями при запуске
    }
]

# Экспортируем для использования в других модулях
__all__ = [
    'try_gpt_request',
    'DEFAULT_PROVIDERS',
    'prepare_conversation_kwargs',
    'create_response_stream',
    'get_available_models',
    'user_models',
    'DEFAULT_MODEL',
    'filter_models_by_regex',
    'fetch_models_from_openrouter',
    'CLAUDE_MODEL_PATTERN',
    'update_available_models',
    'update_default_providers'
] 