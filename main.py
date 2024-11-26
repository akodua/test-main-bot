import asyncio
import logging

import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from telethon import TelegramClient, events

from config import api_id, api_hash, bot_token, my_id, proxy_url

# Визначення станів для очікування вводу
class ChannelAdding(StatesGroup):
    waiting_for_channel_id = State()

class DestinationChannelSetting(StatesGroup):
    waiting_for_destination_channel_id = State()

# Налаштування логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ініціалізація бота та клієнта Telethon
bot = Bot(token=bot_token)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

client = TelegramClient('myGrab', api_id, api_hash)

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
        # Видалено створення таблиці keywords
        await db.commit()

# Функції для роботи з базою даних
async def save_channel(channel_id, channel_title):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('INSERT OR IGNORE INTO channels (id, title) VALUES (?, ?)', (channel_id, channel_title))
        await db.commit()

async def delete_channel(channel_id):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('DELETE FROM channels WHERE id = ?', (channel_id,))
        await db.commit()

async def set_destination_channel(channel_id):
    async with aiosqlite.connect('channels.db') as db:
        await db.execute('DELETE FROM destination')  # Видалення попереднього каналу-приймача
        if channel_id:
            await db.execute('INSERT INTO destination (id) VALUES (?)', (channel_id,))
        await db.commit()

async def get_destination_channel():
    async with aiosqlite.connect('channels.db') as db:
        cursor = await db.execute('SELECT id FROM destination LIMIT 1')
        row = await cursor.fetchone()
        return row[0] if row else None

async def get_channels():
    async with aiosqlite.connect('channels.db') as db:
        cursor = await db.execute('SELECT id, title FROM channels')
        return await cursor.fetchall()

# Видалені функції, пов'язані з keywords:
# async def get_keywords()
# async def add_keyword()
# async def remove_all_keywords()

# Функція для оновлення бази даних
async def update_database():
    await init_db()
    # Можна додати додаткові кроки оновлення бази, якщо необхідно
    logger.info("База даних оновлена.")
    return "База даних успішно оновлена."

# Функція для створення меню клавіатури
def create_menu_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    buttons = [
        types.KeyboardButton("Додати канал"),
        types.KeyboardButton("Видалити канал"),
        types.KeyboardButton("Показати список каналів"),
        types.KeyboardButton("Встановити канал-приймач"),
        types.KeyboardButton("Видалити канал-приймач"),
        types.KeyboardButton("Показати канал-приймач"),
        # Видалена кнопка "Налаштувати фільтр слів"
        types.KeyboardButton("Обновити базу даних"),  # Додана нова кнопка
        types.KeyboardButton("Допомога")
    ]
    keyboard.add(*buttons)
    return keyboard

# Обработчик команды /start
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    if message.from_user.id != my_id:
        return

    start_message = "Привіт! Я бот для роботи з каналами в Telegram.\n\nНатисніть кнопку на клавіатурі для вибору дії."
    keyboard = create_menu_keyboard()
    await message.reply(start_message, reply_markup=keyboard)

# Обработчик сообщений с кнопками
@dp.message_handler()
async def handle_message(message: types.Message):
    if message.from_user.id != my_id:
        return

    if message.text == "Додати канал":
        await ChannelAdding.waiting_for_channel_id.set()
        await message.reply('Введіть ID каналу або його username, який ви хочете додати:')
        logger.info("Очікування вводу ID каналу")

    elif message.text == "Видалити канал":
        channels = await get_channels()
        if channels:
            buttons = [types.InlineKeyboardButton(text=name, callback_data=f'delete_channel_{id}') for id, name in channels]
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(*buttons)
            await message.reply("Виберіть канал, який хочете видалити:", reply_markup=keyboard)
        else:
            await message.reply("Список каналів порожній.")

    elif message.text == "Показати список каналів":
        channels = await get_channels()
        if channels:
            channel_list = '\n'.join(f"{name} ({id})" for id, name in channels)
            await message.reply("Список каналів:\n" + channel_list)
        else:
            await message.reply("Список каналів порожній.")

    elif message.text == "Встановити канал-приймач":
        await DestinationChannelSetting.waiting_for_destination_channel_id.set()
        await message.reply('Введіть ID каналу-приймача або його username, який ви хочете встановити як основний:')

    elif message.text == "Видалити канал-приймач":
        destination_channel = await get_destination_channel()
        if destination_channel:
            await set_destination_channel(None)
            await message.reply("Канал-приймач видалено.")
            logger.info("Канал-приймач видалено.")
        else:
            await message.reply("Канал-приймач не встановлено.")

    elif message.text == "Показати канал-приймач":
        destination_channel = await get_destination_channel()
        if destination_channel:
            try:
                chat = await client.get_entity(destination_channel)
                await message.reply(f"Поточний канал-приймач: {chat.title} ({destination_channel})")
            except:
                await message.reply(f"Поточний канал-приймач ID: {destination_channel}")
        else:
            await message.reply("Канал-приймач не встановлено.")

    elif message.text == "Обновити базу даних":
        update_result = await update_database()
        await message.reply(update_result)
        logger.info("Користувач оновив базу даних.")

    elif message.text == "Допомога":
        await help_message(message)

    else:
        await message.reply("Невідома команда. Натисніть кнопку на клавіатурі для вибору дії.")

