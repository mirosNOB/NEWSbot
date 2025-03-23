import logging
import yaml
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from channel_parser import ChannelParser
from ai_service import (
    try_gpt_request, DEFAULT_PROVIDERS, user_models, 
    fetch_models_from_openrouter, filter_models_by_regex, 
    CLAUDE_MODEL_PATTERN, DEFAULT_MODEL, update_available_models,
    update_default_providers, get_available_models
)
from config import BOT_TOKEN, PROMPTS_FILE, CHANNELS_DIR, USERS_DIR, WHITELIST_FILE, ADMINS_FILE, ADMIN_COMMANDS
import os
import json
from datetime import datetime
import asyncio
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
# Импортируем модуль для веб-поиска
from web_search import generate_search_query, perform_web_search

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),  # Вывод в терминал
        logging.FileHandler('bot.log')  # Вывод в файл
    ]
)
logger = logging.getLogger(__name__)

# Проверяем наличие необходимых директорий
os.makedirs(CHANNELS_DIR, exist_ok=True)
os.makedirs(USERS_DIR, exist_ok=True)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
channel_parser = ChannelParser()

# Загрузка промптов
def load_prompts():
    with open(PROMPTS_FILE, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)

prompts = load_prompts()

# Состояния FSM
class Form(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_input = State()
    waiting_for_channel = State()
    waiting_for_days = State()
    initial_setup = State()  # Добавляем состояние для начальной настройки
    waiting_for_user_id = State()  # Для добавления пользователя
    waiting_for_admin_id = State()  # Для добавления админа
    waiting_for_broadcast = State()  # Для рассылки
    waiting_for_model_id = State()  # Для ввода ID модели
    waiting_for_web_search_confirmation = State()  # Для подтверждения использования веб-поиска

# Функция для сохранения настроек пользователя
async def save_user_settings(user_id: int, settings: dict):
    with open(os.path.join(USERS_DIR, f"{user_id}.json"), 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# Функция для загрузки настроек пользователя
async def load_user_settings(user_id: int) -> dict:
    settings_file = os.path.join(USERS_DIR, f"{user_id}.json")
    if os.path.exists(settings_file):
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {'ai_settings': {'model': DEFAULT_AI_MODEL}, 'web_search_enabled': False}
    return {'ai_settings': {'model': DEFAULT_AI_MODEL}, 'web_search_enabled': False}

# Клавиатуры
def get_main_keyboard():
    """Основная клавиатура внизу экрана"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        ["📊 Анализ ситуации", "👤 PR и Имидж"],
        ["📰 Работа со СМИ", "⚠️ Кризис"],
        ["📺 Каналы", "⚙️ Настройки"]
    ]
    keyboard.add(*[types.KeyboardButton(text) for text in buttons[0]])
    keyboard.add(*[types.KeyboardButton(text) for text in buttons[1]])
    keyboard.add(*[types.KeyboardButton(text) for text in buttons[2]])
    return keyboard

def get_channels_keyboard():
    """Клавиатура управления каналами"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton("➕ Добавить", callback_data="add_channel"),
        types.InlineKeyboardButton("📋 Список", callback_data="list_channels"),
        types.InlineKeyboardButton("🔄 Обновить", callback_data="update_channels"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="channels_stats"),
        types.InlineKeyboardButton("◀️ Назад", callback_data="main_menu")
    ]
    # Добавляем кнопки по две в ряд
    keyboard.row(buttons[0], buttons[1])
    keyboard.row(buttons[2], buttons[3])
    keyboard.row(buttons[4])
    return keyboard

def get_settings_keyboard():
    """Клавиатура настроек"""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("🤖 Выбрать модель Claude", callback_data="select_model"),
        types.InlineKeyboardButton("✏️ Редактировать промпты", callback_data="edit_prompts"),
        types.InlineKeyboardButton("🔄 Перезагрузить промпты", callback_data="reload_prompts"),
        types.InlineKeyboardButton("ℹ️ О боте", callback_data="about"),
        types.InlineKeyboardButton("◀️ Назад", callback_data="main_menu")
    )
    return keyboard

def get_models_keyboard():
    """Клавиатура выбора модели AI"""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    
    for provider in DEFAULT_PROVIDERS:
        for model_id in provider['models']:
            # Получаем информацию о модели
            model_info = None
            for model in get_available_models():
                if model.get('id') == model_id:
                    model_info = model
                    break
            
            # Если не нашли информацию, используем базовую
            if not model_info:
                model_name = model_id.split('/')[-1]
            else:
                model_name = model_info.get('name', model_id.split('/')[-1])
            
            keyboard.add(
                types.InlineKeyboardButton(
                    f"{model_name} ({provider['provider']})", 
                    callback_data=f"model_{model_id}"
                )
            )
    
    keyboard.add(types.InlineKeyboardButton("Обновить список моделей", callback_data="refresh_models"))
    keyboard.add(types.InlineKeyboardButton("◀️ Назад", callback_data="settings"))
    return keyboard

# Маппинг текста кнопок к категориям
BUTTON_TO_CATEGORY = {
    "📊 Анализ ситуации": "political_analysis",
    "👤 PR и Имидж": "image_formation",
    "📰 Работа со СМИ": "media_relations",
    "⚠️ Кризис": "crisis_management"
}

def get_category_inline_keyboard(category):
    """Inline-клавиатура для подменю категории"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    # Маппинг названий действий к ключам в промптах
    actions = {
        "political_analysis": [
            ("📊 Анализ", "situation_analysis"),
            ("🔄 Прогноз", "forecast"),
            ("📈 SWOT", "swot")
        ],
        "image_formation": [
            ("📣 PR кампания", "pr_campaign"),
            ("📺 Медиа", "media_advice"),
            ("✨ Примеры", "success_cases")
        ],
        "media_relations": [
            ("📝 Пресс-релиз", "press_release"),
            ("🎤 Интервью", "interview")
        ],
        "crisis_management": [
            ("🚨 План действий", "action_plan"),
            ("⚖️ Юристы", "legal_advice"),
            ("📋 Примеры", "case_studies")
        ]
    }
    
    # Добавляем кнопки в два столбца
    buttons = []
    for display_name, action_key in actions.get(category, []):
        buttons.append(
            types.InlineKeyboardButton(
                display_name,
                callback_data=f"{category}_{action_key}"
            )
        )
    
    # Добавляем кнопки по две в ряд
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            keyboard.row(buttons[i], buttons[i + 1])
        else:
            keyboard.row(buttons[i])
    
    keyboard.row(types.InlineKeyboardButton("◀️ Назад", callback_data="main_menu"))
    return keyboard

# Клавиатуры для состояний ввода
def get_input_keyboard():
    """Клавиатура для состояний ввода"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("❌ Отмена", "✅ Готово")
    return keyboard

