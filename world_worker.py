"""Aegis AI — Open World mode (early access).

Same infrastructure as the chat worker (GitHub Actions session, Firestore
backends, Groq model, heartbeat dashboard) but a different environment and
system prompt: Aegis gets its own persistent 3D world. Each tick the model
receives the current world state and returns a "program" (a list of world
commands) plus an inner thought, which the engine executes and stores. The
world state document is publicly readable, so a viewer page can render the
world and the thought log live.

No code from the model is ever executed. The model only picks from a fixed
whitelist of world commands with validated values.
"""

import asyncio
import json
import logging
import os
import re
import signal
import time
from datetime import datetime, timezone

import httpx

from firestore_worker import (Worker, Status, MESSAGE_SERVERS, base, pack, fields,
                             QuotaExhausted, RateLimited, log)

WORLD_PATH = 'system/aegisWorld'
# Default 150s: sized so a compacted tick (~2k tokens) fits Groq's free-tier
# 8K tokens/min and 200K tokens/day without tripping 429s. Tune with the env var.
WORLD_TICK_SECONDS = max(20, int(os.getenv('WORLD_TICK_SECONDS', '150')))
MAX_OBJECTS = 80
MAX_ACTIONS = 8
MAX_THOUGHTS = 24
MAX_SPEECH = 12
SHAPES = ('cube', 'sphere', 'cone', 'cylinder', 'torus', 'pyramid', 'ring', 'star')
FORMS = SHAPES
EFFECTS = ('stars', 'rain', 'snow', 'aurora', 'fireflies')
MAX_EFFECTS = 3
COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
URL_RE = re.compile(r'https?://\S+', re.I)

# default look of the world — Aegis can change all of it
DEFAULT_ENV = {
    'skyTop': '#0d0221', 'skyBottom': '#2b1258',
    'fogColor': '#1a0a3a', 'fogDensity': 0.35,
    'lightColor': '#cdbaff', 'lightIntensity': 1.0,
    'groundColor': '#11042a',
    'effects': [],
}


def ensure_env(state):
    """Fill in environment defaults (old world docs have no env yet)."""
    env = state.setdefault('env', {})
    if not isinstance(env, dict):
        env = {}
        state['env'] = env
    for key, value in DEFAULT_ENV.items():
        env.setdefault(key, value if not isinstance(value, list) else [])
    if not isinstance(env['effects'], list):
        env['effects'] = []
    env['effects'] = [e for e in env['effects'] if e in EFFECTS][:MAX_EFFECTS]
    return env


def hex_color(value, fallback):
    """Return value if it is a valid #rrggbb color, else the fallback."""
    value = str(value or '')
    return value if COLOR_RE.match(value) else fallback


def clamp(value, low, high):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return None


def clean_text(value, limit):
    """Strip URLs and secrets from model-authored text and cap its length."""
    text = URL_RE.sub('', str(value or '')).strip()
    for key in ('SITE_USERNAME', 'SITE_PASSWORD', 'GROQ_API_KEY'):
        secret = os.getenv(key, '')
        if len(secret) >= 20 and secret.lower() in text.lower():
            text = text.replace(secret, '████')
    return text[:limit]


def parse_plan(text):
    """Parse the model's mandatory single-JSON-object response."""
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError('Model output rejected: not one JSON object')
    if not isinstance(plan, dict):
        raise RuntimeError('Model output rejected: not one JSON object')
    thought = clean_text(plan.get('thought', ''), 140)
    actions = plan.get('actions', [])
    if not isinstance(actions, list):
        raise RuntimeError('Model output rejected: actions must be a list')
    if len(actions) > MAX_ACTIONS:
        actions = actions[:MAX_ACTIONS]
    if not thought:
        raise RuntimeError('Model output rejected: thought is required')
    return thought, actions


