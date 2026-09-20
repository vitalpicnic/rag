import asyncio
from html import unescape
import threading
import unittest

from rag.bot_support import SerialWorker, split_telegram_html


class FormattingTests(unittest.TestCase):
    def test_untrusted_html_is_escaped_and_chunks_preserve_text(self):
        text = '<b>not markup</b> & "quotes" ' + '😀' * 3000
        chunks = split_telegram_html(text)
        self.assertEqual(''.join(unescape(c) for c in chunks), text)
        self.assertTrue(all(len(c.encode('utf-16-le')) // 2 <= 4000 for c in chunks))
        self.assertNotIn('<b>', ''.join(chunks))


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_keeps_one_worker_and_skips_expired_waiting_job(self):
        worker = SerialWorker(queue_size=1)
        entered, release = threading.Event(), threading.Event()
        ran_second = []
        def slow():
            entered.set()
            release.wait(2)
            return 'late answer'
        first = asyncio.create_task(worker.submit(slow, timeout=.05))
        while not entered.is_set():
            await asyncio.sleep(.001)
        second = asyncio.create_task(worker.submit(lambda: ran_second.append(True), timeout=.05))
        await asyncio.sleep(.01)
        with self.assertRaises(asyncio.QueueFull):
            await worker.submit(lambda: None, timeout=1)
        with self.assertRaises(TimeoutError):
            await first
        with self.assertRaises(TimeoutError):
            await second
        self.assertEqual(ran_second, [])
        release.set()
        await asyncio.wait_for(worker.queue.join(), 1)
        self.assertEqual(await worker.submit(lambda: 42, timeout=1), 42)
        self.assertEqual(ran_second, [])
        await worker.close()

    async def test_error_does_not_stop_queue(self):
        worker = SerialWorker()
        def fail():
            raise ValueError('failure')
        with self.assertRaises(ValueError):
            await worker.submit(fail, timeout=1)
        self.assertEqual(await worker.submit(lambda: 'next', timeout=1), 'next')
        await worker.close()


if __name__ == '__main__':
    unittest.main()