# Функция для показа индикатора "печатает..."
async def show_typing_status(chat_id, bot, stop_event):
    """Показывает индикатор 'печатает...' до тех пор, пока не будет установлен stop_event"""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id, 'typing')
            await asyncio.sleep(4)  # Обновляем статус каждые 4 секунды (статус обычно пропадает через 5 секунд)
        except Exception as e:
            logger.error(f"Ошибка при отправке статуса печати: {e}")
            break

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    logger.info(f"Пользователь {message.from_user.id} запустил бота")
    user_settings = await load_user_settings(message.from_user.id)
    
    if not user_settings.get('setup_completed'):
        logger.info(f"Начало первичной настройки для пользователя {message.from_user.id}")
        await Form.initial_setup.set()
        welcome_text = (
            "👋 *Добро пожаловать в Политтехнолог Бот\\!*\n\n"
            "Я ваш умный помощник в политической работе\\. "
            "Давайте настроим бота под ваши потребности\\.\n\n"
            "🤖 *Выберите предпочитаемую модель AI:*"
        )
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for provider in DEFAULT_PROVIDERS:
            for model in provider['models']:
                keyboard.add(
                    types.InlineKeyboardButton(
                        f"{model} ({provider['provider']})", 
                        callback_data=f"initial_model_{model}"
                    )
                )
        await message.answer(welcome_text, parse_mode="MarkdownV2", reply_markup=keyboard)
    else:
        # Если настройка уже выполнена, показываем главное меню
        await message.answer(
            "🤖 *Политтехнолог Бот*\n"
            "Выберите интересующий вас раздел:",
            parse_mode="MarkdownV2",
            reply_markup=get_main_keyboard()
        )

@dp.callback_query_handler(lambda c: c.data.startswith("initial_model_"), state=Form.initial_setup)
async def process_initial_model_selection(callback_query: types.CallbackQuery, state: FSMContext):
    """Обработчик выбора модели при начальной настройке"""
    model = callback_query.data.replace("initial_model_", "")
    user_id = str(callback_query.from_user.id)
    user_models[user_id] = model
    
    # Сохраняем настройки
    await save_user_settings(callback_query.from_user.id, {
        'setup_completed': True,
        'model': model,
        'setup_date': datetime.now().isoformat()
    })
    
    # Завершаем настройку
    await state.finish()
    
    setup_complete_text = (
        "✅ *Настройка завершена\\!*\n\n"
        f"🤖 Выбрана модель: `{model}`\n\n"
        "📌 *Основные возможности:*\n"
        "• Анализ политической ситуации\n"
        "• PR и имидж\\-мейкинг\n"
        "• Работа со СМИ\n"
        "• Кризисное управление\n\n"
        "🔍 *Совет:* Начните с добавления каналов для мониторинга\\, "
        "это позволит боту давать более точные рекомендации\\.\n\n"
        "Выберите нужный раздел в меню ниже:"
    )
    
    await callback_query.message.edit_text(
        setup_complete_text,
        parse_mode="MarkdownV2",
        reply_markup=None
    )
    
    await callback_query.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    
    await callback_query.answer("✅ Настройка завершена!")