def apply_action(state, action):
    """Validate and run one whitelisted world command against the state."""
    if not isinstance(action, dict):
        return False
    do = action.get('do')
    now = datetime.now(timezone.utc).isoformat()
    if do == 'think':
        text = clean_text(action.get('text', ''), 140)
        if not text:
            return False
        state['thoughts'].append({'t': now, 'text': text})
        state['thoughts'] = state['thoughts'][-MAX_THOUGHTS:]
        return True
    if do == 'say':
        text = clean_text(action.get('text', ''), 100)
        if not text:
            return False
        state['speech'].append({'t': now, 'text': text})
        state['speech'] = state['speech'][-MAX_SPEECH:]
        return True
    if do == 'body':
        form = str(action.get('form', '')).lower()
        if form not in FORMS:
            return False
        color = str(action.get('color', '#9b6dff'))
        if not COLOR_RE.match(color):
            color = '#9b6dff'
        size = clamp(action.get('size', 1), 0.2, 4.0)
        if size is None:
            size = 1.0
        state['body'] = {'form': form, 'color': color, 'size': round(size, 2),
                        'x': state['body']['x'] if state['body'] else 0.0,
                        'y': state['body']['y'] if state['body'] else 2.0,
                        'z': state['body']['z'] if state['body'] else 0.0}
        return True
    if do == 'move':
        if not state['body']:
            return False
        for axis, low, high in (('x', -40, 40), ('y', 0, 30), ('z', -40, 40)):
            value = clamp(action.get(axis), low, high)
            if value is not None:
                state['body'][axis] = round(value, 2)
        return True
    if do == 'spawn':
        shape = str(action.get('shape', '')).lower()
        if shape not in SHAPES:
            return False
        x, y, z = (clamp(action.get(axis), -40, 40) for axis in ('x', 'y', 'z'))
        if x is None or y is None or z is None:
            return False
        color = str(action.get('color', '#7d57ef'))
        if not COLOR_RE.match(color):
            color = '#7d57ef'
        size = clamp(action.get('size', 1), 0.2, 6.0)
        if size is None:
            size = 1.0
        name = clean_text(action.get('name', ''), 24) or 'object%d' % (state['tick'] + 1)
        if any(e['name'] == name for e in state['entities']):
            return False  # duplicate name: the model must pick another
        state['entities'].append({'shape': shape, 'x': round(x, 2), 'y': round(y, 2), 'z': round(z, 2),
                                  'color': color, 'size': round(size, 2), 'name': name})
        if len(state['entities']) > MAX_OBJECTS:
            state['entities'] = state['entities'][-MAX_OBJECTS:]
        return True
    if do == 'delete':
        name = clean_text(action.get('name', ''), 24)
        before = len(state['entities'])
        state['entities'] = [e for e in state['entities'] if e['name'] != name]
        return len(state['entities']) < before
    if do == 'recolor':
        name = clean_text(action.get('name', ''), 24)
        color = str(action.get('color', ''))
        if not COLOR_RE.match(color):
            return False
        for e in state['entities']:
            if e['name'] == name:
                e['color'] = color
                return True
        return False
    if do == 'move_object':
        name = clean_text(action.get('name', ''), 24)
        for e in state['entities']:
            if e['name'] == name:
                for axis, low, high in (('x', -40, 40), ('y', 0, 30), ('z', -40, 40)):
                    value = clamp(action.get(axis), low, high)
                    if value is not None:
                        e[axis] = round(value, 2)
                return True
        return False
    if do == 'rotate':
        name = clean_text(action.get('name', ''), 24)
        speed = clamp(action.get('speed', 1), 0, 2)
        if speed is None:
            speed = 1.0
        for e in state['entities']:
            if e['name'] == name:
                e['spin'] = round(speed, 2)
                return True
        return False
    if do == 'orbit':
        name = clean_text(action.get('name', ''), 24)
        center = str(action.get('center', '0,0,0')).split(',')
        if len(center) != 3:
            return False
        try:
            cx, cy, cz = (float(v) for v in center)
        except ValueError:
            return False
        radius = clamp(action.get('radius', 5), 0, 40)
        speed = clamp(action.get('speed', 1), 0, 2)
        if radius is None or speed is None:
            return False
        for e in state['entities']:
            if e['name'] == name:
                e['orbit'] = {'cx': round(cx, 2), 'cy': round(cy, 2), 'cz': round(cz, 2),
                              'radius': round(radius, 2), 'speed': round(speed, 2)}
                return True
        return False
    if do == 'reset':
        state['entities'] = []
        state['body'] = None
        state['env']['effects'] = []
        return True
    if do == 'sky':
        env = state['env']
        if 'top' in action:
            env['skyTop'] = hex_color(action.get('top'), env['skyTop'])
        if 'bottom' in action:
            env['skyBottom'] = hex_color(action.get('bottom'), env['skyBottom'])
        return True
    if do == 'fog':
        env = state['env']
        if 'color' in action:
            env['fogColor'] = hex_color(action.get('color'), env['fogColor'])
        density = clamp(action.get('density'), 0, 1)
        if density is not None:
            env['fogDensity'] = round(density, 2)
        return True
    if do == 'light':
        env = state['env']
        if 'color' in action:
            env['lightColor'] = hex_color(action.get('color'), env['lightColor'])
        intensity = clamp(action.get('intensity'), 0.2, 2.0)
        if intensity is not None:
            env['lightIntensity'] = round(intensity, 2)
        return True
    if do == 'ground':
        state['env']['groundColor'] = hex_color(action.get('color'), state['env']['groundColor'])
        return True
    if do == 'effect':
        env = state['env']
        name = str(action.get('name', '')).lower().strip()
        if name not in EFFECTS:
            return False
        on = action.get('on', True)
        if on:
            if name not in env['effects']:
                env['effects'] = (env['effects'] + [name])[-MAX_EFFECTS:]
        else:
            env['effects'] = [e for e in env['effects'] if e != name]
        return True
    return False


