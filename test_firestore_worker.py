import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import AsyncMock
import httpx
import firestore_worker
from firestore_worker import Worker, outcome, pack, unpack, QuotaExhausted, IdentityUnavailable, MESSAGE_SERVERS, MAIN, Status, SLOW_FLOOR

QUOTA_BODY = {'error': {'code': 429, 'message': 'Quota exceeded.', 'status': 'RESOURCE_EXHAUSTED'}}


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

    def test_server_allowlist(self):
        async def check():
            async with httpx.AsyncClient() as c:
                with self.assertRaises(RuntimeError):
                    await Worker(c).recent('database-9-nope')
        asyncio.run(check())

    def test_quota_error_detected(self):
        async def check():
            transport = httpx.MockTransport(lambda r: httpx.Response(429, json=QUOTA_BODY))
            async with httpx.AsyncClient(transport=transport) as c:
                w = Worker(c)
                with self.assertRaises(QuotaExhausted):
                    await w.request('GET', 'https://firestore.googleapis.com/v1/projects/database-2-a5b32/databases/(default)/documents/system/sigmaState')
        asyncio.run(check())

    def test_login_falls_back_to_account_mirror(self):
        def route(request):
            url = str(request.url)
            if 'signInWithPassword' in url:
                return httpx.Response(200, json={'localId': 'uid1', 'idToken': 'tok1'})
            if request.method == 'GET' and MAIN in url and '/users/uid1' in url:
                return httpx.Response(429, json=QUOTA_BODY)
            if request.method == 'GET' and 'accountData/uid1' in url:
                if 'database-1-ec196' in url:
                    return httpx.Response(429, json=QUOTA_BODY)  # first mirror server quota-exhausted too
                if 'database-3-9462a' in url:
                    return httpx.Response(200, json={'name': url, 'fields': {'role': {'stringValue': 'aegis'}, 'tier': {'stringValue': 'free'}}})
                return httpx.Response(404, json={})
            return httpx.Response(200, json={})
        async def check():
            os.environ.setdefault('SITE_USERNAME', 'aegis@example.com')
            os.environ.setdefault('SITE_PASSWORD', 'pw')
            transport = httpx.MockTransport(route)
            async with httpx.AsyncClient(transport=transport) as c:
                w = Worker(c)
                await w.login()
                self.assertEqual(w.uid, 'uid1')
        asyncio.run(check())

    def test_login_bootstraps_mirror_when_main_quota_exhausted(self):
        commits = []

        def route(request):
            url = str(request.url)
            if 'signInWithPassword' in url:
                return httpx.Response(200, json={'localId': 'uid1', 'idToken': 'tok1'})
            if request.method == 'GET':
                if 'accountData/' in url:
                    return httpx.Response(404, json={})
                return httpx.Response(429, json=QUOTA_BODY)
            if request.method == 'POST' and ':commit' in url:
                commits.append((url, json.loads(request.content.decode())))
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})
        async def check():
            os.environ.setdefault('SITE_USERNAME', 'aegis@example.com')
            os.environ.setdefault('SITE_PASSWORD', 'pw')
            async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as c:
                w = Worker(c)
                await w.login()  # main database quota-exhausted, no mirror: degraded bootstrap, no exception
                self.assertEqual(w.uid, 'uid1')
            self.assertTrue(commits)
            doc_name = commits[0][1]['writes'][0]['update']['name']
            self.assertTrue(doc_name.startswith('projects/'))
            self.assertIn('accountData/uid1', doc_name)
            payload = commits[0][1]['writes'][0]['update']['fields']
            self.assertEqual(payload['role']['stringValue'], 'aegis')
        asyncio.run(check())

    def test_checks_degraded_on_quota(self):
        def route(request):
            url = str(request.url)
            if request.method == 'GET' and MAIN in url and '/users/uid1' in url:
                return httpx.Response(200, json={'name': url, 'fields': {'role': {'stringValue': 'aegis'}, 'tier': {'stringValue': 'free'}}})
            if request.method == 'GET' and ('system/chatControls' in url or 'system/globalPopup' in url or 'sigmaState' in url):
                return httpx.Response(429, json=QUOTA_BODY)
            return httpx.Response(200, json={})
        async def check():
            transport = httpx.MockTransport(route)
            async with httpx.AsyncClient(transport=transport) as c:
                w = Worker(c)
                w.uid = 'uid1'
                main_doc, p, slow = await w.checks()
                self.assertEqual(p.get('role'), 'aegis')
                self.assertEqual(slow, SLOW_FLOOR)
        asyncio.run(check())

    def test_blocked_check_prevents_send(self):
        async def check():
            async with httpx.AsyncClient() as c:
                w = Worker(c)
                w.checks = AsyncMock(side_effect=RuntimeError('locked'))
                w.request = AsyncMock()
                with self.assertRaises(RuntimeError):
                    await w.send(MESSAGE_SERVERS[0], 'global', 'source', {}, 'hello')
                w.request.assert_not_called()
        asyncio.run(check())

    def test_existing_reply_prevents_write(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
                w = Worker(c)
                w.uid = 'test'
                w.checks = AsyncMock(return_value=({}, {'role': 'aegis'}, 60))
                w.request = AsyncMock()
                await w.send(MESSAGE_SERVERS[2], 'aegis-ai', 'source', {}, 'hello')
                w.request.assert_not_called()
        asyncio.run(check())

    def test_status_writes_heartbeat(self):
        writes = []

        def route(request):
            if request.method == 'PATCH':
                writes.append((str(request.url), json.loads(request.content.decode())))
                return httpx.Response(200, json={})
            return httpx.Response(403, json={})
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as c:
                s = Status(c)
                await s.report(state='watching', detail='test', force=True, repliesSent=2)
                self.assertTrue(writes)
                self.assertIn(MESSAGE_SERVERS[0], writes[0][0])
                self.assertEqual(writes[0][1]['fields']['state']['stringValue'], 'watching')
                self.assertEqual(writes[0][1]['fields']['repliesSent']['integerValue'], '2')
        asyncio.run(check())

    def test_send_targets_found_server(self):
        commits = []

        def route(request):
            if request.method == 'POST' and ':commit' in str(request.url):
                commits.append(str(request.url))
                return httpx.Response(200, json={})
            if request.method == 'GET':
                return httpx.Response(404, json={})
            return httpx.Response(200, json={})
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as c:
                w = Worker(c)
                w.uid = 'test'
                w.checks = AsyncMock(return_value=({}, {'role': 'aegis', 'tier': 'free'}, 60))
                await w.send('database-1-ec196', 'aegis-ai', 'source', {}, 'hello')
            self.assertTrue(commits)
            self.assertIn('database-1-ec196', commits[0])
        asyncio.run(check())


    def test_main_reply_posts_to_the_right_channel(self):
        commits = []
        stop = asyncio.Event()
        queries = {'n': 0}

        def route(request):
            url = str(request.url)
            if request.method == 'POST' and ':runQuery' in url:
                queries['n'] += 1
                # 5 queries build the silent baseline; the 6th reveals the mention.
                # The mention is 61s old (past the 60s slowmode) but newer than
                # the baseline cutoff, so it is processed immediately.
                now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                baseline = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                stamp = (datetime.now(timezone.utc) - timedelta(seconds=61)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                if queries['n'] > 5 and 'database-1-ec196' in url:
                    return httpx.Response(200, json=[{'readTime': now, 'document': {
                        'name': 'projects/database-1-ec196/databases/(default)/documents/messages/mention1',
                        'fields': {'content': {'stringValue': '@aegis hello there'}, 'channelId': {'stringValue': 'global'},
                                   'username': {'stringValue': 'member'}, 'timestamp': {'timestampValue': stamp}}}}])
                return httpx.Response(200, json=[{'readTime': baseline}])
            if request.method == 'POST' and ':commit' in url:
                commits.append(request.read().decode())
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        class StubWorker(Worker):
            def __init__(self, client):
                super().__init__(client)
                self.login = AsyncMock(return_value=None)
                self.uid = 'test'
                self.checks = AsyncMock(return_value=(None, {'role': 'aegis', 'tier': 'free'}, 0))
                self.generate = AsyncMock(return_value='hello there')

        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as c:
                with mock.patch('firestore_worker.POLL_SECONDS', 0.01), \
                        mock.patch('firestore_worker.QUOTA_COOLDOWN_SECONDS', 0.01), \
                        mock.patch('firestore_worker.Worker', StubWorker):
                    task = asyncio.create_task(firestore_worker.run(c, stop, True))
                    for _ in range(500):
                        if commits:
                            break
                        await asyncio.sleep(0.01)
                    stop.set()
                    await asyncio.wait_for(task, 5)
            fields = json.loads(commits[0])['writes'][0]['update']['fields']
            # the reply must land in the mention's channel, not name the message path
            self.assertEqual(fields['channelId']['stringValue'], 'global')
            self.assertEqual(fields['content']['stringValue'], 'hello there')

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