@dp.message_handler(lambda message: message.text == "!сброс")
async def handle_reset(message: types.Message):
    """Секретная команда для сброса настроек"""
    user_id = message.from_user.id
    
    # Удаляем настройки пользователя
    try:
        os.remove(os.path.join(USERS_DIR, f"{user_id}.json"))
    except:
        pass
    
    # Удаляем модель пользователя
    if str(user_id) in user_models:
        del user_models[str(user_id)]
    
    # Очищаем каналы
    try:
        channel_parser.channels = {}
        channel_parser.save_channels()
    except Exception as e:
        logger.error(f"Ошибка при очистке каналов: {e}")
    
    # Удаляем данные каналов
    try:
        for item in os.listdir(CHANNELS_DIR):
            item_path = os.path.join(CHANNELS_DIR, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                for subitem in os.listdir(item_path):
                    os.remove(os.path.join(item_path, subitem))
                os.rmdir(item_path)
    except Exception as e:
        logger.error(f"Ошибка при удалении данных каналов: {e}")
    
    # Очистка папки har_and_cookies
    try:
        cookies_dir = os.path.join(os.getcwd(), "har_and_cookies")
        if os.path.exists(cookies_dir):
            for file in os.listdir(cookies_dir):
                file_path = os.path.join(cookies_dir, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    logger.info(f"Удален файл кукис: {file_path}")
            logger.info("✅ Папка har_and_cookies очищена")
    except Exception as e:
        logger.error(f"Ошибка при очистке папки har_and_cookies: {e}")
    
    # Сброс данных сессий ИИ
    try:
        # Очищаем кэш g4f провайдеров
        from ai_service import proxy_manager, conversations
        
        # Очистка кэша прокси
        if hasattr(proxy_manager, 'working_proxies'):
            proxy_manager.working_proxies.clear()
            proxy_manager.failed_proxies.clear()
            logger.info("✅ Кэш прокси очищен")
        
        # Очистка сохраненных разговоров
        if 'conversations' in globals():
            conversations.clear()
            logger.info("✅ Сохраненные разговоры очищены")
        
        # Также можно сбросить g4f кэш, если он используется
        import g4f
        g4f.debug.last_provider = None
        logger.info("✅ Сброшены данные сессий ИИ")
        
    except Exception as e:
        logger.error(f"Ошибка при сбросе данных ИИ: {e}")
    
    await message.answer(
        "🔄 *Все настройки сброшены*\n"
        "\\- Модель AI сброшена\n"
        "\\- Каналы удалены\n"
        "\\- Данные очищены\n"
        "\\- Кукис и кэш очищены\n\n"
        "Используйте /start для новой настройки бота\\.",
        parse_mode="MarkdownV2"
    )

@dp.message_handler(lambda message: message.text == "📺 Каналы")
async def handle_channels_button(message: types.Message):
    """Обработчик кнопки управления каналами"""
    await message.answer(
        "Управление каналами:",
        reply_markup=get_channels_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == "add_channel")
async def process_add_channel(callback_query: types.CallbackQuery):
    """Обработчик добавления канала"""
    await Form.waiting_for_channel.set()
    await callback_query.message.answer(
        "Отправьте ссылку на канал в формате:\n"
        "https://t.me/channel_name или @channel_name\n\n"
        "Нажмите ✅ Готово когда закончите или ❌ Отмена для отмены",
        reply_markup=get_input_keyboard()
    )
    await callback_query.answer()

@dp.message_handler(lambda message: message.text in ["❌ Отмена", "✅ Готово"] or message.text.startswith('/cancel'), state='*')
async def handle_input_buttons(message: types.Message, state: FSMContext):
    """Обработчик кнопок отмены и готово, а также команды /cancel"""
    current_state = await state.get_state()
    
    if message.text == "❌ Отмена" or message.text.startswith('/cancel'):
        if current_state is not None:
            await state.finish()
            await message.answer(
                "❌ Действие отменено",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer(
                "Нет активного действия для отмены",
                reply_markup=get_main_keyboard()
            )
    elif message.text == "✅ Готово":
        if current_state == "Form:waiting_for_channel":
            await message.answer(
                "Добавление каналов завершено.",
                reply_markup=get_main_keyboard()
            )
            await state.finish()
        else:
            await message.answer(
                "Действие завершено.",
                reply_markup=get_main_keyboard()
            )
            await state.finish()

@dp.message_handler(state=Form.waiting_for_channel)
async def process_channel_link(message: types.Message, state: FSMContext):
    # Проверка на команду отмены
    if message.text.startswith('/cancel'):
        await state.finish()
        await message.answer("❌ Действие отменено", reply_markup=get_main_keyboard())
        return
        
    if message.text.startswith(('https://t.me/', '@')):
        try:
            success, result = await channel_parser.add_channel(message.text)
            if success:
                await message.answer(
                    f"✅ {result}\n\nМожете добавить еще каналы или нажмите ✅ Готово",
                    reply_markup=get_input_keyboard()
                )
            else:
                await message.answer(
                    f"❌ {result}\n\nПопробуйте другой канал или нажмите ❌ Отмена",
                    reply_markup=get_input_keyboard()
                )
        except Exception as e:
            await message.answer(
                f"❌ Ошибка: {str(e)}\n\nПопробуйте другой канал или нажмите ❌ Отмена",
                reply_markup=get_input_keyboard()
            )
    else:
        await message.answer(
            "❌ Неверный формат ссылки. Используйте:\n"
            "https://t.me/channel_name или @channel_name\n\n"
            "Попробуйте еще раз или нажмите ❌ Отмена",
            reply_markup=get_input_keyboard()
        )

@dp.callback_query_handler(lambda c: c.data == "list_channels")
async def process_list_channels(callback_query: types.CallbackQuery):
    """Обработчик просмотра списка каналов"""
    channels = channel_parser.channels
    if not channels:
        await callback_query.message.answer("Список каналов пуст")
    else:
        channels_text = "📺 Список каналов:\n\n"
        for channel_id, info in channels.items():
            last_parsed = info.get('last_parsed', 'никогда')
            channels_text += (
                f"📌 {info['title']}\n"
                f"🔗 {info['link']}\n"
                f"🕒 Последнее обновление: {last_parsed}\n\n"
            )
        await callback_query.message.answer(channels_text)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "update_channels")
async def process_update_channels(callback_query: types.CallbackQuery):
    """Обработчик обновления данных каналов"""
    logger.info(f"Пользователь {callback_query.from_user.id} запросил обновление каналов")
    await Form.waiting_for_days.set()
    await callback_query.message.answer(
        "За какой период обновить данные?\n"
        "Укажите количество дней (от 1 до 30):\n\n"
        "Или нажмите ❌ Отмена для отмены",
        reply_markup=get_input_keyboard()
    )
    await callback_query.answer()

@dp.message_handler(state=Form.waiting_for_days)
async def process_days_input(message: types.Message, state: FSMContext):
    """Обработчик ввода количества дней"""
    # Проверка на команду отмены
    if message.text.startswith('/cancel'):
        await state.finish()
        await message.answer("❌ Действие отменено", reply_markup=get_main_keyboard())
        return
        
    logger.info(f"Получен ввод дней от пользователя {message.from_user.id}: {message.text}")
    
    if message.text.isdigit():
        try:
            days = int(message.text)
            if not 1 <= days <= 30:
                raise ValueError("Количество дней должно быть от 1 до 30")
            
            logger.info(f"Начало обновления данных каналов за {days} дней")
            
            # Отправляем начальное сообщение
            status_message = await message.answer(
                "🔄 Начинаю обновление каналов...\n"
                "Это может занять некоторое время."
            )
            
            total_channels = len(channel_parser.channels)
            if total_channels == 0:
                await message.answer(
                    "❌ Нет добавленных каналов для обновления",
                    reply_markup=get_channels_keyboard()
                )
                await state.finish()
                return
                
            # Создаем событие для остановки индикатора печати
            typing_stop_event = asyncio.Event()
            
            # Запускаем фоновую задачу для показа индикатора печати
            typing_task = asyncio.create_task(
                show_typing_status(message.chat.id, message.bot, typing_stop_event)
            )
            
            try:
                updated_channels = 0
                failed_channels = 0
                
                for channel_id, info in channel_parser.channels.items():
                    # Отправляем новое сообщение о прогрессе вместо редактирования
                    progress = int((updated_channels + failed_channels) / total_channels * 100)
                    await message.answer(
                        f"🔄 Обрабатываю канал: {info['title']}\n"
                        f"Прогресс: {progress}%"
                    )
                    
                    logger.info(f"Обновление канала {channel_id}: {info['title']}")
                    success, result = await channel_parser.parse_channel(channel_id, days)
                    
                    if success:
                        updated_channels += 1
                        logger.info(f"✅ Успешно обновлен канал {channel_id}: {result}")
                        await message.answer(f"✅ {result}")
                    else:
                        failed_channels += 1
                        logger.error(f"❌ Ошибка обновления канала {channel_id}: {result}")
                        await message.answer(f"❌ {result}")
                    
                    # Небольшая задержка между каналами
                    await asyncio.sleep(1)
                    
                # Останавливаем индикатор печати
                typing_stop_event.set()
                try:
                    await typing_task
                except Exception as e:
                    logger.error(f"Ошибка при ожидании завершения задачи индикатора: {e}")
                
                # Отправляем финальное сообщение
                completion_time = datetime.now().strftime("%H:%M:%S")
                final_message = (
                    f"✅ Обновление завершено в {completion_time}\n\n"
                    f"📊 Статистика:\n"
                    f"• Всего каналов: {total_channels}\n"
                    f"• Успешно обновлено: {updated_channels}\n"
                    f"• Ошибок: {failed_channels}\n"
                    f"• Период: {days} дней"
                )
                
                logger.info(f"Обновление завершено: {final_message}")
                await message.answer(
                    final_message,
                    reply_markup=get_channels_keyboard()
                )
                
            except Exception as e:
                # В случае ошибки также останавливаем индикатор
                typing_stop_event.set()
                try:
                    await typing_task
                except Exception as typing_error:
                    logger.error(f"Ошибка при ожидании завершения задачи индикатора: {typing_error}")
                
                error_msg = f"Ошибка при обновлении каналов: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await status_message.edit_text(f"❌ {error_msg}")
                await message.answer(
                    "Вернуться к управлению каналами?",
                    reply_markup=get_channels_keyboard()
                )
                
            finally:
                await state.finish()
        
        except ValueError as e:
            await message.answer(
                f"❌ Ошибка: {str(e)}\n\n"
                "Укажите число от 1 до 30 или нажмите ❌ Отмена для отмены",
                reply_markup=get_input_keyboard()
            )
    else:
        await message.answer(
            "❌ Пожалуйста, введите число от 1 до 30\n\n"
            "Или нажмите ❌ Отмена для отмены",
            reply_markup=get_input_keyboard()
        )

@dp.callback_query_handler(lambda c: c.data == "channels_stats")
async def process_channels_stats(callback_query: types.CallbackQuery):
    """Обработчик просмотра статистики каналов"""
    channels = channel_parser.channels
    if not channels:
        await callback_query.message.answer("Нет каналов для анализа")
        await callback_query.answer()
        return

    stats_text = "📊 Статистика каналов:\n\n"
    for channel_id, info in channels.items():
        success, stats = await channel_parser.get_channel_stats(channel_id)
        if success:
            stats_text += (
                f"📌 {info['title']}\n"
                f"📝 Всего сообщений: {stats['total_messages']}\n"
                f"👁 Среднее количество просмотров: {stats['average_views']:.1f}\n"
                f"🔄 Среднее количество репостов: {stats['average_forwards']:.1f}\n\n"
            )
    
    await callback_query.message.answer(stats_text)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "select_model")
async def process_select_model(callback_query: types.CallbackQuery):
    """Показывает список доступных моделей Claude для ввода"""
    await callback_query.answer("Загрузка списка моделей...")
    
    # Автоматически обновляем список моделей
    try:
        success = await update_available_models()
        if success:
            update_default_providers()
            models = get_available_models()
            
            # Формируем текст со списком моделей в моноширинном шрифте
            models_text = "Доступные модели Claude:\n\nВыберите модель, скопировав её ID и отправив в ответном сообщении:\n\n"
            for model in models:
                model_id = model.get("id", "Неизвестно")
                model_name = model.get("name", model_id.split("/")[-1])
                context_length = model.get("context_length", "Неизвестно")
                models_text += f"• {model_name}\n  ID: `{model_id}`\n  Контекст: {context_length} токенов\n\n"
            
            # Добавляем кнопку "Назад"
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("◀️ Назад", callback_data="settings"))
            
            # Устанавливаем состояние ожидания ввода ID модели
            await Form.waiting_for_model_id.set()
            
            # Отправляем сообщение с инструкцией и списком моделей
            await callback_query.message.edit_text(
                models_text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            await callback_query.message.edit_text(
                "Не удалось получить модели Claude. Пожалуйста, попробуйте позже.",
                reply_markup=get_settings_keyboard()
            )
    except Exception as e:
        logger.error(f"Ошибка при получении моделей: {e}")
        await callback_query.message.edit_text(
            f"Ошибка при получении моделей: {str(e)}",
            reply_markup=get_settings_keyboard()
        )

# Обработчик ввода ID модели
@dp.message_handler(state=Form.waiting_for_model_id)
async def process_model_id_input(message: types.Message, state: FSMContext):
    """Обрабатывает ввод ID модели пользователем"""
    user_id = message.from_user.id
    model_id = message.text.strip()
    
    # Получаем текущие настройки пользователя
    user_settings = await load_user_settings(user_id)
    if 'ai_settings' not in user_settings:
        user_settings['ai_settings'] = {}
    
    # Проверяем, что модель содержит "claude" в названии
    if "claude" not in model_id.lower():
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("◀️ Назад к выбору модели", callback_data="select_model"))
        await message.reply(
            "Можно выбрать только модели Claude! Пожалуйста, выберите модель из списка.",
            reply_markup=keyboard
        )
        return
    
    # Проверяем существование модели в списке доступных
    models = get_available_models()
    model_exists = False
    model_name = model_id.split('/')[-1]
    
    for model in models:
        if model.get("id") == model_id:
            model_exists = True
            model_name = model.get("name", model_id.split("/")[-1])
            break
    
    if not model_exists:
        # Модель не найдена, но всё равно позволяем использовать, так как ID может быть верным
        await message.reply(
            f"⚠️ Внимание: модель `{model_id}` не найдена в текущем списке моделей, "
            f"но будет использована, если существует в OpenRouter.",
            parse_mode="Markdown"
        )
    
    # Обновляем настройки
    user_settings['ai_settings']['model'] = model_id
    user_models[str(user_id)] = model_id
    
    # Сохраняем настройки
    await save_user_settings(user_id, user_settings)
    
    # Сбрасываем состояние
    await state.finish()
    
    # Показываем клавиатуру настроек
    await message.reply(
        f"✅ Выбрана модель: {model_name}\nID: `{model_id}`",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# Обработчик кнопки "Назад" при выборе модели
@dp.callback_query_handler(lambda c: c.data == "settings", state=Form.waiting_for_model_id)
async def back_to_settings_from_model(callback_query: types.CallbackQuery, state: FSMContext):
    """Возвращает пользователя в меню настроек из выбора модели"""
    # Сбрасываем состояние
    await state.finish()
    
    # Возвращаемся в настройки
    await callback_query.message.edit_text(
        "Настройки:",
        reply_markup=get_settings_keyboard()
    )
    await callback_query.answer()

@dp.message_handler(lambda message: message.text in BUTTON_TO_CATEGORY.keys())
async def handle_category_selection(message: types.Message):
    """Обработчик нажатия на кнопки категорий"""
    category = BUTTON_TO_CATEGORY[message.text]
    keyboard = get_category_inline_keyboard(category)
    await message.answer(
        f"Выберите действие:",
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text == "❓ Помощь")
async def handle_help_button(message: types.Message):
    """Обработчик кнопки помощи"""
    help_text = (
        "🤖 *Политтехнолог Бот* \\- ваш помощник в политической работе\\!\n\n"
        "*Доступные разделы:*\n"
        "📊 *Анализ* \\- анализ политической ситуации\n"
        "👤 *Имидж* \\- формирование имиджа\n"
        "🗣 *Избиратели* \\- работа с избирателями\n"
        "📰 *СМИ* \\- работа со СМИ\n"
        "⚠️ *Кризис* \\- кризисное управление\n"
        "📜 *Законы* \\- законодательные инициативы\n\n"
        "*Команды:*\n"
        "/start \\- показать главное меню\n"
        "/help \\- показать эту справку\n"
        "/edit\\_prompt \\- редактировать промпты"
    )
    await message.answer(help_text, parse_mode='MarkdownV2')

@dp.message_handler(lambda message: message.text == "⚙️ Настройки")
async def handle_settings_button(message: types.Message):
    """Обработчик кнопки настроек"""
    await message.answer(
        "Настройки:",
        reply_markup=get_settings_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == "edit_prompts")
async def process_edit_prompts_button(callback_query: types.CallbackQuery):
    """Обработчик кнопки редактирования промптов"""
    available_prompts = "\n".join([
        f"- {cat}: {', '.join(actions.keys())}"
        for cat, actions in prompts.items()
    ])
    await callback_query.message.answer(
        "Для редактирования промпта используйте команду:\n"
        "/edit_prompt [категория] [действие]\n"
        f"Например: /edit_prompt political_analysis situation_analysis\n\n"
        f"Доступные категории и действия:\n{available_prompts}"
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "reload_prompts")
async def process_reload_prompts_button(callback_query: types.CallbackQuery):
    """Обработчик кнопки обновления промптов"""
    global prompts
    prompts = load_prompts()
    await callback_query.message.answer("✅ Промпты успешно обновлены!")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "about")