# Обработчик состояния добавления канала
@dp.message_handler(state=ChannelAdding.waiting_for_channel_id)
async def add_channel_handler(message: types.Message, state: FSMContext):
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
            await save_channel(channel_id, chat.title)
            await message.reply(f"Канал {chat.title} (ID: {channel_id}) додано.")
            logger.info(f"Канал {chat.title} (ID: {channel_id}) додано.")
        else:
            await message.reply("Канал не знайдено. Будь ласка, вкажіть коректний ID каналу або його username (починається з '@').")
            logger.error("Помилка при додаванні каналу.")
    except Exception as e:
        await message.reply(f"Сталася помилка при додаванні каналу: {str(e)}")
        logger.error(f"Помилка при додаванні каналу: {str(e)}")
    finally:
        await state.finish()

# Обработчик кнопок для удаления канала
@dp.callback_query_handler(lambda c: c.data and c.data.startswith('delete_channel_'))
async def delete_channel_callback(callback_query: types.CallbackQuery):
    channel_id = int(callback_query.data[len('delete_channel_'):])
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
    await callback_query.answer()

# Обработчик состояния установки канала-получателя
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
            await set_destination_channel(channel_id)
            await message.reply(f"Канал-приймач {chat.title} (ID: {channel_id}) встановлено.")
            logger.info(f"Канал-приймач {chat.title} (ID: {channel_id}) встановлено.")
        else:
            await message.reply("Канал-приймач не знайдено. Будь ласка, вкажіть коректний ID каналу-приймача або його username (починається з '@').")
            logger.error("Помилка при встановленні каналу-приймача.")
    except Exception as e:
        await message.reply(f"Сталася помилка при встановленні каналу-приймача: {str(e)}")
        logger.error(f"Помилка при встановленні каналу-приймача: {str(e)}")
    finally:
        await state.finish()

# Обработчик команды /help
async def help_message(message: types.Message):
    if message.from_user.id != my_id:
        return

    help_message_text = (
        "📋 **Список доступних команд та кнопок:**\n"
        "Натисніть кнопку на клавіатурі для вибору дії.\n\n"
        "🔹 **Додати канал**: Додати канал для моніторингу\n"
        "🔹 **Видалити канал**: Видалити канал зі списку\n"
        "🔹 **Показати список каналів**: Переглянути додані канали\n"
        "🔹 **Встановити канал-приймач**: Встановити основний канал-приймач\n"
        "🔹 **Видалити канал-приймач**: Видалити канал-приймач\n"
        "🔹 **Показати канал-приймач**: Переглянути встановлений канал-приймач\n"
        "🔹 **Обновити базу даних**: Оновити базу даних\n"
        "🔹 **Допомога**: Отримати цю інформацію\n"
    )
    await message.reply(help_message_text, parse_mode='Markdown')

# Обработчик новых сообщений из каналов с catch_up=True
@client.on(events.NewMessage(catch_up=True))
async def my_event_handler(event):
    channels_list = [channel[0] for channel in await get_channels()]
    if event.chat_id not in channels_list:
        return

    destination_channel = await get_destination_channel()
    if not destination_channel:
        logger.error("Канал-приймач не встановлено.")
        return

    # Відфільтровуємо опитування
    if event.message.poll:
        logger.info("Повідомлення є опитуванням. Пропускаємо.")
        return

    # Отримуємо текст повідомлення
    message_text = event.message.message or ""

    # **Видалено фільтрацію за забороненими словами**

    # Пересилаємо повідомлення
    try:
        await event.message.forward_to(destination_channel)
        logger.info(f"Повідомлення переслано до каналу {destination_channel}")
    except Exception as e:
        logger.error(f"Помилка при пересиланні повідомлення: {str(e)}")

# Основна функція
if __name__ == "__main__":
    async def main():
        await init_db()
        try:
            await client.start()
            await client.connect()

            dp.register_message_handler(start, commands=['start'], commands_prefix='/')
            dp.register_message_handler(help_message, commands=['help'], commands_prefix='/')

            # Запускаємо клієнт і бота паралельно
            await asyncio.gather(
                client.run_until_disconnected(),
                dp.start_polling()
            )
        except Exception as e:
            logger.error(f"Сталася помилка: {str(e)}")
        finally:
            await client.disconnect()

    asyncio.run(main())
