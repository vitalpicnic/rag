"""Telegram interface. Import and --check require no keys or network calls."""
import argparse
import asyncio
from functools import wraps
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import FSInputFile, BufferedInputFile
from rag.tables import CHOICES, SOURCE, TableNotReady, render_chart, load_rows
from rag.bot_support import SerialWorker, split_telegram_html
from rag.engine import setup_rag_chain, store
from rag.index import IndexNotReady, current_index
from rag.pipeline import MAX_INPUT_CHARS
from rag.models import model_options

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')


def read_ids(name):
    return {int(value.strip()) for value in os.getenv(name, '').split(',') if value.strip()}


ADMIN_IDS = read_ids('TELEGRAM_ADMIN_IDS')
ALLOWED_IDS = read_ids('TELEGRAM_ALLOWED_USER_IDS')
REQUEST_TIMEOUT = 60
logger = logging.getLogger(__name__)
dp = Dispatcher()
worker = SerialWorker(queue_size=8)
user_topics, user_chains, user_modes, session_ids, file_links_cache = {}, {}, {}, {}, {}
topic_lookup = {}
MODES = {'💬 Текст': 'text', '📝 Обзор': 'overview', '📊 График': 'graph'}


def user_key(event):
    target = event.message if hasattr(event, 'data') else event
    return target.chat.id, event.from_user.id


def authorized(handler):
    @wraps(handler)
    async def wrapped(event, *args, **kwargs):
        if ALLOWED_IDS and event.from_user.id not in ALLOWED_IDS:
            if hasattr(event, 'data'):
                await event.answer('Доступ к боту ограничен.', show_alert=True)
            else:
                await event.answer('Доступ к боту ограничен.')
            return
        return await handler(event, *args, **kwargs)
    return wrapped


def reset_session(key):
    previous = session_ids.get(key)
    if previous:
        store.pop(previous, None)
    session_ids[key] = 'tg_' + uuid4().hex
    for item in list(file_links_cache):
        if item[0] == key:
            del file_links_cache[item]
    return session_ids[key]


def get_folders():
    data = ROOT / 'data'
    return sorted(path.name for path in data.iterdir() if path.is_dir()) if data.is_dir() else []


def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text='📂 Выбрать тему'), types.KeyboardButton(text='🧹 Очистить чат'))
    builder.row(*(types.KeyboardButton(text=name) for name in MODES))
    builder.row(types.KeyboardButton(text='❓ Справка'))
    return builder.as_markup(resize_keyboard=True)


@dp.message(Command('start'))
@authorized
async def cmd_start(message):
    await message.answer('Выберите тему и задайте вопрос. Режим по умолчанию — короткий ответ.',
                         reply_markup=get_main_menu())


@dp.message(Command('help'))
@dp.message(F.text == '❓ Справка')
@authorized
async def help_message(message):
    await message.answer('Выберите тему, затем режим «Текст» или «Обзор» и задайте вопрос. '
                         'Кнопки под ответом позволяют скачать источники. '
                         '«Очистить чат» начинает новый диалог. '
                         'В режиме «График» выберите банк кнопкой: доступны активы пяти банков на две даты 2025 года.',
                         reply_markup=get_main_menu())


@dp.message(F.text.in_(set(MODES)))
@authorized
async def choose_mode(message):
    mode = MODES[message.text]
    user_modes[user_key(message)] = mode
    text = f'Выбран режим «{message.text}». Задайте вопрос.'
    if mode == 'graph':
        await message.answer('Выберите проверенные данные: активы банков на 01.01 и 01.12.2025.',
                             reply_markup=graph_menu())
        return
    await message.answer(text, reply_markup=get_main_menu())


def graph_menu():
    builder = ReplyKeyboardBuilder()
    for choice in CHOICES:
        builder.row(types.KeyboardButton(text=choice))
    builder.row(types.KeyboardButton(text='💬 Текст'), types.KeyboardButton(text='📝 Обзор'))
    return builder.as_markup(resize_keyboard=True)