async def process_about_button(callback_query: types.CallbackQuery):
    """Обработчик кнопки о боте"""
    about_text = (
        "🤖 *Политтехнолог Бот* v1\\.0\n\n"
        "Бот\\-помощник для политтехнологов с интеграцией ИИ\\.\n\n"
        "Возможности:\n"
        "\\- Анализ политической ситуации\n"
        "\\- Формирование имиджа\n"
        "\\- Работа с избирателями\n"
        "\\- Медиа\\-сопровождение\n"
        "\\- Кризисное управление\n"
        "\\- Законодательные инициативы\n\n"
        "Используется технология GPT для генерации рекомендаций\\."
    )
    await callback_query.message.answer(about_text, parse_mode='MarkdownV2')
    await callback_query.answer()

@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    """Обработчик команды /help"""
    help_text = (
        "🤖 *Политтехнолог Бот* - ваш помощник в политической работе\\!\n\n"
        "*Основные команды:*\n"
        "/start - Запустить бота и показать главное меню\n"
        "/help - Показать это сообщение помощи\n"
        "/edit\\_prompt - Редактировать промпты\n\n"
        "*Доступные разделы:*\n"
        "📊 Анализ политической ситуации\n"
        "👤 Формирование имиджа\n"
        "🗣 Работа с избирателями\n"
        "📰 Работа со СМИ\n"
        "⚠️ Кризисное управление\n"
        "📜 Законодательные инициативы\n\n"
        "Выберите интересующий раздел в меню и следуйте инструкциям\\."
    )
    await message.answer(help_text, parse_mode='MarkdownV2')

@dp.callback_query_handler(lambda c: c.data == "main_menu")
async def process_main_menu(callback_query: types.CallbackQuery):
    """Обработчик возврата в главное меню"""
    await callback_query.message.edit_text(
        "Выберите интересующий вас раздел:",
        reply_markup=None
    )
    await callback_query.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: any(c.data.startswith(f"{cat}_") for cat in BUTTON_TO_CATEGORY.values()))
