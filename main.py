import asyncio
import logging
import os

import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import PeerChannel

from config import api_id, api_hash, bot_token, my_id, proxy_url

# Визначення станів для очікування вводу
class ChannelAdding(StatesGroup):
    waiting_for_channel_id = State()

class MassChannelAdding(StatesGroup):
    waiting_for_channels = State()

class DestinationChannelSetting(StatesGroup):
    waiting_for_destination_channel_id = State()

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Ініціалізація бота та клієнта Telethon
bot = Bot(token=bot_token)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

client = TelegramClient('myGrab.session', api_id, api_hash, catch_up=True)

# Функція для створення бази даних та таблиць
async def init_db():
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS destination (
                id INTEGER PRIMARY KEY
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS last_message_ids (
                channel_id INTEGER PRIMARY KEY,
                last_id INTEGER
            )
        ''')
        await db.commit()
    logger.info("База даних ініціалізована.")

# Функції для роботи з базою даних
async def save_channel(channel_id, channel_title):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('INSERT OR IGNORE INTO channels (id, title) VALUES (?, ?)', (channel_id, channel_title))
        await db.commit()
    logger.info(f"Канал {channel_title} (ID: {channel_id}) збережено у базі даних.")

async def delete_channel(channel_id):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('DELETE FROM channels WHERE id = ?', (channel_id,))
        await db.execute('DELETE FROM last_message_ids WHERE channel_id = ?', (channel_id,))
        await db.commit()
    logger.info(f"Канал з ID {channel_id} видалено з бази даних.")

async def set_destination_channel(channel_id):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('DELETE FROM destination')  # Видалення попереднього каналу-приймача
        if channel_id:
            await db.execute('INSERT INTO destination (id) VALUES (?)', (channel_id,))
            logger.info(f"Канал-приймач з ID {channel_id} встановлено.")
        else:
            logger.info("Канал-приймач видалено.")
        await db.commit()

async def get_destination_channel():
    async with aiosqlite.connect('channels.db') as db:
        cursor = await db.execute('SELECT id FROM destination LIMIT 1')
        row = await cursor.fetchone()
        return row[0] if row else None

async def get_channels():
    async with aiosqlite.connect('channels.db') as db:
        cursor = await db.execute('SELECT id, title FROM channels')
        channels = await cursor.fetchall()
    logger.info(f"Отримано {len(channels)} каналів для моніторингу.")
    return channels

async def get_last_message_id(channel_id):
    async with aiosqlite.connect('channels.db') as db:
        cursor = await db.execute('SELECT last_id FROM last_message_ids WHERE channel_id = ?', (channel_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def update_last_message_id(channel_id, last_id):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('INSERT OR REPLACE INTO last_message_ids (channel_id, last_id) VALUES (?, ?)', (channel_id, last_id))
        await db.commit()
    logger.info(f"last_message_id для каналу {channel_id} оновлено до {last_id}.")

# Функція для отримання історії повідомлень
async def fetch_channel_history(channel_id, limit=1):
    try:
        result = await client(GetHistoryRequest(
            peer=PeerChannel(channel_id),
            limit=limit,  # Отримати останнє повідомлення
            offset_date=None,
            offset_id=0,
            max_id=0,
            min_id=0,
            add_offset=0,
            hash=0
        ))
        logger.info(f"Отримано {len(result.messages)} повідомлень з каналу {channel_id}.")
        return result.messages
    except Exception as e:
        logger.error(f"Помилка при отриманні історії каналу {channel_id}: {str(e)}", exc_info=True)
        return []

# Функція з повторними спробами пересилання
async def safe_forward(message, destination_channel, retries=3, delay=2):
    for attempt in range(1, retries + 1):
        try:
            await message.forward_to(destination_channel)
            return True
        except Exception as e:
            logger.warning(f"Спроба {attempt} переслати повідомлення {message.id} з каналу {message.peer_id.channel_id}: {e}")
            await asyncio.sleep(delay * attempt)  # Експоненційна затримка
    logger.error(f"Не вдалося переслати повідомлення {message.id} після {retries} спроб.")
    return False

# Функція для обмеження швидкості
semaphore = asyncio.Semaphore(5)  # Максимум 5 одночасних пересилань

# Функція для обробки пропущених повідомлень
async def process_missed_messages(channel_id, destination_channel):
    messages = await fetch_channel_history(channel_id)
    if not messages:
        return

    last_id = await get_last_message_id(channel_id)

    for message in reversed(messages):
        if message.id > last_id:
            async with semaphore:
                success = await safe_forward(message, destination_channel)
                if success:
                    logger.info(f"Переслано пропущене повідомлення з каналу {channel_id}, ID повідомлення: {message.id}")
                else:
                    logger.error(f"Не вдалося переслати пропущене повідомлення з каналу {channel_id}, ID: {message.id}")

    # Оновлення last_message_id
    new_last_id = messages[0].id
    await update_last_message_id(channel_id, new_last_id)

# Додана функція add_new_channel
async def add_new_channel(channel_input):
    try:
        # Отримання інформації про канал
        chat = await client.get_entity(channel_input)
        channel_id = chat.id

        # Перевірка, чи це канал або мегагрупа
        if not (chat.broadcast or chat.megagroup):
            logger.error(f"Вказаний ID не є каналом або мегагрупою: {channel_input}")
            return

        # Збереження каналу в базу даних
        await save_channel(channel_id, chat.title)

        # Отримання останнього повідомлення каналу
        messages = await fetch_channel_history(channel_id, limit=1)
        if messages:
            last_id = messages[0].id
            await update_last_message_id(channel_id, last_id)
            logger.info(f"Встановлено last_message_id для каналу {channel_id} на {last_id}")
        else:
            logger.warning(f"Не вдалося отримати останнє повідомлення для каналу {channel_id}")

        logger.info(f"Канал {chat.title} (ID: {channel_id}) додано.")
    except Exception as e:
        logger.error(f"Помилка при додаванні каналу: {str(e)}", exc_info=True)

# Функція для обмеження швидкості
semaphore = asyncio.Semaphore(5)  # Максимум 5 одночасних пересилань

# Оновлений обробник стану додавання каналу
@dp.message_handler(state=ChannelAdding.waiting_for_channel_id)
async def add_channel_handler(message: types.Message, state: FSMContext):
    try:
        channel_input = message.text.strip()
        await add_new_channel(channel_input)
        await message.reply(f"Канал {channel_input} додано.")
    except Exception as e:
        await message.reply(f"Сталася помилка при додаванні каналу: {str(e)}")
        logger.error(f"Помилка при додаванні каналу: {str(e)}", exc_info=True)
    finally:
        await state.finish()

# Доданий обробник нових повідомлень для кожного каналу
@client.on(events.NewMessage())
async def new_message_handler(event):
    try:
        channel_id = event.chat_id  # Використання правильного атрибута для ідентифікації каналу
        channels = await get_channels()
        channels_list = [channel[0] for channel in channels]
        if channel_id not in channels_list:
            return

        destination_channel = await get_destination_channel()
        if not destination_channel:
            logger.error("Канал-приймач не встановлено.")
            return

        # Відфільтровуємо опитування
        if event.message.poll:
            logger.info("Повідомлення є опитуванням. Пропускаємо.")
            return

        # Перевірка останнього пересланого повідомлення
        last_id = await get_last_message_id(channel_id)
        if event.message.id <= last_id:
            logger.info(f"Повідомлення {event.message.id} вже переслано.")
            return

        async with semaphore:
            success = await safe_forward(event.message, destination_channel)
            if success:
                logger.info(f"Повідомлення {event.message.id} переслано до каналу {destination_channel}")
                await update_last_message_id(channel_id, event.message.id)
    except Exception as e:
        logger.error(f"Помилка в обробці повідомлення: {str(e)}", exc_info=True)

# Функція для оновлення бази даних
async def update_database():
    await init_db()
    logger.info("База даних оновлена.")
    return "База даних успішно оновлена."

# Функція для створення меню клавіатури
def create_menu_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("Додати канал"),
        types.KeyboardButton("Додати кілька каналів"),
        types.KeyboardButton("Видалити канал"),
        types.KeyboardButton("Показати список каналів"),
        types.KeyboardButton("Встановити канал-приймач"),
        types.KeyboardButton("Видалити канал-приймач"),
        types.KeyboardButton("Показати канал-приймач"),
        types.KeyboardButton("Обновити базу даних"),
        types.KeyboardButton("Допомога")
    ]
    keyboard.add(*buttons)
    return keyboard

# Обробник команди /start
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    if message.from_user.id != my_id:
        return

    start_message = "Привіт! Я бот для роботи з каналами в Telegram.\n\nНатисніть кнопку на клавіатурі для вибору дії."
    keyboard = create_menu_keyboard()
    await message.reply(start_message, reply_markup=keyboard)
    logger.info(f"Користувач {message.from_user.id} ініціював бота.")

# Обробник повідомлень з кнопками
@dp.message_handler()
async def handle_message(message: types.Message):
    if message.from_user.id != my_id:
        return

    if message.text == "Додати канал":
        await ChannelAdding.waiting_for_channel_id.set()
        await message.reply('Введіть ID каналу або його username, який ви хочете додати:')
        logger.info("Очікування вводу ID каналу")

    elif message.text == "Додати кілька каналів":
        await MassChannelAdding.waiting_for_channels.set()
        await message.reply('Введіть ID каналів або їх usernames, розділені комами або новими рядками:')
        logger.info("Очікування вводу списку каналів для масового додавання")

    elif message.text == "Видалити канал":
        channels = await get_channels()
        if channels:
            buttons = [types.InlineKeyboardButton(text=name, callback_data=f'delete_channel_{id}') for id, name in channels]
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(*buttons)
            await message.reply("Виберіть канал, який хочете видалити:", reply_markup=keyboard)
            logger.info("Надіслано меню для видалення каналів.")
        else:
            await message.reply("Список каналів порожній.")
            logger.info("Спроба видалити канал, але список порожній.")

    elif message.text == "Показати список каналів":
        channels = await get_channels()
        if channels:
            channel_list = '\n'.join(f"{name} ({id})" for id, name in channels)
            await message.reply("Список каналів:\n" + channel_list)
            logger.info("Надіслано список каналів.")
        else:
            await message.reply("Список каналів порожній.")
            logger.info("Спроба показати список каналів, але список порожній.")

    elif message.text == "Встановити канал-приймач":
        await DestinationChannelSetting.waiting_for_destination_channel_id.set()
        await message.reply('Введіть ID каналу-приймача або його username, який ви хочете встановити як основний:')
        logger.info("Очікування вводу ID каналу-приймача")

    elif message.text == "Видалити канал-приймач":
        destination_channel = await get_destination_channel()
        if destination_channel:
            await set_destination_channel(None)
            await message.reply("Канал-приймач видалено.")
            logger.info("Канал-приймач видалено.")
        else:
            await message.reply("Канал-приймач не встановлено.")
            logger.info("Спроба видалити канал-приймач, але він не встановлено.")

    elif message.text == "Показати канал-приймач":
        destination_channel = await get_destination_channel()
        if destination_channel:
            try:
                chat = await client.get_entity(destination_channel)
                await message.reply(f"Поточний канал-приймач: {chat.title} ({destination_channel})")
                logger.info(f"Показано канал-приймач: {chat.title} ({destination_channel})")
            except Exception as e:
                await message.reply(f"Поточний канал-приймач ID: {destination_channel}")
                logger.warning(f"Показано канал-приймач за ID: {destination_channel}, не вдалося отримати назву. Помилка: {e}")
        else:
            await message.reply("Канал-приймач не встановлено.")
            logger.info("Спроба показати канал-приймач, але він не встановлено.")

    elif message.text == "Обновити базу даних":
        update_result = await update_database()
        await message.reply(update_result)
        logger.info("Користувач оновив базу даних.")

    elif message.text == "Допомога":
        await help_message(message)

    else:
        await message.reply("Невідома команда. Натисніть кнопку на клавіатурі для вибору дії.")
        logger.warning(f"Отримано невідому команду: {message.text}")

# Обробник стану масового додавання каналів
@dp.message_handler(state=MassChannelAdding.waiting_for_channels)
async def mass_add_channels_handler(message: types.Message, state: FSMContext):
    try:
        channels_input = message.text.strip()
        # Розділення за комами або новими рядками
        channels = [ch.strip() for ch in channels_input.replace(',', '\n').split('\n') if ch.strip()]
        if not channels:
            await message.reply("Список каналів порожній. Спробуйте ще раз.")
            logger.warning("Спроба масового додавання каналів з порожнім списком.")
            return

        added_channels = []
        failed_channels = []

        for channel in channels:
            try:
                if channel.startswith("@"):
                    username = channel[1:]
                    chat = await client.get_entity(username)
                    channel_id = chat.id
                elif channel.startswith("-100"):
                    channel_id = int(channel)
                    chat = await client.get_entity(channel_id)
                else:
                    channel_id = int(channel)
                    chat = await client.get_entity(channel_id)

                if chat:
                    # Перевірка прав доступу
                    if not (chat.broadcast or chat.megagroup):
                        failed_channels.append(channel)
                        logger.error(f"Спроба додати не канал: {channel}")
                        continue

                    await save_channel(channel_id, chat.title)
                    added_channels.append(f"{chat.title} ({channel_id})")
                    logger.info(f"Канал {chat.title} (ID: {channel_id}) додано.")

                    # Встановлюємо last_message_id на останнє повідомлення
                    messages = await fetch_channel_history(channel_id, limit=1)
                    if messages:
                        last_id = messages[0].id
                        await update_last_message_id(channel_id, last_id)
                        logger.info(f"Встановлено last_message_id для каналу {channel_id} на {last_id}")
                else:
                    failed_channels.append(channel)
            except Exception as e:
                failed_channels.append(channel)
                logger.error(f"Помилка при додаванні каналу {channel}: {str(e)}", exc_info=True)

        response_message = ""
        if added_channels:
            response_message += "Успішно додано:\n" + "\n".join(added_channels) + "\n"
        if failed_channels:
            response_message += "Не вдалося додати:\n" + "\n".join(failed_channels)

        await message.reply(response_message if response_message else "Немає каналів для додавання.")
        logger.info("Масове додавання каналів завершено.")
    except Exception as e:
        await message.reply(f"Сталася помилка при масовому додаванні каналів: {str(e)}")
        logger.error(f"Помилка при масовому додаванні каналів: {str(e)}", exc_info=True)
    finally:
        await state.finish()

# Обробник кнопок для видалення каналу
@dp.callback_query_handler(lambda c: c.data and c.data.startswith('delete_channel_'))
async def delete_channel_callback(callback_query: types.CallbackQuery):
    channel_id = int(callback_query.data[len('delete_channel_'):])
    try:
        async with aiosqlite.connect('channels.db') as db:
            cursor = await db.execute('SELECT title FROM channels WHERE id = ?', (channel_id,))
            row = await cursor.fetchone()
            if row:
                channel_title = row[0]
                await delete_channel(channel_id)
                await callback_query.message.reply(f"Канал {channel_title} (ID: {channel_id}) видалено.")
                logger.info(f"Канал {channel_title} (ID: {channel_id}) видалено.")
            else:
                await callback_query.message.reply("Канал не знайдено.")
                logger.warning(f"Спроба видалити канал з ID {channel_id}, але він не знайдено.")
    except Exception as e:
        await callback_query.message.reply("Сталася помилка при видаленні каналу.")
        logger.error(f"Помилка при видаленні каналу {channel_id}: {str(e)}", exc_info=True)
    finally:
        await callback_query.answer()

# Обробник стану встановлення каналу-приймача
@dp.message_handler(state=DestinationChannelSetting.waiting_for_destination_channel_id)
async def set_destination_channel_handler(message: types.Message, state: FSMContext):
    try:
        channel_input = message.text.strip()
        channel_id = None
        chat = None

        if channel_input.startswith("@"):
            username = channel_input[1:]
            chat = await client.get_entity(username)
            channel_id = chat.id
        elif channel_input.startswith("-100"):
            channel_id = int(channel_input)
            chat = await client.get_entity(channel_id)
        else:
            channel_id = int(channel_input)
            chat = await client.get_entity(channel_id)

        if chat:
            # Перевірка прав доступу
            if not (chat.broadcast or chat.megagroup):
                await message.reply("Вказаний ID не є каналом.")
                logger.error(f"Спроба встановити не канал як приймач: {channel_input}")
                return

            await set_destination_channel(channel_id)
            await message.reply(f"Канал-приймач {chat.title} (ID: {channel_id}) встановлено.")
            logger.info(f"Канал-приймач {chat.title} (ID: {channel_id}) встановлено.")
        else:
            await message.reply("Канал-приймач не знайдено. Будь ласка, вкажіть коректний ID каналу-приймача або його username (починається з '@').")
            logger.error("Помилка при встановленні каналу-приймача: канал не знайдено.")
    except Exception as e:
        await message.reply(f"Сталася помилка при встановленні каналу-приймача: {str(e)}")
        logger.error(f"Помилка при встановленні каналу-приймача: {str(e)}", exc_info=True)
    finally:
        await state.finish()

# Обробник команди /help
async def help_message(message: types.Message):
    if message.from_user.id != my_id:
        return

    help_message_text = (
        "📋 **Список доступних команд та кнопок:**\n"
        "Натисніть кнопку на клавіатурі для вибору дії.\n\n"
        "🔹 **Додати канал**: Додати один канал для моніторингу\n"
        "🔹 **Додати кілька каналів**: Додати кілька каналів одночасно\n"
        "🔹 **Видалити канал**: Видалити канал зі списку\n"
        "🔹 **Показати список каналів**: Переглянути додані канали\n"
        "🔹 **Встановити канал-приймач**: Встановити основний канал-приймач\n"
        "🔹 **Видалити канал-приймач**: Видалити канал-приймач\n"
        "🔹 **Показати канал-приймач**: Переглянути встановлений канал-приймач\n"
        "🔹 **Обновити базу даних**: Оновити базу даних\n"
        "🔹 **Допомога**: Отримати цю інформацію\n"
    )
    await message.reply(help_message_text, parse_mode='Markdown')
    logger.info(f"Користувач {message.from_user.id} запросив допомогу.")

# Крок 6: Перевірка пропущених повідомлень при запуску
async def check_missed_messages():
    channels = await get_channels()
    destination_channel = await get_destination_channel()
    if not destination_channel:
        logger.error("Канал-приймач не встановлено. Не можна обробити пропущені повідомлення.")
        return

    for channel_id, _ in channels:
        await process_missed_messages(channel_id, destination_channel)
    logger.info("Перевірка пропущених повідомлень завершена.")

# Основна функція
if __name__ == "__main__":
    async def main():
        await init_db()
        try:
            await client.start()
            logger.info("Telethon клієнт запущено та підключено.")

            # Перевірка пропущених повідомлень при запуску
            await check_missed_messages()

            # Запуск клієнта і бота паралельно
            await asyncio.gather(
                client.run_until_disconnected(),
                dp.start_polling()
            )
        except Exception as e:
            logger.error(f"Сталася помилка: {str(e)}", exc_info=True)
        finally:
            await client.disconnect()
            logger.info("Telethon клієнт відключено.")

    asyncio.run(main())
