import requests
from bs4 import BeautifulSoup
import logging
import re
import random
import time
from typing import List, Dict, Tuple, Optional
from urllib.parse import quote_plus

# Настраиваем логирование
logger = logging.getLogger(__name__)

# Пользовательские агенты для запросов
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
]

def generate_search_query(user_text: str, context: str = "") -> str:
    """
    Генерирует поисковый запрос на основе текста пользователя и контекста.
    
    Args:
        user_text: Исходный текст пользователя
        context: Дополнительный контекст (например, текст промпта) для улучшения запроса
        
    Returns:
        Строка поискового запроса
    """
    # Базовая очистка текста пользователя
    cleaned_user_text = re.sub(r'[^\w\s]', ' ', user_text)
    user_words = cleaned_user_text.split()
    
    # Извлекаем ключевые слова из текста пользователя
    # Берем не более 7 слов из начала текста (обычно самые важные)
    if len(user_words) > 7:
        primary_keywords = user_words[:7]
    else:
        primary_keywords = user_words
    
    # Базовая очистка контекста
    context_keywords = []
    if context:
        cleaned_context = re.sub(r'[^\w\s]', ' ', context)
        # Находим потенциально важные слова в контексте (имена, даты, названия)
        context_words = cleaned_context.split()
        
        # Определяем важные слова - с заглавной буквы, цифры и т.д.
        important_pattern = re.compile(r'([A-ZА-Я][a-zа-я]+|\d{4}|\d{1,2}\.\d{1,2}|\d{1,2}\.\d{1,2}\.\d{2,4})')
        
        for word in context_words:
            if important_pattern.match(word) and len(word) > 3:  # Длиннее 3 символов считаем важным
                context_keywords.append(word)
        
        # Если важных слов не нашлось, берем просто первые несколько слов
        if not context_keywords and len(context_words) > 0:
            context_keywords = context_words[:3]
    
    # Добавляем дополнительные слова из контекста, если они есть
    if context_keywords:
        # Берем до 3 важных слов из контекста
        selected_context = context_keywords[:3]
        
        # Добавляем важные контекстные слова к ключевым словам запроса
        keywords = primary_keywords + selected_context
    else:
        keywords = primary_keywords
    
    # Собираем запрос обратно в строку
    query = " ".join(keywords)
    
    # Логирование для отладки
    logger.info(f"Сгенерирован поисковый запрос: '{query}' на основе текста пользователя и контекста")
    
    return query

def search_duckduckgo(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Выполняет поиск в DuckDuckGo и возвращает результаты.
    
    Args:
        query: Поисковый запрос
        max_results: Максимальное количество результатов
        
    Returns:
        Список словарей с результатами поиска (заголовок, URL, описание)
    """
    try:
        encoded_query = quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
        
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://duckduckgo.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Ошибка поиска: код {response.status_code}")
            return []
        
        # Парсим HTML-ответ
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # DuckDuckGo возвращает результаты в элементах с классом 'result'
        for result in soup.select('.result')[:max_results]:
            title_elem = result.select_one('.result__title')
            link_elem = result.select_one('.result__url')
            snippet_elem = result.select_one('.result__snippet')
            
            if title_elem and link_elem:
                # Извлекаем реальную ссылку из атрибута href
                link = link_elem.get('href', '')
                if not link.startswith(('http://', 'https://')):
                    # Если ссылка относительная, пробуем найти в атрибуте data-href
                    link = result.select_one('a.result__a').get('href', '')
                
                # Очищаем ссылку от трекинга DuckDuckGo
                link = re.sub(r'^/.*?uddg=', '', link)
                
                results.append({
                    'title': title_elem.get_text(strip=True),
                    'url': link,
                    'snippet': snippet_elem.get_text(strip=True) if snippet_elem else ''
                })
        
        logger.info(f"Найдено {len(results)} результатов для запроса: {query}")
        return results
    
    except Exception as e:
        logger.error(f"Ошибка при поиске в DuckDuckGo: {str(e)}")
        return []

def extract_text_from_url(url: str, max_size: int = 50000) -> str:
    """
    Извлекает текст со страницы по указанному URL.
    
    Args:
        url: URL страницы для парсинга
        max_size: Максимальный размер извлекаемого текста
        
    Returns:
        Извлеченный текст страницы
    """
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            logger.error(f"Ошибка загрузки страницы {url}: код {response.status_code}")
            return ""
        
        # Определяем кодировку и декодируем содержимое
        try:
            response.encoding = response.apparent_encoding
            soup = BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            logger.error(f"Ошибка парсинга HTML: {str(e)}")
            return ""
        
        # Удаляем ненужные элементы
        for tag in soup(['script', 'style', 'nav', 'footer', 'iframe', 'noscript']):
            tag.decompose()
        
        # Извлекаем основной контент
        main_content = ""
        
        # Сначала проверяем основные контентные блоки
        content_tags = soup.select('article, main, .content, .post, .article, #content, #main')
        if content_tags:
            # Используем первый найденный основной блок контента
            main_content = content_tags[0].get_text(separator='\n', strip=True)
        else:
            # Если основных блоков нет, берем весь текст из body
            main_content = soup.body.get_text(separator='\n', strip=True) if soup.body else ""
        
        # Очищаем текст
        main_content = re.sub(r'\n+', '\n', main_content)  # Убираем повторяющиеся переводы строк
        main_content = re.sub(r'\s+', ' ', main_content)   # Убираем повторяющиеся пробелы
        
        # Ограничиваем размер возвращаемого текста
        if len(main_content) > max_size:
            main_content = main_content[:max_size] + "... [текст обрезан]"
        
        logger.info(f"Извлечено {len(main_content)} символов с {url}")
        return main_content
    
    except Exception as e:
        logger.error(f"Ошибка при извлечении текста с {url}: {str(e)}")
        return ""

def perform_web_search(query: str, max_pages: int = 3, max_text_per_page: int = 15000) -> str:
    """
    Выполняет поиск и извлекает информацию по запросу.
    
    Args:
        query: Поисковый запрос
        max_pages: Максимальное количество страниц для анализа
        max_text_per_page: Максимальный размер текста с одной страницы
        
    Returns:
        Объединенный текст со всех страниц
    """
    # Поиск в DuckDuckGo
    search_results = search_duckduckgo(query, max_results=max_pages)
    
    if not search_results:
        return "Не удалось найти информацию по запросу."
    
    # Извлекаем текст с каждой страницы
    all_texts = []
    for i, result in enumerate(search_results):
        url = result['url']
        logger.info(f"Обработка результата {i+1}/{len(search_results)}: {url}")
        
        # Добавляем заголовок и описание
        result_header = f"[Источник {i+1}: {result['title']}]\nURL: {url}\n"
        all_texts.append(result_header)
        
        # Извлекаем текст страницы
        page_text = extract_text_from_url(url, max_size=max_text_per_page)
        if page_text:
            all_texts.append(page_text)
        else:
            all_texts.append("Не удалось извлечь текст с этой страницы.")
        
        all_texts.append("-" * 40)  # Разделитель между страницами
        
        # Небольшая пауза между запросами, чтобы не перегружать сервера
        time.sleep(random.uniform(1.0, 2.0))
    
    # Объединяем все тексты
    combined_text = "\n".join(all_texts)
    
    return combined_text 