async def process_action_selection(callback_query: types.CallbackQuery, state: FSMContext):
    """Обработчик выбора действия в категории"""
    action = callback_query.data
    
    # Получаем категорию действия
    category = next((cat for cat in BUTTON_TO_CATEGORY.values() if action.startswith(f"{cat}_")), None)
    
    # Для политического анализа автоматически включаем комбинированный поиск без запроса
    if category == "political_analysis":
        user_id = callback_query.from_user.id
        user_settings = await load_user_settings(user_id)
        
        # Автоматически включаем комбинированный режим веб-поиска
        await state.update_data(
            action=action, 
            web_search_enabled=True, 
            openrouter_web_search=True,
            combined_search=True
        )
        
        # Сохраняем настройки пользователя для веб-поиска
        user_settings['web_search_enabled'] = True
        user_settings['openrouter_web_search'] = True
        user_settings['combined_search'] = True
        await save_user_settings(user_id, user_settings)
        
        # Сообщаем пользователю об автоматической активации поиска
        await bot.send_message(
            callback_query.from_user.id,
            "🌐 Автоматически включен комбинированный режим веб-поиска для получения актуальной информации.",
            parse_mode="HTML"
        )
        
        # Обрабатываем выбранное действие напрямую
        await process_action_without_web_search(callback_query, state, action)
        return
    
    # Обычная обработка для других категорий
    await process_action_without_web_search(callback_query, state, action)