async def handle_graph(message, key):
    if message.text not in CHOICES:
        await message.answer('Для этого запроса нет проверенной таблицы. Выберите доступный график.',
                             reply_markup=graph_menu())
        return
    if user_topics.get(key, 'Банкинг') != 'Банкинг':
        await message.answer('Проверенные графики доступны только для темы «Банкинг».')
        return
    session = session_ids.get(key) or reset_session(key)
    try:
        image, caption = await worker.submit(lambda: render_chart(ROOT, None, message.text),
                                              timeout=REQUEST_TIMEOUT)
        if session_ids.get(key) != session or user_modes.get(key) != 'graph':
            return
        from types import SimpleNamespace
        buttons = source_buttons(key, [SimpleNamespace(metadata={'source': str(ROOT / SOURCE)})])
        await message.answer_photo(BufferedInputFile(image, filename='assets.png'),
                                   caption=caption, reply_markup=buttons)
    except TableNotReady as exc:
        await message.answer(str(exc))
    except asyncio.QueueFull:
        await message.answer('Очередь заполнена. Попробуйте позже.')
    except TimeoutError:
        await message.answer('Время построения графика истекло. Попробуйте позже.')
    except Exception as exc:
        logger.warning('Chart failed: %s', type(exc).__name__)
        await message.answer('Не удалось построить график. Проверьте таблицы и зависимости.')


@dp.message(F.text == '🧹 Очистить чат')
@authorized
async def clear_history(message):
    reset_session(user_key(message))
    await message.answer('История диалога очищена.', reply_markup=get_main_menu())


