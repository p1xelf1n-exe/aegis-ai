import asyncio
import os
import socket
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright
import bot


async def run():
    for name in ('GROQ_API_KEY', 'GROQ_MODEL', 'SITE_USERNAME', 'SITE_PASSWORD'):
        bot.required_env(name)
    bot.PASS_SECONDS = int(os.getenv('PASS_SECONDS', '1200'))
    bot.BOOTSTRAP_TASK = bot.BOOTSTRAP_TASK.replace(
        f'Open {bot.SITE_URL}.',
        'The approved site is already open in the existing browser tab. '
        'Inspect that tab. Do not open a new tab or reconstruct the site URL.'
    )
    original_instructions = bot.make_instructions

    def instructions(rules, send_enabled):
        return original_instructions(rules, send_enabled) + (
            '\nTOOL CONTRACT: Call only tools explicitly declared in this '
            'request, using their exact names and argument schemas. '
            'There is no separately enabled server-side python or '
            'code_interpreter tool. If computation is needed, use only '
            'a declared browser-agent tool; never invent a tool name. '
            'Keep tool arguments and reasoning concise. If the required '
            'tool is unavailable, stop and report blocked. '
            'NAVIGATION: Work in the pre-opened site tab. Never pass a '
            'URL containing a secret placeholder to navigation tools. '
            'Use on-page controls for navigation within the approved site.'
        )

    bot.make_instructions = instructions
    bot.install_groq_error_logging()
    original_send = httpx.AsyncClient.send
    lock = asyncio.Lock()
    next_request = 0.0
    interval = max(65.0, float(os.getenv('GROQ_REQUEST_INTERVAL', '65')))

    async def paced_send(self, request, *args, **kwargs):
        nonlocal next_request
        if request.url.host != 'api.groq.com':
            return await original_send(self, request, *args, **kwargs)
        async with lock:
            delay = max(0.0, next_request - time.monotonic())
            if delay:
                bot.log.info('Pacing Groq request: waiting %.1fs.', delay)
                await asyncio.sleep(delay)
            next_request = time.monotonic() + interval
            response = await original_send(self, request, *args, **kwargs)
            if response.status_code == 429:
                try:
                    retry = float(response.headers.get('retry-after', '65'))
                except ValueError:
                    retry = 65.0
                next_request = max(next_request, time.monotonic() + max(65.0, retry))
            return response

    paced_send._aegis_diagnostics = True
    httpx.AsyncClient.send = paced_send
    bot.log.info('Groq pacing enabled: at least %.0fs between requests.', interval)
    original_browser = bot.Browser
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix='aegis-browser-') as profile:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                profile,
                headless=True,
                args=[f'--remote-debugging-port={port}',
                      '--remote-debugging-address=127.0.0.1'],
            )
            try:
                async def restrict_route(route):
                    parsed = urlsplit(route.request.url)
                    if parsed.scheme in ('http', 'https') and parsed.hostname != bot.SITE_HOST:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route('**/*', restrict_route)
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(bot.SITE_URL, wait_until='domcontentloaded', timeout=60000)
                    if urlsplit(page.url).hostname != bot.SITE_HOST:
                        raise RuntimeError('Unexpected destination')
                except Exception:
                    raise RuntimeError(
                        'Direct initial navigation failed or left the approved host. '
                        'Details suppressed to avoid logging credential-matching URLs.'
                    ) from None
                bot.log.info('Direct initial navigation reached the approved host.')

                def attached_browser(*args, **kwargs):
                    return original_browser(
                        cdp_url=f'http://127.0.0.1:{port}',
                        keep_alive=kwargs.get('keep_alive', True),
                        allowed_domains=kwargs.get('allowed_domains', [bot.SITE_HOST]),
                    )

                bot.Browser = attached_browser
                await bot.main()
            finally:
                bot.Browser = original_browser
                await context.close()


if __name__ == '__main__':
    asyncio.run(run())
