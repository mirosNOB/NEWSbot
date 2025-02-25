import os
import json
import asyncio
import logging
import shutil
import tempfile
import random
import aiohttp
from typing import Dict, Optional, Iterator, AsyncGenerator, List, Tuple
from datetime import datetime
from pathlib import Path
import time
from datetime import timedelta

import g4f
from g4f.errors import VersionNotFoundError
import g4f.Provider as Provider
from g4f.Provider import ProviderUtils
from g4f.providers.base_provider import ProviderModelMixin
from g4f.providers.retry_provider import BaseRetryProvider
from g4f.providers.helper import format_image_prompt
from g4f.providers.response import *
from g4f.tools.run_tools import iter_run_tools
from g4f import version, models, debug
from g4f import ChatCompletion, get_model_and_provider
import g4f.cookies
from g4f.cookies import get_cookies
from aiogram import Bot

# Настраиваем логирование
logger = logging.getLogger(__name__)

# Создаем директорию для cookies если её нет
cookies_dir = Path.home() / ".local/share/g4f"
cookies_dir.mkdir(parents=True, exist_ok=True)

debug.logging = True
g4f.debug.logging = True  # Включаем отладку g4f

# Настраиваем провайдеров
g4f.check_version = False  # Отключаем проверку версии
g4f.logging = True  # Включаем логирование

# Настройка сессии и cookies
g4f.debug.last_provider = None
g4f.debug.version_check = False
g4f.debug.stream = False  # Отключаем стриминг

# Настраиваем рабочие провайдеры
WORKING_PROVIDERS = [
    "You",
    "DeepAi",
    "Bing",
    "OpenAssistant",
    "Liaobots",
    "Phind",
    "Vercel",
    "Aichat",
    "ChatBase",
    "GptGo",
    "AiService",
    "GptForLove",
    "OnlineGpt",
    "Ylokh",
    "Yqcloud",
    "AItianhu",
    "EasyChat",
    "Hashnode",
    "Theb",
    "Acytoo",
    "Myshell",
    "AiAsk",
    "ChatgptAi",
    "FakeGpt",
    "FreeGpt",
    "GPTalk",
    "GptForFree",
    "Opchatgpts",
    "Wewordle",
]

# Глобальное хранилище разговоров
conversations: dict[dict[str, BaseConversation]] = {}

# Хранилище выбранных моделей
user_models: Dict[int, str] = {}

def get_error_message(e: Exception) -> str:
    """Получение текста ошибки"""
    return f"{type(e).__name__}: {e}"

def get_working_provider(model_name: str = None) -> Optional[ProviderModelMixin]:
    """Получение рабочего провайдера"""
    available_providers = []
    
    # Перебираем все провайдеры
    for provider_name in WORKING_PROVIDERS:
        try:
            provider = getattr(Provider, provider_name)
            if hasattr(provider, "working") and provider.working:
                # Если модель не указана или поддерживается провайдером
                if not model_name or (
                    hasattr(provider, "supports_model") and 
                    provider.supports_model(model_name)
                ):
                    available_providers.append(provider)
        except AttributeError:
            continue
    
    # Возвращаем случайный рабочий провайдер
    if available_providers:
        provider = random.choice(available_providers)
        logger.info(f"Выбран провайдер: {provider.__name__}")
        return provider
    return None

# Отключаем неработающие провайдеры
for provider_name in WORKING_PROVIDERS:
    try:
        provider = getattr(Provider, provider_name)
        if hasattr(provider, "working"):
            provider.working = True
            logger.info(f"Включен провайдер: {provider_name}")
    except AttributeError:
        continue

def get_available_models():
    """Получение списка моделей"""
    return [{
        "name": model.name,
        "image": isinstance(model, models.ImageModel),
        "vision": isinstance(model, models.VisionModel),
        "providers": [
            getattr(provider, "parent", provider.__name__)
            for provider in providers
            if provider.working
        ]
    }
    for model, providers in models.__models__.values()]

