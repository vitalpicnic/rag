import asyncio
from datetime import datetime, timezone
from html import unescape
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime_deps'), str(ROOT / '.test_deps')]
with patch.dict(os.environ, {}, clear=True):
    import bot
from rag.bot_support import SerialWorker


class Chain:
    def __init__(self):
        self.calls = []
    def invoke(self, inputs, config):
        self.calls.append(inputs)
        return {'answer': '<b>42 & 43</b>' + '😀' * 2500, 'sources': []}


def message(text, user=1):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user, first_name='User'),
                           chat=SimpleNamespace(id=10), answer=AsyncMock(),
                           bot=SimpleNamespace(send_chat_action=AsyncMock()))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.worker = SerialWorker()
        for mapping in (bot.user_topics, bot.user_chains, bot.user_modes, bot.session_ids, bot.file_links_cache):
            mapping.clear()
        bot.store.clear()
        self.allowed = bot.ALLOWED_IDS
        bot.ALLOWED_IDS = set()
        self.chain = Chain()
        bot.user_chains[(10, 1)] = self.chain
        bot.reset_session((10, 1))

    async def asyncTearDown(self):
        await bot.worker.close()
        bot.ALLOWED_IDS = self.allowed

    async def test_text_has_one_generation_and_safely_split_response(self):
        msg = message('question')
        await bot.handle_chat(msg)
        self.assertEqual(len(self.chain.calls), 1)
        chunks = [call.args[0] for call in msg.answer.call_args_list]
        self.assertEqual(''.join(unescape(c) for c in chunks), '<b>42 & 43</b>' + '😀' * 2500)
        self.assertTrue(all(len(c.encode('utf-16-le')) // 2 <= 4000 for c in chunks))

    async def test_help_and_mode_selection_make_no_model_call(self):
        await bot.help_message(message('❓ Справка'))
        await bot.choose_mode(message('📝 Обзор'))
        self.assertEqual(self.chain.calls, [])
        msg = message('overview question')
        await bot.handle_chat(msg)
        self.assertEqual(self.chain.calls[0]['mode'], 'overview')

    async def test_unsupported_graph_does_not_call_model(self):
        await bot.choose_mode(message('📊 График'))
        msg = message('graph question')
        await bot.handle_chat(msg)
        self.assertEqual(len(self.chain.calls), 0)
        self.assertIn('проверенн', msg.answer.call_args_list[-1].args[0])

    async def test_graph_works_without_rag_chain(self):
        bot.user_chains.clear()
        await bot.choose_mode(message('📊 График'))
        msg = message(bot.CHOICES[0])
        msg.answer_photo = AsyncMock()
        with patch('bot.render_chart', return_value=(b'png', 'verified caption')):
            await bot.handle_chat(msg)
        msg.answer_photo.assert_awaited_once()
        self.assertEqual(self.chain.calls, [])

    async def test_unauthorized_and_oversized_requests_do_not_invoke_chain(self):
        bot.ALLOWED_IDS = {99}
        await bot.handle_chat(message('question'))
        bot.ALLOWED_IDS = set()
        await bot.handle_chat(message('x' * 8001))
        self.assertEqual(self.chain.calls, [])

    async def test_clear_during_generation_prevents_late_answer(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def slow(inputs, config):
            entered.set()
            release.wait(2)
            return {'answer': 'late', 'sources': []}
        self.chain.invoke = slow
        msg = message('question')
        task = asyncio.create_task(bot.handle_chat(msg))
        while not entered.is_set():
            await asyncio.sleep(.001)
        await bot.clear_history(message('🧹 Очистить чат'))
        release.set()
        await task
        msg.answer.assert_not_awaited()

    async def test_dispatcher_routes_help_without_reaching_rag(self):
        from aiogram import Bot, types
        client = Bot(token='123456:offline-test-token')
        update = types.Update(update_id=1, message=types.Message(
            message_id=1, date=datetime.now(timezone.utc), chat=types.Chat(id=10, type='private'),
            from_user=types.User(id=1, is_bot=False, first_name='Test'), text='/help',
            entities=[types.MessageEntity(type='bot_command', offset=0, length=5)]))
        try:
            with patch.object(client.session, 'make_request', new=AsyncMock(return_value=update.message)) as transport:
                await bot.dp.feed_update(client, update)
            self.assertEqual(self.chain.calls, [])
            self.assertEqual(transport.await_count, 1)
            self.assertIn('Выберите тему', transport.call_args.args[1].text)
        finally:
            await client.session.close()


if __name__ == '__main__':
    unittest.main()
