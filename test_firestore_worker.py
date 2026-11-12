import asyncio
import unittest
from unittest.mock import AsyncMock
import httpx
from firestore_worker import Worker, outcome, pack, unpack


class WorkerTests(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(outcome('[FINAL RESPONSE]Hello[END FINAL RESPONSE]'), 'Hello')
        self.assertIsNone(outcome('[NO RESPONSE][END NO RESPONSE]'))
        for value in ('hello', '[FINAL RESPONSE][END FINAL RESPONSE]',
                      '[FINAL RESPONSE]a[END FINAL RESPONSE][FINAL RESPONSE]b[END FINAL RESPONSE]'):
            with self.assertRaises(RuntimeError):
                outcome(value)

    def test_fields(self):
        for value in (None, True, 4, 'text', {'a': 'b'}):
            self.assertEqual(unpack(pack(value)), value)

    def test_channel_allowlist(self):
        async def check():
            async with httpx.AsyncClient() as c:
                with self.assertRaises(RuntimeError):
                    await Worker(c).recent('private-channel')
        asyncio.run(check())

    def test_blocked_check_prevents_send(self):
        async def check():
            async with httpx.AsyncClient() as c:
                w = Worker(c)
                w.checks = AsyncMock(side_effect=RuntimeError('locked'))
                w.request = AsyncMock()
                with self.assertRaises(RuntimeError):
                    await w.send('global', 'source', {}, 'hello')
                w.request.assert_not_called()
        asyncio.run(check())

    def test_existing_reply_prevents_write(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
                w = Worker(c)
                w.uid = 'test'
                w.checks = AsyncMock(return_value=({}, {'role': 'aegis'}, 60))
                w.request = AsyncMock()
                await w.send('aegis-ai', 'source', {}, 'hello')
                w.request.assert_not_called()
        asyncio.run(check())