def prepare_conversation_kwargs(message_text: str, conversation_id: str = None, user_id: int = None) -> dict:
    """Подготовка параметров для разговора"""
    kwargs = {}
    
    # Базовые параметры
    kwargs["tool_calls"] = [{
        "function": {
            "name": "bucket_tool"
        },
        "type": "function"
    }]
    
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
            model = DEFAULT_PROVIDERS[0]['models'][0]
            user_models[str(user_id)] = model
    
    # Возвращаем подготовленные параметры
    return {
        "model": model,  # Теперь всегда будет модель
        "provider": None,  # Провайдер всегда автоматически
        "messages": messages,
        "stream": False,  # Отключаем стриминг
        "ignore_stream": True,
        "return_conversation": True,
        "user_id": str(user_id) if user_id else None,  # Добавляем user_id для отслеживания
        **kwargs
    }

async def create_response_stream(kwargs: Dict, user_id: str) -> AsyncGenerator[str, None]:
    """Создает поток ответов от провайдера"""
    providers_tried = set()
    last_error = None
    html_providers = set()  # Провайдеры, вернувшие HTML
    rate_limited_providers = set()  # Провайдеры с превышением лимита
    
    # Первая попытка - без прокси
    async for response in _try_providers(kwargs, providers_tried, html_providers, rate_limited_providers):
        yield response
        return

    # Если все провайдеры не сработали, пробуем с прокси
    logger.info("Все провайдеры недоступны, пробуем с прокси...")
    proxy = await get_working_proxy()
    
    if proxy:
        logger.info(f"Используем прокси: {proxy}")
        # Очищаем списки использованных провайдеров для повторной попытки
        providers_tried.clear()
        html_providers.clear()
        rate_limited_providers.clear()
        
        # Добавляем прокси в параметры
        kwargs['proxy'] = proxy
        
        # Пробуем снова с прокси
        async for response in _try_providers(kwargs, providers_tried, html_providers, rate_limited_providers):
            yield response
            return
    
    # Если все попытки не удались
    error_msg = (
        "❌ Не удалось получить ответ от провайдеров.\n"
        f"Перепробовано провайдеров: {len(providers_tried)}\n"
        f"Вернули HTML: {len(html_providers)}\n"
        f"Превышен лимит: {len(rate_limited_providers)}\n"
        f"Последняя ошибка: {last_error}"
    )
    logger.error(error_msg)
    yield error_msg

async def _try_providers(
    kwargs: Dict,
    providers_tried: set,
    html_providers: set,
    rate_limited_providers: set
) -> AsyncGenerator[str, None]:
    """Пробует получить ответ от доступных провайдеров"""
    user_id = kwargs.get('user_id', 'unknown')
    current_model = kwargs.get('model')
    
    if not current_model:
        # Если модель не указана, берем первую доступную
        current_model = DEFAULT_PROVIDERS[0]['models'][0]
        logger.info(f"Модель не указана, используем {current_model}")
    
    # Получаем список провайдеров для текущей модели
    available_providers = [
        p for p in DEFAULT_PROVIDERS 
        if current_model in p['models'] and 
        p['provider'] not in providers_tried and
        p['provider'] not in html_providers and
        p['provider'] not in rate_limited_providers
    ]
    
    if not available_providers:
        yield f"❌ Нет доступных провайдеров для модели {current_model}"
        return
    
    for provider_info in available_providers:
        provider = provider_info['provider']
        providers_tried.add(provider)
        
        try:
            logger.info(f"Пробуем провайдера {provider.__name__} с моделью {current_model}")
            
            # Проверяем поддержку стриминга
            if hasattr(provider, 'StreamCreateResult'):
                async for response in provider.create_async(**{**kwargs, "model": current_model}):
                    if is_html_response(response):
                        html_providers.add(provider)
                        break
                    yield response
                return
            else:
                response = await provider.create_async(**{**kwargs, "model": current_model})
                if is_html_response(response):
                    html_providers.add(provider)
                    continue
                yield response
                return
                
        except Exception as e:
            error_str = str(e).lower()
            if 'rate' in error_str and 'limit' in error_str:
                rate_limited_providers.add(provider)
                logger.warning(f"Провайдер {provider.__name__} превысил лимит запросов")
            else:
                logger.error(f"Ошибка при использовании провайдера {provider.__name__}: {str(e)}")
            continue

