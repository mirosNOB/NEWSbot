import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Конфигурация бота
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Пути к файлам и директориям
DATA_DIR = 'data'
CHANNELS_DIR = os.path.join(DATA_DIR, 'channels')
USERS_DIR = os.path.join(DATA_DIR, 'users')
PROMPTS_FILE = 'prompts.yaml'

# Настройки AI
DEFAULT_AI_MODEL = "gpt-3.5-turbo"
AI_TEMPERATURE = 0.7

# Создание необходимых директорий
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHANNELS_DIR, exist_ok=True)
os.makedirs(USERS_DIR, exist_ok=True) 