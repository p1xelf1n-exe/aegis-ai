import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

log = logging.getLogger('aegis')


def error_detail(response, secrets=()):
    status, message = 'UNKNOWN', 'No structured error available.'
    try:
        payload = response.json()
        rows = payload if isinstance(payload, list) else [payload]
        error = next((r['error'] for r in rows if isinstance(r, dict) and isinstance(r.get('error'), dict)), {})
        candidate = error.get('status', '')
        if isinstance(candidate, str) and re.fullmatch(r'[A-Z_]{1,64}', candidate):
            status = candidate
        if isinstance(error.get('message'), str):
            message = error['message'][:8000]
    except (ValueError, TypeError):
        pass
    values = list(secrets) + [os.getenv(k, '') for k in ('SITE_USERNAME', 'SITE_PASSWORD', 'GROQ_API_KEY', 'FIREBASE_ID_TOKEN', 'FIREBASE_REFRESH_TOKEN')]
    for value in sorted((v for v in values if v), key=len, reverse=True):
        message = message.replace(value, '[REDACTED]')
    message = re.sub(r'https?://\S+', '[URL OMITTED]', message)
    message = re.sub(r'Bearer\s+\S+', 'Bearer [REDACTED]', message, flags=re.I)
    message = re.sub(r'eyJ[\w-]+\.[\w-]+\.[\w-]+', '[TOKEN REDACTED]', message)
    message = re.sub(r'[\w.+-]+@[\w.-]+', '[EMAIL REDACTED]', message)
    message = re.sub(r'(?:projects/|/documents/)\S+', '[RESOURCE OMITTED]', message)
    return 'HTTP %s; status=%s; message=%s' % (response.status_code, status, ' '.join(message.split())[:1000])


def retry_after(response):
    raw = response.headers.get('Retry-After', '')
    try:
        value = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            value = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, value) if value == value else 0


async def fetch(client, method, url, *, secrets=(), **kwargs):
    parsed = urlsplit(url)
    read = parsed.scheme == 'https' and parsed.hostname == 'firestore.googleapis.com' and (
        method.upper() == 'GET' or (method.upper() == 'POST' and parsed.path.endswith(':runQuery')))
    if not read:
        return await client.request(method, url, **kwargs)
    # At most five attempts, with a 120-second total read budget. Never retry writes.
    # 429s are daily-quota exhaustions that will not recover inside the retry
    # window, so they return immediately and let the caller fail over to a
    # backup server instead of burning the whole budget per read.
    deadline = time.monotonic() + 120
    for attempt in range(5):
        remaining = deadline - time.monotonic()
        response = await client.request(method, url, timeout=min(45, max(0.1, remaining)), **kwargs)
        if response.status_code not in (502, 503, 504) or attempt == 4:
            return response
        delay = max(5 * 2 ** attempt + random.uniform(0, 1), retry_after(response))
        if delay + 1 >= deadline - time.monotonic():
            log.warning('Firestore read retry budget exhausted; %s', error_detail(response, secrets))
            return response
        log.warning('Firestore read retry %s/4 in %.1fs; %s', attempt + 1, delay, error_detail(response, secrets))
        await asyncio.sleep(delay)
    raise RuntimeError('Read retry loop exhausted')