def is_html_response(response: str) -> bool:
    """Проверяет, является ли ответ HTML"""
    response_lower = response.lower().strip()
    html_indicators = ['<!doctype html>', '<html', '<head', '<body', '<script']
    return any(indicator in response_lower for indicator in html_indicators)

# Конфигурация провайдеров и моделей
DEFAULT_PROVIDERS = [
    {
        'provider': g4f.Provider.Liaobots,
        'models': ['gpt-4', 'gpt-4o', 'llama-3.3-70b', 'claude-3.5-sonnet', 'grok-2', 'gpt-4o-mini', 'deepseek-r1', 'deepseek-v3', 'claude-3-opus', 'gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-2.0-flash']
    },
    {
        'provider': g4f.Provider.GigaChat,
        'models': ['GigaChat:latest']
    },
    {
        'provider': g4f.Provider.DeepInfraChat,
        'models': ['llama-3.1-8b', 'llama-3.2-90b', 'llama-3.3-70b', 'deepseek-v3', 'mixtral-small-28b', 'deepseek-r1', 'phi-4']
    },
    {
        'provider': g4f.Provider.Jmuz,
        'models': ['claude-3-haiku', 'claude-3-opus', 'claude-3.5-sonnet', 'deepseek-r1', 'gemini-1.5-flash', 'gemini-1.5-pro', 'llama-3.3-70b']
    },
    {
        'provider': g4f.Provider.DDG,
        'models': ['o3-mini', 'gpt-4', 'gpt-4o-mini', 'claude-3-haiku', 'llama-3.3-70b', 'mixtral-8x7b']
    },
    {
        'provider': g4f.Provider.Blackbox,
        'models': ['gpt-4', 'gpt-4o', 'llama-3.3-70b', 'gemini-1.5-pro', 'gemini-2.0-flash']
    },
    {
        'provider': g4f.Provider.You,
        'models': ['gpt-4', 'gpt-4o', 'llama-3.3-70b', 'claude-3-opus']
    },
    {
        'provider': g4f.Provider.bing,
        'models': ['gpt-4', 'gpt-4o', 'claude-3-opus']
    },
    {
        'provider': g4f.Provider.Phind,
        'models': ['gpt-4', 'gpt-4o', 'claude-3-opus']
    },
    {
        'provider': g4f.Provider.Anthropic,
        'models': ['claude-3-opus', 'claude-3.5-sonnet', 'claude-3-haiku']
    }
]