def run_plan(state, thought, actions):
    """Execute a validated plan; returns the number of actions applied."""
    applied = 0
    for action in actions:
        if apply_action(state, action):
            applied += 1
    state['thoughts'].append({'t': datetime.now(timezone.utc).isoformat(), 'text': thought})
    state['thoughts'] = state['thoughts'][-MAX_THOUGHTS:]
    return applied


def fresh_state():
    state = {'tick': 0, 'body': None, 'entities': [], 'thoughts': [], 'speech': [],
             'updatedAt': datetime.now(timezone.utc).isoformat()}
    ensure_env(state)
    return state


class WorldWorker(Worker):
    async def load_world(self):
        for server in MESSAGE_SERVERS:
            try:
                doc = await self.doc(server, WORLD_PATH, optional=True)
            except QuotaExhausted as exc:
                log.warning('World server %s quota-exhausted; trying the next: %s', server, exc)
                continue
            if doc is not None:
                state = fields(doc)
                # tolerate older/partial documents
                state.setdefault('tick', 0)
                state.setdefault('entities', [])
                state.setdefault('thoughts', [])
                state.setdefault('speech', [])
                ensure_env(state)
                if state.get('body') is not None:
                    state['body'].setdefault('x', 0.0)
                    state['body'].setdefault('y', 2.0)
                    state['body'].setdefault('z', 0.0)
                log.info('World loaded from %s (tick %s, %d objects)', server, state['tick'], len(state['entities']))
                return state
        log.info('No world yet; starting an empty world')
        return fresh_state()

    async def save_world(self, state, servers=None):
        state['updatedAt'] = datetime.now(timezone.utc).isoformat()
        payload = {k: pack(v) for k, v in state.items()}
        saved = []
        for server in (servers or MESSAGE_SERVERS):
            try:
                r = await self.request('PATCH', base(server) + '/' + WORLD_PATH,
                                       timeout=20, json={'fields': payload})
                saved.append(server)
            except (QuotaExhausted, RuntimeError, httpx.HTTPError) as exc:
                log.warning('World save to %s failed: %s', server, exc)
        return saved

    async def think(self, state):
        """One model turn: world in, plan out. One corrective retry on bad output.

        The state is sent compact (objects as [shape,x,y,z,color,size,name] rows,
        thoughts/speech as bare text) so a tick costs a fraction of Groq's free-tier
        token budget; the constant rules prompt stays cacheable (cached tokens are
        not counted against rate limits)."""
        rules = open('aegis_world_rules.txt', encoding='utf-8').read()
        visible = {
            'tick': state['tick'],
            'body': state['body'],
            'objects': [[e.get('shape'), e.get('x'), e.get('y'), e.get('z'),
                         e.get('color'), e.get('size'), e.get('name')]
                        for e in state['entities']],
            'environment': state['env'],
            'recent_thoughts': [t.get('text', '') for t in state['thoughts'][-6:]],
            'recent_speech': [s.get('text', '') for s in state['speech'][-4:]],
        }
        messages = [
            {'role': 'system', 'content': rules},
            {'role': 'user', 'content': 'Current world state (objects are [shape,x,y,z,color,size,name]):\n'
                        + json.dumps(visible, separators=(',', ':'))},
        ]

        async def complete():
            model = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')
            payload = {'model': model, 'temperature': 0.85, 'max_completion_tokens': 700, 'messages': messages}
            if 'gpt-oss' in model:
                payload['reasoning_effort'] = 'low'   # skip hidden reasoning tokens
            r = await self.request('POST', 'https://api.groq.com/openai/v1/chat/completions',
                                   headers={'Authorization': 'Bearer ' + os.environ['GROQ_API_KEY']},
                                   json=payload)
            m = r.json()['choices'][0]
            if m.get('finish_reason') != 'stop' or m['message'].get('tool_calls'):
                raise RuntimeError('Incomplete or tool-bearing model output rejected')
            return m['message'].get('content') or ''

        content = await complete()
        try:
            return parse_plan(content)
        except RuntimeError:
            messages.append({'role': 'assistant', 'content': content})
            messages.append({'role': 'user', 'content': 'That was not exactly one valid JSON object with a "thought" string and an "actions" list. Reply again with only the JSON object: {"thought":"...","actions":[...]}. Use only the allowed commands.'})
            return parse_plan(await complete())