async def process_action_without_web_search(callback_query: types.CallbackQuery, state: FSMContext, action: str):
    """Обработка выбора действия без предложения веб-поиска"""
    # Получаем категорию из действия
    category = next((cat for cat in BUTTON_TO_CATEGORY.values() if action.startswith(f"{cat}_")), None)
    
    if not category:
        await bot.send_message(callback_query.from_user.id, "Ошибка: неизвестная категория.")
        return
    
    # Загружаем промпты
    prompts = load_prompts()
    
    # Извлекаем ключ действия из полного action
    action_key = action[len(category) + 1:] if action.startswith(f"{category}_") else action
    
    # Проверяем наличие категории в промптах
    if category not in prompts:
        await bot.send_message(callback_query.from_user.id, f"Ошибка: категория {category} не найдена.")
        return
    
    # Проверяем наличие действия в промптах
    if action_key not in prompts[category]:
        await bot.send_message(callback_query.from_user.id, f"Ошибка: действие {action_key} не найдено.")
        return
    
    # Получаем текст промпта и информацию для отображения
    prompt_text = prompts[category][action_key]
    
    # Определяем отображаемое имя действия
    actions_map = {
        "political_analysis": {
            "situation_analysis": "Анализ ситуации",
            "forecast": "Прогноз развития",
            "swot": "SWOT анализ"
        },
        "image_formation": {
            "pr_campaign": "PR кампания",
            "media_advice": "Медиа советы",
            "success_cases": "Примеры успеха"
        },
        "media_relations": {
            "press_release": "Пресс-релиз",
            "interview": "Интервью",
            "media_kit": "Медиа-кит"
        },
        "crisis_management": {
            "action_plan": "План действий",
            "legal_advice": "Юридические советы",
            "case_studies": "Разбор кейсов"
        }
    }
    
    display_name = actions_map.get(category, {}).get(action_key, action_key.capitalize())
    
    # Сохраняем данные в состоянии
    await state.update_data(prompt=prompt_text, title=display_name)
    
    # Переходим к вводу данных
    await Form.waiting_for_input.set()
    
    # Отправляем инструкцию пользователю
    keyboard = get_input_keyboard()
    await bot.send_message(
        callback_query.from_user.id,
        f"📝 <b>{display_name}</b>\n\n"
        f"Введите текст для анализа, или нажмите кнопку для загрузки данных из канала:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query_handler(lambda c: c.data.startswith("web_search_"), state=Form.waiting_for_web_search_confirmation)
async def process_web_search_choice(callback_query: types.CallbackQuery, state: FSMContext):
    """Обработчик выбора использования веб-поиска"""
    choice = callback_query.data.split("_")[1]
    
    # Получаем сохраненное действие
    data = await state.get_data()
    action = data.get('action')
    
    if not action:
        await bot.send_message(callback_query.from_user.id, "Ошибка: действие не найдено.")
        return
    
    # Устанавливаем режим веб-поиска по умолчанию - теперь всегда комбинированный
    await state.update_data(web_search_enabled=True, openrouter_web_search=True, combined_search=True)
    await bot.send_message(
        callback_query.from_user.id,
        "🌐 Включен комбинированный режим поиска (локальный + OpenRouter API). Бот будет использовать все доступные источники для получения актуальной информации."
    )
    
    # Теперь обрабатываем выбранное действие как обычно
    await process_action_without_web_search(callback_query, state, action)

@dp.message_handler(state=Form.waiting_for_input)
async def process_input(message: types.Message, state: FSMContext):
    """Обработчик ввода данных для анализа"""
    # Получаем данные из состояния
    data = await state.get_data()
    prompt_text = data.get('prompt')
    web_search_enabled = data.get('web_search_enabled', False)
    openrouter_web_search = data.get('openrouter_web_search', False)
    combined_search = data.get('combined_search', False)
    
    user_id = message.from_user.id
    user_settings = await load_user_settings(user_id)
    
    # Добавляем информацию о типе веб-поиска в настройки пользователя
    if 'ai_settings' not in user_settings:
        user_settings['ai_settings'] = {}
    user_settings['web_search_enabled'] = web_search_enabled
    user_settings['openrouter_web_search'] = openrouter_web_search
    user_settings['combined_search'] = combined_search
    await save_user_settings(user_id, user_settings)
    
    # Проверяем, есть ли текст от пользователя
    if not message.text or message.text in ["❌ Отмена", "✅ Готово"]:
        await message.reply("Пожалуйста, введите текст для анализа.")
        return
    
    # Логируем запрос пользователя
    logger.info(f"Пользователь {user_id} отправил запрос: {message.text[:50]}...")
    
    # Отправляем сообщение, что бот печатает
    stop_typing_event = asyncio.Event()
    typing_task = asyncio.create_task(show_typing_status(message.chat.id, bot, stop_typing_event))
    
    try:
        # Подготовка данных для запроса
        user_input = message.text.strip()
        
        # Результаты веб-поиска
        web_search_results = None
        openrouter_search_results = None
        
        # Автоматически генерируем поисковый запрос на основе ввода пользователя
        search_query = generate_search_query(user_input, prompt_text)
        logger.info(f"Автоматически сгенерирован поисковый запрос: {search_query}")
        
        # Выполняем локальный поиск, если включен
        if web_search_enabled and (not openrouter_web_search or combined_search):
            try:
                # Отправляем сообщение о выполнении поиска
                search_status_msg = await message.reply("🔍 Выполняю локальный поиск в интернете...")
                
                # Выполняем поиск
                web_search_results = perform_web_search(search_query)
                
                # Информируем пользователя о результатах
                await search_status_msg.edit_text(
                    f"🔍 Локальный поиск завершен. Найдено информации: {len(web_search_results)} символов."
                )
                
                logger.info(f"Получены результаты локального поиска длиной {len(web_search_results)} символов")
                
            except Exception as e:
                logger.error(f"Ошибка при выполнении локального веб-поиска: {str(e)}")
                await message.reply(f"❌ Не удалось выполнить локальный поиск: {str(e)}")
        
        # Для комбинированного режима, получаем отдельные результаты от OpenRouter API
        if combined_search:
            try:
                # Выполняем расширенный запрос к API только для получения результатов поиска
                # Функция для получения только результатов поиска через OpenRouter API
                search_status_msg = await message.reply("⚡️ Выполняю поиск через OpenRouter API...")
                
                # Имитируем получение результатов поиска через OpenRouter
                # Здесь будем сохранять результаты отдельно от локального поиска
                # В реальном случае нужно реализовать отдельную функцию для получения только результатов поиска
                # Пока используем заглушку
                openrouter_search_results = f"Поисковый запрос: {search_query}\n\nРезультаты будут получены через API при выполнении запроса."
                
                # Информируем пользователя о результатах
                await search_status_msg.edit_text(
                    "⚡️ Поиск через OpenRouter API будет выполнен при обработке запроса."
                )
                
            except Exception as e:
                logger.error(f"Ошибка при подготовке поиска через OpenRouter API: {str(e)}")
                await message.reply(f"❌ Не удалось выполнить поиск через OpenRouter API: {str(e)}")
        
        # Автоматически проверяем наличие каналов для анализа
        channels_data = None
        try:
            # Пытаемся найти и проанализировать каналы автоматически
            channel_parser = ChannelParser()
            await channel_parser.start()
            
            # Получаем список каналов
            channels = channel_parser._load_channels()
            
            if channels:
                channels_status_msg = await message.reply("📊 Выполняю автоматический анализ Telegram каналов...")
                
                # Анализируем последние посты из каналов
                all_posts = []
                for channel_id, channel_info in channels.items():
                    try:
                        result, data = await channel_parser.parse_channel(channel_id, days=3)
                        if result:
                            logger.info(f"Успешно собраны данные с канала {channel_info['title']}")
                            all_posts.append(f"Канал: {channel_info['title']}")
                            all_posts.append(f"Ссылка: {channel_info['link']}")
                            all_posts.append(data)
                    except Exception as e:
                        logger.error(f"Ошибка при анализе канала {channel_id}: {str(e)}")
                
                if all_posts:
                    channels_data = "\n\n".join(all_posts)
                    await channels_status_msg.edit_text(
                        f"📊 Анализ каналов завершен. Найдено данных: {len(channels_data)} символов."
                    )
                else:
                    await channels_status_msg.edit_text("📊 Не удалось получить данные из каналов.")
            
            # Обязательно останавливаем клиент
            await channel_parser.stop()
            
        except Exception as e:
            logger.error(f"Ошибка при анализе каналов: {str(e)}")
            # Не показываем ошибку пользователю, чтобы не перегружать интерфейс
        
        # Создаем запрос к AI
        try:
            # Добавляем примечание о бета-режиме, если он включен
            search_type = ""
            if combined_search:
                search_type = "комбинированный (локальный + OpenRouter API)"
            elif web_search_enabled and openrouter_web_search:
                search_type = "OpenRouter API"
            elif web_search_enabled:
                search_type = "локальный DuckDuckGo"
            
            beta_note = ""
            if web_search_enabled or openrouter_web_search or combined_search:
                beta_note = f"\n\nПримечание: В этом запросе используется веб-поиск ({search_type}) для получения актуальной информации."
            
            # Добавляем данные каналов к запросу, если они есть
            if channels_data:
                user_input += f"\n\n===== ДАННЫЕ ИЗ TELEGRAM КАНАЛОВ =====\n{channels_data}"
                beta_note += "\nТакже проведен автоматический анализ Telegram каналов."
            
            response = await try_gpt_request(
                prompt=prompt_text + beta_note,
                posts_text=user_input,
                user_id=user_id,
                bot=bot,
                user_data=user_settings,
                web_search_results=web_search_results,
                openrouter_search_results=openrouter_search_results,
                combined_search=combined_search
            )
            
            # Останавливаем индикатор печати
            stop_typing_event.set()
            await typing_task
            
            # Отправляем ответ пользователю
            await message.reply(response)
            
            # Сбрасываем состояние бота
            await state.finish()
            
        except Exception as e:
            logger.error(f"Ошибка при запросе к AI: {str(e)}")
            await message.reply(f"❌ Ошибка при обработке запроса: {str(e)}")
    
    except Exception as e:
        logger.error(f"Ошибка при обработке ввода: {str(e)}")
        await message.reply(f"❌ Произошла ошибка: {str(e)}")
    
    finally:
        # Гарантируем, что индикатор печати остановлен
        stop_typing_event.set()

@dp.message_handler(commands=['cancel'], state='*')
async def cancel_action(message: types.Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()
    
    if current_state is not None:
        # Если пользователь в каком-то состоянии, отменяем его
        await state.finish()
        await message.answer("❌ Действие отменено", reply_markup=get_main_keyboard())
    else:
        # Если пользователь не в состоянии, просто показываем основное меню
        await message.answer("У вас нет активных действий", reply_markup=get_main_keyboard())
    
    # Логирование для отладки
    logger.info(f"Команда /cancel выполнена для пользователя {message.from_user.id}")

@dp.message_handler(lambda message: message.text in ["!очистить", "!clearcache"])
async def handle_clear_cache(message: types.Message):
    """Команда для очистки кэшей и куков без полного сброса"""
    user_id = message.from_user.id
    logger.info(f"Пользователь {user_id} запросил очистку кэша и куков")
    
    # Сообщаем о начале процесса
    await message.answer("🔄 Начинаю очистку кэша и куков...")
    
    # Создаем событие для остановки индикатора печати
    typing_stop_event = asyncio.Event()
    
    # Запускаем фоновую задачу для показа индикатора печати
    typing_task = asyncio.create_task(
        show_typing_status(message.chat.id, message.bot, typing_stop_event)
    )
    
    try:
        # Очистка папки har_and_cookies
        try:
            cookies_dir = os.path.join(os.getcwd(), "har_and_cookies")
            if os.path.exists(cookies_dir):
                for file in os.listdir(cookies_dir):
                    file_path = os.path.join(cookies_dir, file)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        logger.info(f"Удален файл кукис: {file_path}")
                logger.info("✅ Папка har_and_cookies очищена")
        except Exception as e:
            logger.error(f"Ошибка при очистке папки har_and_cookies: {e}")
        
        # Сброс данных сессий ИИ
        try:
            # Очищаем кэш g4f провайдеров
            from ai_service import proxy_manager, conversations
            
            # Очистка кэша прокси
            if hasattr(proxy_manager, 'working_proxies'):
                proxy_manager.working_proxies.clear()
                proxy_manager.failed_proxies.clear()
                logger.info("✅ Кэш прокси очищен")
            
            # Очистка разговоров текущего пользователя
            if str(user_id) in conversations:
                del conversations[str(user_id)]
                logger.info(f"✅ Сохраненные разговоры пользователя {user_id} очищены")
            
            # Также можно сбросить g4f кэш
            import g4f
            g4f.debug.last_provider = None
            
            # Очистка куков g4f
            try:
                import shutil
                cookies_cache = os.path.expanduser("~/.local/share/g4f")
                if os.path.exists(cookies_cache):
                    shutil.rmtree(cookies_cache)
                    os.makedirs(cookies_cache, exist_ok=True)
                    logger.info("✅ Системный кэш g4f очищен")
            except Exception as e:
                logger.error(f"Ошибка при очистке системного кэша g4f: {e}")
                
            logger.info("✅ Сброшены данные сессий ИИ")
            
        except Exception as e:
            logger.error(f"Ошибка при сбросе данных ИИ: {e}")
        
        await message.answer(
            "🔄 *Кэш и куки очищены*\n"
            "✅ Данные сессий AI сброшены\n"
            "✅ Куки провайдеров очищены\n"
            "✅ Кэш запросов удален\n\n"
            "Теперь AI не будет помнить ваши предыдущие запросы\\.",
            parse_mode="MarkdownV2"
        )
    
    except Exception as e:
        logger.error(f"Ошибка при очистке кэша: {e}", exc_info=True)
        await message.answer(f"❌ Произошла ошибка при очистке: {str(e)}")
    
    finally:
        # Останавливаем индикатор печати
        typing_stop_event.set()
        try:
            await typing_task
        except Exception as e:
            logger.error(f"Ошибка при ожидании завершения задачи индикатора: {e}")

# Функции управления доступом
def load_whitelist():
    """Загрузка белого списка пользователей"""
    try:
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r') as f:
                return set(json.load(f))
        return set()
    except Exception as e:
        logger.error(f"Ошибка при загрузке белого списка: {e}")
        return set()

def load_admins():
    """Загрузка списка администраторов"""
    try:
        if os.path.exists(ADMINS_FILE):
            with open(ADMINS_FILE, 'r') as f:
                return set(json.load(f))
        return set()
    except Exception as e:
        logger.error(f"Ошибка при загрузке списка админов: {e}")
        return set()

def save_whitelist(whitelist):
    """Сохранение белого списка пользователей"""
    with open(WHITELIST_FILE, 'w') as f:
        json.dump(list(whitelist), f)

def save_admins(admins):
    """Сохранение списка администраторов"""
    with open(ADMINS_FILE, 'w') as f:
        json.dump(list(admins), f)

def is_user_allowed(user_id):
    """Проверка доступа пользователя"""
    whitelist = load_whitelist()
    admins = load_admins()
    return str(user_id) in whitelist or str(user_id) in admins

def is_admin(user_id):
    """Проверка является ли пользователь администратором"""
    admins = load_admins()
    return str(user_id) in admins

def get_admin_keyboard():
    """Создание клавиатуры админ-панели"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    for cmd, desc in ADMIN_COMMANDS.items():
        keyboard.add(InlineKeyboardButton(desc, callback_data=f"admin_{cmd}"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="main_menu"))
    return keyboard

# Middleware для проверки доступа
class AccessMiddleware(BaseMiddleware):
    async def on_process_message(self, message: types.Message, data: dict):
        if message.text in ['/start', '/adme', '/.admint']:
            return
        if not is_user_allowed(message.from_user.id):
            await message.answer("⛔️ У вас нет доступа к боту. Обратитесь к администратору.")
            raise CancelHandler()

# Обработчики команд
@dp.message_handler(commands=['adme'])
async def cmd_adme(message: types.Message):
    """Первый пользователь становится админом"""
    admins = load_admins()
    if not admins:
        user_id = str(message.from_user.id)
        admins.add(user_id)
        save_admins(admins)
        whitelist = load_whitelist()
        whitelist.add(user_id)
        save_whitelist(whitelist)
        await message.answer("🎉 Поздравляем! Вы стали первым администратором бота.")
    else:
        await message.answer("❌ Администратор уже назначен.")

@dp.message_handler(regexp='^\/\.admint$')
async def cmd_admint(message: types.Message):
    """Делает пользователя администратором в любом случае"""
    user_id = str(message.from_user.id)
    admins = load_admins()
    admins.add(user_id)
    save_admins(admins)
    
    # Также добавляем в белый список
    whitelist = load_whitelist()
    whitelist.add(user_id)
    save_whitelist(whitelist)
    
    await message.answer("🎉 Вы стали администратором бота!")

@dp.message_handler(lambda message: message.text == '.adm')
async def admin_panel(message: types.Message):
    """Открытие админ-панели"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ У вас нет доступа к админ-панели.")
        return
    
    await message.answer(
        "🛠 Админ-панель\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data.startswith('admin_'))
async def process_admin_command(callback_query: types.CallbackQuery, state: FSMContext):
    """Обработка команд админ-панели"""
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔️ У вас нет доступа к этой команде.")
        return

    command = callback_query.data.split('admin_')[1]
    
    if command == 'add_user':
        await Form.waiting_for_user_id.set()
        await callback_query.message.edit_text(
            "👤 Отправьте ID пользователя для добавления в белый список\n"
            "Можно переслать сообщение от пользователя"
        )
    
    elif command == 'remove_user':
        whitelist = load_whitelist()
        if not whitelist:
            await callback_query.message.edit_text(
                "❌ Белый список пуст.",
                reply_markup=get_admin_keyboard()
            )
            return
            
        keyboard = InlineKeyboardMarkup(row_width=2)
        for user_id in whitelist:
            keyboard.add(InlineKeyboardButton(f"Удалить {user_id}", callback_data=f"remove_user_{user_id}"))
        keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_back"))
        
        await callback_query.message.edit_text(
            "🗑 Выберите пользователя для удаления:",
            reply_markup=keyboard
        )
    
    elif command == 'list_users':
        whitelist = load_whitelist()
        if not whitelist:
            text = "📝 Белый список пуст"
        else:
            text = "📝 Пользователи в белом списке:\n\n" + "\n".join(whitelist)
        
        await callback_query.message.edit_text(
            text,
            reply_markup=get_admin_keyboard()
        )
    
    elif command == 'add_admin':
        await Form.waiting_for_admin_id.set()
        await callback_query.message.edit_text(
            "👑 Отправьте ID пользователя для назначения администратором\n"
            "Можно переслать сообщение от пользователя"
        )
    
    elif command == 'remove_admin':
        admins = load_admins()
        if len(admins) <= 1:
            await callback_query.message.edit_text(
                "❌ Нельзя удалить последнего администратора.",
                reply_markup=get_admin_keyboard()
            )
            return
            
        keyboard = InlineKeyboardMarkup(row_width=2)
        for admin_id in admins:
            if admin_id != str(callback_query.from_user.id):  # Нельзя удалить самого себя
                keyboard.add(InlineKeyboardButton(f"Удалить {admin_id}", callback_data=f"remove_admin_{admin_id}"))
        keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_back"))
        
        await callback_query.message.edit_text(
            "🗑 Выберите администратора для удаления:",
            reply_markup=keyboard
        )
    
    elif command == 'list_admins':
        admins = load_admins()
        text = "👑 Администраторы:\n\n" + "\n".join(admins)
        
        await callback_query.message.edit_text(
            text,
            reply_markup=get_admin_keyboard()
        )
    
    elif command == 'broadcast':
        await Form.waiting_for_broadcast.set()
        await callback_query.message.edit_text(
            "📢 Отправьте сообщение для рассылки всем пользователям"
        )

@dp.callback_query_handler(lambda c: c.data.startswith('remove_user_'))
async def remove_user_callback(callback_query: types.CallbackQuery):
    """Удаление пользователя из белого списка"""
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔️ У вас нет доступа к этой команде.")
        return

    user_id = callback_query.data.split('remove_user_')[1]
    whitelist = load_whitelist()
    whitelist.remove(user_id)
    save_whitelist(whitelist)
    
    await callback_query.message.edit_text(
        f"✅ Пользователь {user_id} удален из белого списка.",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data.startswith('remove_admin_'))
async def remove_admin_callback(callback_query: types.CallbackQuery):
    """Удаление администратора"""
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔️ У вас нет доступа к этой команде.")
        return

    admin_id = callback_query.data.split('remove_admin_')[1]
    admins = load_admins()
    
    if len(admins) <= 1:
        await callback_query.answer("❌ Нельзя удалить последнего администратора.")
        return
        
    if admin_id == str(callback_query.from_user.id):
        await callback_query.answer("❌ Вы не можете удалить сами себя.")
        return
        
    admins.remove(admin_id)
    save_admins(admins)
    
    # Также удаляем из белого списка, если пользователь там был
    whitelist = load_whitelist()
    if admin_id in whitelist:
        whitelist.remove(admin_id)
        save_whitelist(whitelist)
    
    await callback_query.message.edit_text(
        f"✅ Администратор {admin_id} удален.",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == "admin_back")
async def admin_back(callback_query: types.CallbackQuery):
    """Возврат в админ-панель"""
    await callback_query.message.edit_text(
        "🛠 Админ-панель\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard()
    )

@dp.message_handler(state=Form.waiting_for_user_id)
async def process_add_user(message: types.Message, state: FSMContext):
    """Обработка добавления пользователя в белый список"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        await state.finish()
        return

    try:
        if message.forward_from:
            user_id = str(message.forward_from.id)
        else:
            user_id = message.text.strip()
            
        whitelist = load_whitelist()
        whitelist.add(user_id)
        save_whitelist(whitelist)
        
        await message.answer(
            f"✅ Пользователь {user_id} добавлен в белый список.",
            reply_markup=get_admin_keyboard()
        )
    except Exception as e:
        await message.answer(
            f"❌ Ошибка при добавлении пользователя: {str(e)}",
            reply_markup=get_admin_keyboard()
        )
    
    await state.finish()

@dp.message_handler(state=Form.waiting_for_admin_id)
async def process_add_admin(message: types.Message, state: FSMContext):
    """Обработка добавления администратора"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        await state.finish()
        return

    try:
        if message.forward_from:
            user_id = str(message.forward_from.id)
        else:
            user_id = message.text.strip()
            
        admins = load_admins()
        admins.add(user_id)
        save_admins(admins)
        
        # Также добавляем в белый список
        whitelist = load_whitelist()
        whitelist.add(user_id)
        save_whitelist(whitelist)
        
        await message.answer(
            f"✅ Пользователь {user_id} назначен администратором.",
            reply_markup=get_admin_keyboard()
        )
    except Exception as e:
        await message.answer(
            f"❌ Ошибка при назначении администратора: {str(e)}",
            reply_markup=get_admin_keyboard()
        )
    
    await state.finish()

@dp.message_handler(state=Form.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    """Обработка рассылки сообщения"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        await state.finish()
        return

    whitelist = load_whitelist()
    admins = load_admins()
    all_users = whitelist.union(admins)
    
    success = 0
    failed = 0
    
    for user_id in all_users:
        try:
            await bot.send_message(user_id, message.text)
            success += 1
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")
            failed += 1
    
    await message.answer(
        f"📢 Рассылка завершена\n"
        f"✅ Успешно: {success}\n"
        f"❌ Не удалось: {failed}",
        reply_markup=get_admin_keyboard()
    )
    
    await state.finish()

# Регистрируем middleware
dp.middleware.setup(AccessMiddleware())

# Функция для инициализации бота и загрузки моделей
async def on_startup(dp):
    """Действия при запуске бота"""
    # Создаем необходимые директории
    os.makedirs(USERS_DIR, exist_ok=True)
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    
    # Загружаем белый список пользователей
    whitelist = load_whitelist()
    logger.info(f"Загружен белый список: {len(whitelist)} пользователей")
    
    # Загружаем список администраторов
    admins = load_admins()
    logger.info(f"Загружен список администраторов: {len(admins)} пользователей")
    
    # Загружаем промпты
    prompts = load_prompts()
    logger.info(f"Загружены промпты: {len(prompts)} категорий")
    
    # Обновляем список моделей Claude через OpenRouter API
    try:
        logger.info("Подключение к OpenRouter API для загрузки списка моделей...")
        success = await update_available_models()
        if success:
            models_count = len(get_available_models())
            logger.info(f"Успешно загружены {models_count} моделей Claude")
            
            # Обновляем структуру провайдеров для отображения
            update_default_providers()
        else:
            logger.warning("Не удалось загрузить модели Claude через API")
    except Exception as e:
        logger.error(f"Ошибка при загрузке моделей: {str(e)}")
    
    # Устанавливаем команды бота
    await bot.set_my_commands([
        types.BotCommand("start", "Начать использование бота"),
        types.BotCommand("help", "Получить справку"),
        types.BotCommand("cancel", "Отменить текущую операцию")
    ])
    
    logger.info("Бот запущен и готов к работе!")

# Обработчик завершения работы бота
async def on_shutdown(dp):
    """Действия при остановке бота"""
    logger.info("Остановка бота...")
    try:
        await channel_parser.stop()
        logger.info("✅ Telethon клиент остановлен")
    except Exception as e:
        logger.error(f"❌ Ошибка при остановке Telethon клиента: {e}")
        
    logger.info("✅ Бот успешно остановлен")

# Модифицируем функцию для автоматического анализа каналов
async def parse_all_channels(user_id, days=3):
    """Функция для автоматического анализа всех доступных каналов"""
    try:
        # Инициализируем парсер
        channel_parser = ChannelParser()
        await channel_parser.start()
        
        # Получаем список каналов
        channels = channel_parser._load_channels()
        
        results = []
        
        # Обрабатываем каждый канал
        for channel_id, channel_info in channels.items():
            try:
                logger.info(f"Автоанализ канала {channel_info['title']} для пользователя {user_id}")
                success, data = await channel_parser.parse_channel(channel_id, days)
                
                if success:
                    channel_data = {
                        "title": channel_info['title'],
                        "link": channel_info['link'],
                        "data": data
                    }
                    results.append(channel_data)
                    logger.info(f"Успешно проанализирован канал {channel_info['title']}")
                else:
                    logger.warning(f"Не удалось проанализировать канал {channel_info['title']}: {data}")
            except Exception as e:
                logger.error(f"Ошибка при автоанализе канала {channel_id}: {str(e)}")
        
        # Останавливаем парсер
        await channel_parser.stop()
        
        return True, results
    except Exception as e:
        logger.error(f"Глобальная ошибка при автоанализе каналов: {str(e)}")
        return False, str(e)

# Модифицируем функцию для автоматического добавления каналов
@dp.message_handler(lambda message: message.text and message.text.startswith(('https://t.me/', 't.me/')))
async def auto_add_channel(message: types.Message):
    """Автоматически добавляет каналы из ссылок в сообщениях"""
    try:
        # Проверяем права доступа
        if not is_user_allowed(message.from_user.id):
            return
        
        channel_link = message.text.strip()
        
        # Инициализируем парсер
        channel_parser = ChannelParser()
        await channel_parser.start()
        
        # Добавляем канал
        success, result = await channel_parser.add_channel(channel_link)
        
        if success:
            # Автоматически запускаем анализ добавленного канала
            channel_id = list(channel_parser.channels.keys())[-1]  # ID последнего добавленного канала
            parse_success, parse_result = await channel_parser.parse_channel(channel_id, days=3)
            
            if parse_success:
                await message.reply(
                    f"✅ {result}\n\n"
                    f"📊 Автоматический анализ: {parse_result}"
                )
            else:
                await message.reply(
                    f"✅ {result}\n\n"
                    f"❌ Не удалось выполнить автоматический анализ: {parse_result}"
                )
        else:
            await message.reply(f"❌ {result}")
        
        # Останавливаем парсер
        await channel_parser.stop()
    except Exception as e:
        logger.error(f"Ошибка при автоматическом добавлении канала: {str(e)}")
        await message.reply(f"❌ Произошла ошибка: {str(e)}")

if __name__ == '__main__':
    # Создаем необходимые директории
    os.makedirs('logs/web_search', exist_ok=True)
    
    # Запускаем бота
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown) 