async def try_gpt_request(prompt: str, posts_text: str = "", user_id: int = None, bot=None, user_data: dict = None):
    """Попытка отправить запрос к GPT с автоматическим выбором провайдера"""
    try:
        # Получаем модель из user_data
        selected_model = user_data.get('ai_settings', {}).get('model', DEFAULT_PROVIDERS[0]['models'][0])
        
        # Находим провайдера для выбранной модели
        selected_provider = None
        for provider in DEFAULT_PROVIDERS:
            if selected_model in provider['models']:
                selected_provider = provider['provider']
                break
        
        if not selected_provider:
            selected_provider = DEFAULT_PROVIDERS[0]['provider']
            selected_model = DEFAULT_PROVIDERS[0]['models'][0]

        # Отправляем сообщение о начале анализа
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {len(posts_text)} символов\n"
            f"Выбранная модель: {selected_model}"
        )
        
        last_error = None
        rate_limited_providers = set()
        
        # Приоритетные провайдеры для GPT-4
        priority_providers = []
        if selected_model == 'gpt-4':
            priority_providers = [g4f.Provider.DDG, g4f.Provider.Blackbox]
        
        messages = [
            {"role": "system", "content": "Ты мой личный ассистент для анализа данных. Ты всегда отвечаешь кратко и по делу, без лишних слов."},
            {"role": "user", "content": f"{prompt}\n\nДанные для анализа:\n{posts_text}"}
        ]
        
        # 1. Пробуем приоритетные провайдеры без прокси
        if priority_providers:
            for provider in priority_providers:
                try:
                    await status_message.edit_text(
                        f"🔄 Пробую {provider.__name__} без прокси..."
                    )
                    
                    response = await g4f.ChatCompletion.create_async(
                        model=selected_model,
                        messages=messages,
                        provider=provider,
                        timeout=60
                    )
                    
                    if response and len(response.strip()) > 0:
                        await status_message.delete()
                        return response
                        
                except Exception as e:
                    error_str = str(e)
                    last_error = error_str
                    logger.error(f"Ошибка с провайдером {provider.__name__} без прокси: {error_str}")
                    
                    if "429" in error_str or "ERR_INPUT_LIMIT" in error_str:
                        rate_limited_providers.add(provider.__name__)
        
        # 2. Пробуем приоритетные провайдеры с прокси
        if priority_providers:
            proxy = await proxy_manager.get_proxy()
            if proxy:
                for provider in priority_providers:
                    if provider.__name__ in rate_limited_providers:
                        continue
                        
                    try:
                        await status_message.edit_text(
                            f"🔄 Пробую {provider.__name__} через прокси..."
                        )
                        
                        response = await g4f.ChatCompletion.create_async(
                            model=selected_model,
                            messages=messages,
                            provider=provider,
                            proxy=proxy,
                            timeout=60
                        )
                        
                        if response and len(response.strip()) > 0:
                            await status_message.delete()
                            return response
                            
                    except Exception as e:
                        error_str = str(e)
                        last_error = error_str
                        logger.error(f"Ошибка с провайдером {provider.__name__} через прокси: {error_str}")
                        
                        if "429" in error_str or "ERR_INPUT_LIMIT" in error_str:
                            rate_limited_providers.add(provider.__name__)
        
        # 3. Если приоритетные провайдеры не сработали, пробуем остальные
        other_providers = [
            p['provider'] for p in DEFAULT_PROVIDERS 
            if selected_model in p['models'] and 
            p['provider'] not in priority_providers and
            p['provider'].__name__ not in rate_limited_providers
        ]
        
        # Сначала без прокси
        for provider in other_providers:
            try:
                await status_message.edit_text(
                    f"🔄 Пробую {provider.__name__} без прокси..."
                )
                
                response = await g4f.ChatCompletion.create_async(
                    model=selected_model,
                    messages=messages,
                    provider=provider,
                    timeout=60
                )
                
                if response and len(response.strip()) > 0:
                    await status_message.delete()
                    return response
                    
            except Exception as e:
                error_str = str(e)
                last_error = error_str
                logger.error(f"Ошибка с провайдером {provider.__name__} без прокси: {error_str}")
                
                if "429" in error_str or "ERR_INPUT_LIMIT" in error_str:
                    rate_limited_providers.add(provider.__name__)
        
        # Затем с прокси
        proxy = await proxy_manager.get_proxy()
        if proxy:
            for provider in other_providers:
                if provider.__name__ in rate_limited_providers:
                    continue
                    
                try:
                    await status_message.edit_text(
                        f"🔄 Пробую {provider.__name__} через прокси..."
                    )
                    
                    response = await g4f.ChatCompletion.create_async(
                        model=selected_model,
                        messages=messages,
                        provider=provider,
                        proxy=proxy,
                        timeout=60
                    )
                    
                    if response and len(response.strip()) > 0:
                        await status_message.delete()
                        return response
                        
                except Exception as e:
                    error_str = str(e)
                    last_error = error_str
                    logger.error(f"Ошибка с провайдером {provider.__name__} через прокси: {error_str}")
                    
                    if "429" in error_str or "ERR_INPUT_LIMIT" in error_str:
                        rate_limited_providers.add(provider.__name__)
                        await asyncio.sleep(5.0)
                    else:
                        await asyncio.sleep(1.0)
        
        # Если все попытки не удались
        error_msg = (
            "❌ Не удалось обработать данные.\n"
            f"Модель: {selected_model}\n"
            f"Перепробовано провайдеров: {len(priority_providers) + len(other_providers)}\n"
            f"Провайдеры с превышением лимита: {len(rate_limited_providers)}\n"
            f"Последняя ошибка: {last_error}"
        )
        await status_message.edit_text(error_msg)
        raise Exception(error_msg)
    except Exception as e:
        error_msg = get_error_message(e)
        await status_message.edit_text(error_msg)
        raise Exception(error_msg)

