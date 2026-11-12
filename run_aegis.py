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
        'The approved site is opened by deployment code. Start with page_info(). '
        'If a previous result showed about:blank, call page_info() again: '
        'deployment code repairs blank tabs before each model request. '
        'Do not construct URLs, call goto_url(), or open new tabs. '
        'Focus the actual login field before using type_secret.'
    )
    original_instructions = bot.make_instructions

    def instructions(rules, send_enabled):
        return original_instructions(rules, send_enabled) + (
            '\nTOOL CONTRACT: Use only declared tools and their exact schemas. '
            'No separate server-side python or code_interpreter tool is enabled. '
            'Keep reasoning and tool arguments concise. '
            'NAVIGATION: Use the existing site tabs and on-page controls. '
            'Never navigate to a URL containing a secret placeholder. '
            'If page state shows about:blank, refresh page_info() before acting. '
            'Never type a secret without first inspecting and focusing its '
            'login field on the approved site. If blocked, stop and report it.'
        )

    bot.make_instructions = instructions
    browser_context = None
    readiness_lock = asyncio.Lock()

    async def repair_blank_tabs():
        if browser_context is None:
            raise RuntimeError('Browser readiness check ran before browser startup.')
        async with readiness_lock:
            try:
                pages = [page for page in browser_context.pages if not page.is_closed()]
                if not pages:
                    raise RuntimeError('No attached pages')
                repaired = 0
                for page in pages:
                    if page.url in ('about:blank', ''):
                        await page.goto(bot.SITE_URL, wait_until='domcontentloaded', timeout=60000)
                        repaired += 1
                    if urlsplit(page.url).hostname != bot.SITE_HOST:
                        raise RuntimeError('Unexpected page host')
                if repaired:
                    bot.log.info('Browser readiness: navigated %s blank tab(s) to the approved host.', repaired)
            except Exception:
                raise RuntimeError(
                    'Browser readiness failed: a tab could not reach the approved host. '
                    'URL details suppressed to protect credentials.'
                ) from None

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
            await repair_blank_tabs()
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
            browser_context = context
            try:
                async def restrict_route(route):
                    parsed = urlsplit(route.request.url)
                    if parsed.scheme in ('http', 'https') and parsed.hostname != bot.SITE_HOST:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route('**/*', restrict_route)
                if not context.pages:
                    await context.new_page()
                await repair_blank_tabs()
                bot.log.info('Direct initial navigation reached the approved host.')

                class ReadyBrowser(original_browser):
                    async def start(self):
                        result = await super().start()
                        await repair_blank_tabs()
                        bot.log.info('Post-attachment tab readiness check passed.')
                        return result

                def attached_browser(*args, **kwargs):
                    return ReadyBrowser(
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