@dp.message(Command('clearcache'))
@authorized
async def cmd_clearcache(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    for key in list(session_ids):
        reset_session(key)
    user_chains.clear()
    store.clear()
    await message.answer('Кэш и диалоги очищены. Выберите тему заново.', reply_markup=get_main_menu())


@dp.message(F.text == '📂 Выбрать тему')
@authorized
async def show_topics(message):
    builder = InlineKeyboardBuilder()
    for topic in get_folders():
        token = hashlib.sha256(topic.encode('utf-8')).hexdigest()[:16]
        topic_lookup[token] = topic
        builder.button(text=topic, callback_data='set_topic:' + token)
    builder.adjust(1)
    await message.answer('Выберите базу знаний:', reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith('set_topic:'))
@authorized
async def process_topic(callback):
    await callback.answer()
    topic = topic_lookup.get(callback.data.split(':', 1)[1])
    if topic not in get_folders():
        await callback.message.answer('Тема не найдена. Откройте список тем заново.')
        return
    key = user_key(callback)
    session = reset_session(key)
    user_chains.pop(key, None)
    user_topics.pop(key, None)
    status = await callback.message.answer('Загружаю готовую базу…')
    try:
        chain = await worker.submit(lambda: setup_rag_chain(topic), timeout=REQUEST_TIMEOUT)
        if session_ids.get(key) == session:
            user_chains[key], user_topics[key] = chain, topic
            await status.edit_text(f'Готово. Выбрана тема: {topic}')
    except asyncio.QueueFull:
        await status.edit_text('Очередь заполнена. Попробуйте позже.')
    except TimeoutError:
        if session_ids.get(key) == session:
            reset_session(key)
        await status.edit_text('Время ожидания загрузки истекло. Попробуйте позже.')
    except IndexNotReady as exc:
        await status.edit_text(str(exc))
    except Exception as exc:
        logger.warning('Topic load failed: %s', type(exc).__name__)
        await status.edit_text('Не удалось загрузить базу. Проверьте настройки сервиса.')


@dp.callback_query(F.data.startswith('getdoc:'))
@authorized
async def send_source_doc(callback):
    path = file_links_cache.get((user_key(callback), callback.data.split(':', 1)[1]))
    if not path or not Path(path).is_file():
        await callback.answer('Источник не найден. Получите новый ответ.', show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.answer_document(FSInputFile(path))
    except Exception as exc:
        logger.warning('Document send failed: %s', type(exc).__name__)
        await callback.message.answer('Не удалось отправить документ.')


def source_buttons(key, sources):
    for item in list(file_links_cache):
        if item[0] == key:
            del file_links_cache[item]
    builder = InlineKeyboardBuilder()
    paths = dict.fromkeys(doc.metadata.get('source') for doc in sources if doc.metadata.get('source'))
    for source in paths:
        path = Path(source).resolve()
        if not path.is_relative_to(ROOT / 'data') or not path.is_file():
            continue
        token = hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]
        file_links_cache[(key, token)] = str(path)
        builder.button(text='Скачать: ' + path.name[:100], callback_data='getdoc:' + token)
    builder.adjust(1)
    return builder.as_markup() if list(builder.buttons) else None


@dp.message()
@authorized
async def handle_chat(message):
    if not message.text:
        return
    if message.text.startswith('/'):
        await message.answer('Неизвестная команда. Используйте /help.')
        return
    if len(message.text) > MAX_INPUT_CHARS:
        await message.answer('Сократите вопрос до 8000 символов.')
        return
    key = user_key(message)
    if user_modes.get(key) == 'graph':
        await handle_graph(message, key)
        return
    chain = user_chains.get(key)
    if chain is None:
        await message.answer('Сначала выберите тему.', reply_markup=get_main_menu())
        return
    session = session_ids.get(key) or reset_session(key)
    mode = user_modes.get(key, 'text')
    request = {'input': message.text, 'request_id': uuid4().hex, 'mode': mode}

    def invoke():
        if session_ids.get(key) != session:
            return None
        try:
            return chain.invoke(request, config={'configurable': {'session_id': session}})
        finally:
            if session_ids.get(key) != session:
                store.pop(session, None)

    try:
        await message.bot.send_chat_action(message.chat.id, 'typing')
        response = await worker.submit(invoke, timeout=REQUEST_TIMEOUT)
        if response is None or session_ids.get(key) != session:
            return
        chunks = split_telegram_html(response.get('answer') or 'Нет текста ответа.')
        buttons = source_buttons(key, response.get('sources', []))
        for index, chunk in enumerate(chunks):
            if session_ids.get(key) != session:
                return
            await message.answer(chunk, parse_mode='HTML',
                                 reply_markup=(buttons or get_main_menu()) if index == len(chunks) - 1 else None)
    except asyncio.QueueFull:
        await message.answer('Очередь заполнена. Попробуйте позже.')
    except TimeoutError:
        if session_ids.get(key) == session:
            reset_session(key)
        await message.answer('Время ожидания истекло. Поздний ответ не будет показан; попробуйте позже.')
    except IndexNotReady as exc:
        await message.answer(str(exc))
    except Exception as exc:
        logger.warning('Request failed: %s', type(exc).__name__)
        await message.answer('Не удалось обработать запрос. Попробуйте позже.')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check local readiness without API calls')
    args = parser.parse_args()
    if args.check:
        indexes = {}
        for topic in get_folders():
            try:
                current_index(ROOT, topic)
                indexes[topic] = 'ready'
            except IndexNotReady:
                indexes[topic] = 'not_ready'
        try:
            tables = {'status': 'ready', 'observations': len(load_rows(ROOT))}
        except TableNotReady:
            tables = {'status': 'not_ready'}
        print(json.dumps({'offline_import_check': 'ok', 'indexes': indexes, 'tables': tables,
                          'model_options': model_options(),
                          'telegram_configured': bool(os.getenv('TELEGRAM_BOT_TOKEN')),
                          'google_configured': bool(os.getenv('GOOGLE_API_KEY'))}, ensure_ascii=False))
        return
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        raise SystemExit('Для запуска Telegram заполните TELEGRAM_BOT_TOKEN в .env. Для текстового RAG также нужен GOOGLE_API_KEY.')
    client = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])
    try:
        await dp.start_polling(client, close_bot_session=False)
    finally:
        await worker.close()
        await client.session.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
