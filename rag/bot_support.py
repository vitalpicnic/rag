"""Bounded background work and safe Telegram text, independent of credentials."""
import asyncio
from html import escape


async def query_runtime(runtime, request):
    """Use the shared runtime's admission; cancelling this await never frees its slot."""
    future = runtime.submit(request)
    return await asyncio.wait_for(asyncio.wrap_future(future), runtime.wait_timeout)


def split_telegram_html(text, limit=4000):
    """Escape each character before splitting; never cut entities or emoji pairs."""
    if limit < 6:
        raise ValueError('limit must be at least 6')
    chunks, current, units = [], [], 0
    for char in str(text):
        encoded = escape(char, quote=False)
        size = len(encoded.encode('utf-16-le')) // 2
        if units + size > limit:
            chunks.append(''.join(current))
            current, units = [], 0
        current.append(encoded)
        units += size
    if current:
        chunks.append(''.join(current))
    return chunks or ['Нет текста ответа.']


class SerialWorker:
    def __init__(self, queue_size=8):
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.task = None
        self.closed = False

    async def submit(self, function, timeout=60):
        if self.closed:
            raise RuntimeError('Worker is closed')
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((function, future))
        # Cancels only this result, not the thread. The worker stays occupied until
        # the synchronous call really ends, preventing runaway parallel API calls.
        return await asyncio.wait_for(future, timeout)

    async def _run(self):
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                function, future = item
                if future.cancelled():
                    continue
                try:
                    result = await asyncio.to_thread(function)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(result)
            finally:
                self.queue.task_done()

    async def close(self):
        self.closed = True
        if self.task is None:
            return
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item is not None:
                item[1].cancel()
            self.queue.task_done()
        await self.queue.put(None)
        await self.task
