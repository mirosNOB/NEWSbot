import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Конфигурация бота
BOT_TOKEN = os.getenv('BOT_TOKEN')

# OpenRouter API ключ
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Пути к файлам и директориям
DATA_DIR = 'data'
CHANNELS_DIR = os.path.join(DATA_DIR, 'channels')
USERS_DIR = os.path.join(DATA_DIR, 'users')
PROMPTS_FILE = 'prompts.yaml'
WHITELIST_FILE = os.path.join(DATA_DIR, 'whitelist.json')
ADMINS_FILE = os.path.join(DATA_DIR, 'admins.json')

# Настройки AI
DEFAULT_AI_MODEL = "openai/gpt-3.5-turbo"
AI_TEMPERATURE = 0.7

# Создание необходимых директорий
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHANNELS_DIR, exist_ok=True)
os.makedirs(USERS_DIR, exist_ok=True)

# Настройки прав доступа
ADMIN_COMMANDS = {
    'add_user': 'Добавить пользователя в белый список',
    'remove_user': 'Удалить пользователя из белого списка',
    'list_users': 'Показать список пользователей',
    'add_admin': 'Добавить администратора',
    'remove_admin': 'Удалить администратора',
    'list_admins': 'Показать список администраторов',
    'broadcast': 'Отправить сообщение всем пользователям'
} 