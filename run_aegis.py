import asyncio
import os
import time

import httpx
import bot


async def run():
    bot.PASS_SECONDS = int(os.getenv('PASS_SECONDS', '1200'))
    original_instructions = bot.make_instructions

    def instructions(rules, send_enabled):
        return original_instructions(rules, send_enabled) + (
            '\nTOOL CONTRACT: Call only tools explicitly declared in this '
            'request, using their exact names and argument schemas. '
            'There is no separately enabled server-side python or '
            'code_interpreter tool. If computation is needed, use only '
            'a declared browser-agent tool; never invent a tool name. '
            'Keep tool arguments and reasoning concise. If the required '
            'tool is unavailable, stop and report blocked.'
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
    await bot.main()


if __name__ == '__main__':
    asyncio.run(run())