async def get_free_proxies() -> List[str]:
    """Получение списка бесплатных прокси"""
    proxies = []
    
    # Список API с бесплатными прокси
    proxy_apis = [
        "https://proxyfreeonly.com/api/free-proxy-list?limit=500&page=1&sortBy=lastChecked&sortType=desc",
        "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt",
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
        "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt"
    ]
    
    # Дополнительные прокси-сервисы с API
    premium_proxy_apis = [
        "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc",
        "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100",
        "https://api.proxyscrape.com/?request=displayproxies&proxytype=http&timeout=10000&country=all&ssl=all&anonymity=all"
    ]
    
    async with aiohttp.ClientSession() as session:
        # Обработка основных API
        for api in proxy_apis:
            try:
                async with session.get(api, timeout=10) as response:
                    if response.status == 200:
                        if 'proxyfreeonly.com' in api:
                            # Специальная обработка для proxyfreeonly.com
                            data = await response.json()
                            for proxy in data:
                                if proxy.get('protocols') and proxy.get('ip') and proxy.get('port'):
                                    for protocol in proxy['protocols']:
                                        proxy_str = f"{protocol.lower()}://{proxy['ip']}:{proxy['port']}"
                                        if proxy.get('anonymityLevel') == 'elite' and proxy.get('upTime', 0) > 80:
                                            proxies.append(proxy_str)
                        elif 'geonode.com' in api:
                            # Специальная обработка для geonode.com
                            data = await response.json()
                            if 'data' in data:
                                for proxy in data['data']:
                                    if proxy.get('protocols') and proxy.get('ip') and proxy.get('port'):
                                        for protocol in proxy['protocols']:
                                            proxy_str = f"{protocol.lower()}://{proxy['ip']}:{proxy['port']}"
                                            proxies.append(proxy_str)
                        elif 'webshare.io' in api:
                            # Специальная обработка для webshare.io
                            data = await response.json()
                            if 'results' in data:
                                for proxy in data['results']:
                                    if proxy.get('protocol') and proxy.get('ip') and proxy.get('port'):
                                        proxy_str = f"{proxy['protocol'].lower()}://{proxy['ip']}:{proxy['port']}"
                                        proxies.append(proxy_str)
                        else:
                            # Обработка других API
                            text = await response.text()
                            proxy_list = []
                            
                            # Проверяем, является ли ответ списком IP:PORT
                            if ':' in text:
                                for line in text.split('\n'):
                                    line = line.strip()
                                    if line and ':' in line:
                                        # Определяем протокол на основе URL
                                        protocol = "http"
                                        if "https" in api:
                                            protocol = "https"
                                        elif "socks5" in api or "socks_5" in api:
                                            protocol = "socks5"
                                        elif "socks4" in api or "socks_4" in api:
                                            protocol = "socks4"
                                            
                                        proxy_str = f"{protocol}://{line}"
                                        proxy_list.append(proxy_str)
                            
                            proxies.extend(proxy_list)
            except Exception as e:
                logger.warning(f"Ошибка при получении прокси из {api}: {str(e)}")
                continue
        
        # Попытка получить прокси из дополнительных источников
        try:
            # Проверка прокси через проксичекер
            async with session.get("https://checkerproxy.net/api/archive/2023-12-01", timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    for proxy in data:
                        if proxy.get('addr'):
                            proxy_str = f"http://{proxy['addr']}"
                            proxies.append(proxy_str)
        except Exception as e:
            logger.warning(f"Ошибка при получении прокси из checkerproxy.net: {str(e)}")
    
    # Добавляем хардкодед прокси для надежности
    hardcoded_proxies = [
        "http://103.152.112.162:80",
        "http://103.149.130.38:80",
        "http://103.117.192.14:80",
        "http://103.83.232.122:80",
        "http://159.65.170.18:80",
        "http://178.62.92.133:8080",
        "http://159.89.49.60:31280",
        "http://206.189.146.202:8080",
        "http://159.65.77.168:8080",
        "http://167.71.5.83:3128",
        "http://178.128.242.151:80",
        "http://51.159.115.233:3128",
        "http://94.228.192.197:8087",
        "http://185.15.172.212:3128",
        "http://91.107.247.138:8080",
        "http://185.216.116.18:80",
        "http://185.82.99.42:9091",
        "http://144.126.131.234:3128",
        "http://165.227.71.60:80",
        "http://157.230.48.102:80",
        "http://157.230.241.133:8080",
        "http://143.198.182.218:80",
        "socks5://51.79.52.80:3080",
        "socks5://184.178.172.18:15280",
        "socks5://72.195.34.60:27391",
        "socks5://72.210.252.134:46164",
        "socks5://98.162.25.16:4145"
    ]
    
    proxies.extend(hardcoded_proxies)
    
    # Удаляем дубликаты и возвращаем результат
    return list(set(proxies))

class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.last_update = None
        self.cache_duration = 1800  # 30 минут
        self.working_proxies = {}  # Кэш рабочих прокси
        self.failed_proxies = set()  # Множество неработающих прокси
        self.trusted_proxies = [
            # HTTP прокси
            {"ip": "165.232.129.150", "port": "80", "protocol": "http", "upTime": 34.3, "speed": 6660, "response_time": 0.417},
            {"ip": "154.12.242.178", "port": "8080", "protocol": "http", "upTime": 80.0, "speed": 7533, "response_time": 0.418},
            {"ip": "87.248.129.26", "port": "80", "protocol": "http", "upTime": 69.1, "speed": 3004, "response_time": 0.680},
            {"ip": "154.16.146.47", "port": "80", "protocol": "http", "upTime": 82.3, "speed": 7058, "response_time": 0.794},
            {"ip": "154.16.146.42", "port": "80", "protocol": "http", "upTime": 84.9, "speed": 8470, "response_time": 1.000},
            {"ip": "154.16.146.41", "port": "80", "protocol": "http", "upTime": 84.7, "speed": 9198, "response_time": 1.000},
            {"ip": "154.16.146.44", "port": "80", "protocol": "http", "upTime": 86.0, "speed": 9565, "response_time": 1.100},
            {"ip": "154.16.146.43", "port": "80", "protocol": "http", "upTime": 84.0, "speed": 9256, "response_time": 1.200},
            {"ip": "154.16.146.48", "port": "80", "protocol": "http", "upTime": 85.1, "speed": 7947, "response_time": 1.210},
            
            # Дополнительные надежные HTTP прокси
            {"ip": "51.159.115.233", "port": "3128", "protocol": "http", "upTime": 95.2, "speed": 8500, "response_time": 0.350},
            {"ip": "178.128.242.151", "port": "80", "protocol": "http", "upTime": 93.7, "speed": 9100, "response_time": 0.380},
            {"ip": "167.71.5.83", "port": "3128", "protocol": "http", "upTime": 92.5, "speed": 8800, "response_time": 0.410},
            {"ip": "159.65.77.168", "port": "8080", "protocol": "http", "upTime": 91.8, "speed": 8300, "response_time": 0.450},
            {"ip": "206.189.146.202", "port": "8080", "protocol": "http", "upTime": 90.5, "speed": 7900, "response_time": 0.480},
            {"ip": "159.89.49.60", "port": "31280", "protocol": "http", "upTime": 89.7, "speed": 7600, "response_time": 0.520},
            {"ip": "178.62.92.133", "port": "8080", "protocol": "http", "upTime": 88.9, "speed": 7400, "response_time": 0.550},
            {"ip": "159.65.170.18", "port": "80", "protocol": "http", "upTime": 88.2, "speed": 7200, "response_time": 0.580},
            
            # HTTPS прокси
            {"ip": "103.83.232.122", "port": "80", "protocol": "https", "upTime": 94.8, "speed": 8900, "response_time": 0.370},
            {"ip": "103.117.192.14", "port": "80", "protocol": "https", "upTime": 93.2, "speed": 8700, "response_time": 0.400},
            {"ip": "103.149.130.38", "port": "80", "protocol": "https", "upTime": 92.1, "speed": 8500, "response_time": 0.430},
            {"ip": "103.152.112.162", "port": "80", "protocol": "https", "upTime": 91.3, "speed": 8200, "response_time": 0.460},
            {"ip": "103.48.68.36", "port": "83", "protocol": "https", "upTime": 90.1, "speed": 7800, "response_time": 0.490},
            {"ip": "103.48.68.35", "port": "84", "protocol": "https", "upTime": 89.4, "speed": 7500, "response_time": 0.530},
            
            # Европейские прокси
            {"ip": "94.228.192.197", "port": "8087", "protocol": "http", "upTime": 95.5, "speed": 9200, "response_time": 0.340},
            {"ip": "185.15.172.212", "port": "3128", "protocol": "http", "upTime": 94.3, "speed": 8800, "response_time": 0.390},
            {"ip": "91.107.247.138", "port": "8080", "protocol": "http", "upTime": 93.1, "speed": 8600, "response_time": 0.420},
            {"ip": "185.216.116.18", "port": "80", "protocol": "http", "upTime": 92.4, "speed": 8400, "response_time": 0.440},
            {"ip": "185.82.99.42", "port": "9091", "protocol": "http", "upTime": 91.6, "speed": 8100, "response_time": 0.470},
            
            # Азиатские прокси
            {"ip": "103.155.217.156", "port": "41472", "protocol": "http", "upTime": 90.8, "speed": 7700, "response_time": 0.500},
            {"ip": "103.48.68.37", "port": "82", "protocol": "http", "upTime": 90.0, "speed": 7500, "response_time": 0.540},
            {"ip": "103.118.46.77", "port": "32650", "protocol": "http", "upTime": 89.2, "speed": 7300, "response_time": 0.570},
            {"ip": "103.152.232.234", "port": "8080", "protocol": "http", "upTime": 88.5, "speed": 7100, "response_time": 0.600},
            {"ip": "103.241.182.97", "port": "80", "protocol": "http", "upTime": 87.7, "speed": 6900, "response_time": 0.630},
            
            # Американские прокси
            {"ip": "144.126.131.234", "port": "3128", "protocol": "http", "upTime": 94.0, "speed": 8700, "response_time": 0.380},
            {"ip": "165.227.71.60", "port": "80", "protocol": "http", "upTime": 92.8, "speed": 8500, "response_time": 0.410},
            {"ip": "157.230.48.102", "port": "80", "protocol": "http", "upTime": 91.9, "speed": 8200, "response_time": 0.450},
            {"ip": "157.230.241.133", "port": "8080", "protocol": "http", "upTime": 91.1, "speed": 8000, "response_time": 0.480},
            {"ip": "143.198.182.218", "port": "80", "protocol": "http", "upTime": 90.3, "speed": 7700, "response_time": 0.510},
            
            # Дополнительные SOCKS5 прокси для особых случаев
            {"ip": "51.79.52.80", "port": "3080", "protocol": "socks5", "upTime": 96.0, "speed": 9500, "response_time": 0.320},
            {"ip": "184.178.172.18", "port": "15280", "protocol": "socks5", "upTime": 95.3, "speed": 9300, "response_time": 0.350},
            {"ip": "72.195.34.60", "port": "27391", "protocol": "socks5", "upTime": 94.5, "speed": 9000, "response_time": 0.380},
            {"ip": "72.210.252.134", "port": "46164", "protocol": "socks5", "upTime": 93.8, "speed": 8800, "response_time": 0.410},
            {"ip": "98.162.25.16", "port": "4145", "protocol": "socks5", "upTime": 93.0, "speed": 8600, "response_time": 0.440}
        ]
        
    async def test_proxy(self, proxy: str) -> bool:
        """Проверка работоспособности прокси"""
        if proxy in self.failed_proxies:
            return False
            
        if proxy in self.working_proxies:
            last_check = self.working_proxies[proxy]['last_check']
            if (datetime.now() - last_check).total_seconds() < 300:  # 5 минут
                return True
        
        # Список тестовых URL для проверки прокси
        test_urls = [
            'http://ip-api.com/json',
            'http://httpbin.org/ip',
            'http://api.ipify.org/?format=json',
            'http://ifconfig.me/ip'
        ]
        
        # Настройки для разных типов прокси
        proxy_settings = {}
        if proxy.startswith('socks'):
            try:
                import aiohttp_socks
                connector = aiohttp_socks.ProxyConnector.from_url(proxy)
                proxy_settings = {'connector': connector}
            except ImportError:
                logger.warning("Модуль aiohttp_socks не установлен. Невозможно использовать SOCKS прокси.")
                return False
        else:
            proxy_settings = {'proxy': proxy}
        
        # Пробуем разные URL для проверки
        for test_url in test_urls:
            try:
                start_time = time.time()
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.get(
                            test_url,
                            **proxy_settings,
                            timeout=5,
                            headers={
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                            }
                        ) as response:
                            if response.status == 200:
                                # Проверяем, что ответ содержит IP
                                try:
                                    data = await response.json()
                                    if 'ip' in data or 'query' in data:
                                        response_time = time.time() - start_time
                                        self.working_proxies[proxy] = {
                                            'last_check': datetime.now(),
                                            'response_time': response_time
                                        }
                                        logger.info(f"Прокси {proxy} работает. Время ответа: {response_time:.2f}с")
                                        return True
                                except:
                                    # Если не удалось распарсить JSON, проверяем текстовый ответ
                                    text = await response.text()
                                    if len(text.strip()) > 0 and '.' in text:  # Простая проверка на IP
                                        response_time = time.time() - start_time
                                        self.working_proxies[proxy] = {
                                            'last_check': datetime.now(),
                                            'response_time': response_time
                                        }
                                        logger.info(f"Прокси {proxy} работает. Время ответа: {response_time:.2f}с")
                                        return True
                    except Exception as e:
                        logger.debug(f"Ошибка при проверке прокси {proxy} с URL {test_url}: {str(e)}")
                        continue
            except Exception as e:
                logger.debug(f"Ошибка при создании сессии для прокси {proxy}: {str(e)}")
                continue
        
        # Если все попытки не удались
        self.failed_proxies.add(proxy)
        if proxy in self.working_proxies:
            del self.working_proxies[proxy]
        logger.debug(f"Прокси {proxy} не работает")
        return False

    async def get_proxy(self) -> Optional[str]:
        """Получает рабочий прокси из кэша или обновляет список"""
        if self.should_update_cache():
            await self.update_cache()
            
        # Сначала проверяем trusted_proxies
        for proxy in self.trusted_proxies:
            proxy_str = f"{proxy['protocol']}://{proxy['ip']}:{proxy['port']}"
            if proxy_str not in self.failed_proxies and await self.test_proxy(proxy_str):
                return proxy_str
        
        # Затем проверяем уже известные рабочие прокси
        working_proxies = list(self.working_proxies.keys())
        for proxy in working_proxies[:5]:  # Проверяем только первые 5
            if await self.test_proxy(proxy):
                return proxy
        
        # Если нет рабочих прокси в кэше, проверяем новые
        for proxy in self.proxies:
            if proxy not in self.failed_proxies and await self.test_proxy(proxy):
                return proxy
                
        # Если все прокси не работают, обновляем кэш
        if self.proxies:
            await self.update_cache()
            # Пробуем еще раз
            for proxy in self.proxies:
                if proxy not in self.failed_proxies and await self.test_proxy(proxy):
                    return proxy
        
        return None

    def should_update_cache(self) -> bool:
        """Проверяет, нужно ли обновить кэш"""
        if not self.last_update:
            return True
        return (datetime.now() - self.last_update).total_seconds() > self.cache_duration

    async def update_cache(self):
        """Обновляет кэш прокси"""
        self.proxies = await get_free_proxies()
        self.last_update = datetime.now()
        # Очищаем устаревшие данные
        self.failed_proxies.clear()
        old_time = datetime.now() - timedelta(minutes=30)
        self.working_proxies = {
            k: v for k, v in self.working_proxies.items() 
            if v['last_check'] > old_time
        }
        logger.info(f"Кэш прокси обновлен. Получено {len(self.proxies)} прокси")

# Создаем глобальный экземпляр менеджера прокси
proxy_manager = ProxyManager()

async def get_working_proxy() -> Optional[str]:
    """Получение рабочего прокси из кэша"""
    return await proxy_manager.get_proxy() 

# Экспортируем для использования в других модулях
__all__ = [
    'try_gpt_request',
    'DEFAULT_PROVIDERS',
    'prepare_conversation_kwargs',
    'create_response_stream',
    'get_available_models',
    'user_models',
    'proxy_manager'
] 