async def run(client, stop):
    """One continuous world session: sign in, then build forever."""
    status = Status(client)
    await status.report(state='booting', detail='signing in', force=True)
    status.note('World session starting')
    w = WorldWorker(client)
    while not stop.is_set():
        try:
            await w.login()
            break
        except (QuotaExhausted, RuntimeError) as exc:
            log.warning('Login unavailable; retrying in 60s: %s', exc)
            await status.report(state='login_retry', detail='sign-in unavailable; retrying', force=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
    if stop.is_set():
        await status.report(state='stopped', detail='stopped before login completed', force=True)
        return

    state = await w.load_world()
    await status.report(state='watching', detail='world loaded; Aegis is dreaming', force=True,
                       messagesSeen=len(state['entities']))
    status.note('World loaded: tick %s, %d objects' % (state['tick'], len(state['entities'])))

    while not stop.is_set():
        try:
            await status.report(state='thinking', detail='tick %d: Aegis is thinking' % state['tick'], force=True)
            thought, actions = await w.think(state)
            applied = run_plan(state, thought, actions)
            state['tick'] = int(state['tick']) + 1
            saved = await w.save_world(state)
            await status.report(state='building', detail='tick %d: ran %d action(s); %d objects; saved to %d server(s)'
                                % (state['tick'], applied, len(state['entities']), len(saved)),
                                force=True, messagesSeen=len(state['entities']), repliesSent=state['tick'])
            status.note('Tick %d: thought=%r actions=%d applied=%d saved=%s'
                        % (state['tick'], thought[:80], len(actions), applied, ','.join(saved or [])))
            log.info('Tick %d: %d/%d actions applied; %d objects; saved to %s',
                     state['tick'], applied, len(actions), len(state['entities']), ','.join(saved or ['none']))
        except QuotaExhausted as exc:
            log.warning('World tick deferred (quota); cooling down: %s', exc)
            await status.report(state='quota_wait', detail='a database is quota-exhausted; cooling down', force=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass
        except RateLimited as exc:
            # The model API's rate window — resume the instant it resets.
            wait = max(15, min(exc.retry_after, 300))
            log.warning('World tick rate-limited; resuming in %.0fs: %s', wait, exc)
            await status.report(state='quota_wait', detail='model limit hit; resuming in %ds' % int(wait), force=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        except Exception as exc:
            log.warning('World tick failed (%s); retrying next tick: %s', type(exc).__name__, exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=WORLD_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=WORLD_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass

    await status.report(state='stopped', detail='world session ended', force=True)
    log.info('World session stopped.')


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        await run(client, stop)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    asyncio.run(main())
