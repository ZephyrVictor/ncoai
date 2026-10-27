#!/usr/bin/env python3
"""Measure rendered Chat latency through Nginx with real browser sessions.

Requires a running deployment and its host environment. Creates dedicated test
users without changing global quota policy. Results and screenshots are ignored.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import secrets
import statistics
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import devctl

ASSISTANT = '[data-step-type="assistant_message"]'
CONTENT = ASSISTANT + ' .message-content'
ERROR_TEXT = (
    "Chat request failed", "排队等待超时", "额度已用尽", "剩余额度不足",
    "当前不在配额生效时间内", "当前没有可用配额配置",
)
OBSERVER = r"""() => {
  window.chatProbe = {started: null, first_feedback_ms: null,
    first_content_ms: null, queued: false, lengths: [], first_text: null};
  document.addEventListener('click', e => {
    if (e.target.closest('#chat-submit')) {
      window.chatProbe = {started: performance.now(), first_feedback_ms: null,
        submitted_utc_ms: Date.now(), first_content_ms: null, queued: false,
        lengths: [], first_text: null};
    }
  }, true);
  new MutationObserver(() => {
    const p = window.chatProbe;
    if (p.started === null) return;
    const messages = document.querySelectorAll('[data-step-type="assistant_message"]');
    const message = messages[messages.length - 1];
    if (!message) return;
    const now = performance.now() - p.started;
    if (p.first_feedback_ms === null) p.first_feedback_ms = now;
    const text = (message.querySelector('.message-content')?.textContent || '').trim();
    if (text.includes('系统繁忙') || text.includes('正在排队')) {p.queued = true; return;}
    if (!text) return;
    if (p.first_content_ms === null) {p.first_content_ms = now; p.first_text = text;}
    const prev = p.lengths[p.lengths.length - 1];
    if ((!prev || prev[1] !== text.length) && p.lengths.length < 32)
      p.lengths.push([now, text.length]);
  }).observe(document.body, {subtree: true, childList: true, characterData: true});
}"""


def percentile(values, fraction):
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


async def prepare(browser, admin, base, prefix, index, password, gate):
    async with gate:
        username = f"{prefix}-{index}"
        response = await admin.post(base + '/admin/api/users', data={
            'identifier': username, 'display_name': username,
            'password': password, 'is_active': True,
        })
        if response.status != 200:
            raise RuntimeError(f"User creation failed: HTTP {response.status}")
        context = await browser.new_context(locale='zh-CN', viewport={'width': 1280, 'height': 900})
        page = await context.new_page()
        errors = {'page_errors': [], 'http_5xx': [], 'websocket_closed': 0,
                  'crashes': 0, 'navigations': [], 'document_responses': [],
                  'failed_requests': [], 'console_errors': []}
        connected_sockets = set()
        last_socket_event = time.monotonic()

        def watch_socket(ws):
            nonlocal last_socket_event
            last_socket_event = time.monotonic()

            def received(payload):
                nonlocal last_socket_event
                if isinstance(payload, str) and payload.startswith('40'):
                    connected_sockets.add(ws)
                    last_socket_event = time.monotonic()

            def closed():
                nonlocal last_socket_event
                connected_sockets.discard(ws)
                errors['websocket_closed'] += 1
                last_socket_event = time.monotonic()

            ws.on('framereceived', received)
            ws.on('close', closed)

        page.on('pageerror', lambda error: errors['page_errors'].append(str(error)))
        page.on('response', lambda r: errors['http_5xx'].append({'status': r.status, 'url': r.url})
                if r.status >= 500 and '/chat' in r.url else None)
        page.on('websocket', watch_socket)
        page.on('crash', lambda: errors.update(crashes=errors['crashes'] + 1))
        page.on('framenavigated', lambda frame: errors['navigations'].append({
            'url': frame.url, 'utc_ms': int(time.time() * 1000),
        }) if frame == page.main_frame else None)
        page.on('response', lambda r: errors['document_responses'].append({
            'url': r.url, 'status': r.status,
        }) if r.request.resource_type == 'document' else None)
        page.on('requestfailed', lambda r: errors['failed_requests'].append({
            'url': r.url, 'error': r.failure,
        }))
        page.on('console', lambda message: errors['console_errors'].append(message.text[:1000])
                if message.type == 'error' and len(errors['console_errors']) < 20 else None)
        await page.goto(base + '/chat', wait_until='domcontentloaded', timeout=60000)
        await page.locator('#email').wait_for(timeout=60000)
        await page.locator('#email').fill(username)
        await page.locator('#password').fill(password)
        await page.locator('button[type="submit"]').click()
        await page.locator('#chat-input').wait_for(timeout=60000)
        await page.wait_for_function("() => !document.querySelector('#chat-input')?.disabled", timeout=60000)
        # Login replaces its unauthenticated socket. Start measurement only
        # after the authenticated Socket.IO connection has settled.
        deadline = time.monotonic() + 30
        while not connected_sockets or time.monotonic() - last_socket_event < .5:
            if time.monotonic() >= deadline:
                raise RuntimeError('Authenticated Chat socket did not connect')
            await asyncio.sleep(.1)
        await page.evaluate(OBSERVER)
        return {'context': context, 'page': page, 'username': username, 'errors': errors,
                'prepared_document': await document_state(page)}


async def document_state(page):
    return await page.evaluate("""() => ({url: location.href, title: document.title,
        wasDiscarded: document.wasDiscarded, readyState: document.readyState,
        timeOrigin: performance.timeOrigin, bodyLength: document.body?.innerHTML.length,
        bodyText: document.body?.innerText.slice(0, 1500), hasProbe: !!window.chatProbe,
        navigation: performance.getEntriesByType('navigation').map(n => n.toJSON())})""")


async def settled_quota(context, base):
    for _ in range(60):
        response = await context.request.get(base + '/chat/api/quota/me')
        if response.status != 200:
            raise RuntimeError(f"Quota request failed: HTTP {response.status}")
        payload = await response.json()
        if payload['quota']['reserved_tokens'] == 0:
            return payload['quota']
        await asyncio.sleep(.25)
    raise RuntimeError('Quota reservation did not settle')


async def send(session, base, prompt, artifacts, label, index):
    page = session['page']
    websocket_closed_before = session['errors']['websocket_closed']
    navigations_before = len(session['errors']['navigations'])
    started = time.perf_counter()
    result = {'username': session['username'], 'ok': False}
    try:
        if not await page.evaluate('!!window.chatProbe'):
            raise RuntimeError('Measurement probe disappeared before submission')
        await page.locator('#chat-input').fill(prompt)
        await page.locator('#chat-submit').click(timeout=30000)
        await page.wait_for_function('() => !window.chatProbe || window.chatProbe.first_content_ms !== null', timeout=240000)
        if not await page.evaluate('!!window.chatProbe'):
            raise RuntimeError('Measurement probe disappeared during generation')
        await page.locator('#stop-button').wait_for(state='hidden', timeout=240000)
        text = await page.locator(CONTENT).last.inner_text()
        if any(error in text for error in ERROR_TEXT):
            raise RuntimeError(text[:500])
        quota = await settled_quota(session['context'], base)
        # Settlement must update the visible quota without a page reload.
        await page.wait_for_function("expected => { const text = document.querySelector('#user-nav-button')?.textContent || ''; return text.replace(/[,\\s]/g, '').includes(expected); }",
                                     arg=f"{quota['used_tokens']}/{quota['limit_tokens']}", timeout=10000)
        probe = await page.evaluate('window.chatProbe')
        assert text.strip(), 'Empty assistant output'
        assert len(probe['lengths']) > 1, 'No incremental rendering observed'
        assert not session['errors']['page_errors'], session['errors']['page_errors']
        assert not session['errors']['http_5xx'], session['errors']['http_5xx']
        assert not session['errors']['crashes'], 'Browser renderer crashed'
        assert len(session['errors']['navigations']) == navigations_before, 'Unexpected navigation during generation'
        websocket_closures = session['errors']['websocket_closed'] - websocket_closed_before
        assert websocket_closures == 0, 'WebSocket closed during generation'
        assert await page.locator('#chat-input').is_enabled(), 'Input stayed disabled'
        result.update(ok=True, ttft_s=probe['first_content_ms'] / 1000,
                      feedback_s=probe['first_feedback_ms'] / 1000, queued=probe['queued'],
                      rendered_length=len(text), render_updates=len(probe['lengths']),
                      total_s=time.perf_counter() - started, quota=quota,
                      browser=probe, websocket_closures_during_request=websocket_closures,
                      errors=copy.deepcopy(session['errors']))
        if index == 0:
            await page.screenshot(path=str(artifacts / f'chat-latency-{label}.png'), full_page=True)
    except Exception as error:
        result['error'] = str(error)
        result['errors'] = copy.deepcopy(session['errors'])
        try:
            result['browser'] = await page.evaluate('window.chatProbe')
            result['prepared_document'] = session['prepared_document']
            result['failed_document'] = await document_state(page)
            await page.screenshot(path=str(artifacts / f'chat-latency-{label}-failed-{index}.png'))
        except Exception:
            pass
    return result


async def auth_network_recovery(page):
    """A failed /user revalidation must not sign out an active Chat user."""
    failed = asyncio.Event()

    async def interrupt(route):
        failed.set()
        await route.abort('connectionfailed')

    endpoint = '**/chat/user'
    await page.bring_to_front()
    await page.route(endpoint, interrupt)
    try:
        # Allow SWR's focus throttle to expire, then exercise its real fetcher.
        await page.wait_for_timeout(5500)
        await page.evaluate("() => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('online')); }")
        await asyncio.wait_for(failed.wait(), timeout=20)
        await page.wait_for_timeout(1000)
        assert '/login' not in page.url, 'Network error incorrectly signed out the user'
        assert await page.locator('#stop-button').is_visible(), 'Network error lost active Chat controls'
    finally:
        await page.unroute(endpoint, interrupt)

    async with page.expect_response(lambda r: r.url.endswith('/chat/user') and r.status == 200, timeout=20000):
        await page.wait_for_timeout(5500)
        await page.evaluate("() => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('online')); }")
    assert '/login' not in page.url, 'Authentication did not recover in place'
    return {'failed_revalidation_observed': True, 'authenticated_retry_succeeded': True}


async def lifecycle(browser, admin, base, prefix, password, artifacts, env):
    """Verify stop, next request, and refresh during a real model generation."""
    session = await prepare(browser, admin, base, prefix, 'lifecycle', password, asyncio.Semaphore(1))
    page = session['page']
    result = {}
    try:
        prompt = 'Output a numbered list from 1 to 1000. Each line: <number>. alpha beta gamma delta epsilon zeta eta theta iota kappa. Start immediately.'
        await page.locator('#chat-input').fill(prompt)
        await page.locator('#chat-submit').click()
        await page.wait_for_function("() => (document.querySelector('[data-step-type=\"assistant_message\"] .message-content')?.textContent || '').length > 100", timeout=120000)
        result['auth_network_recovery'] = await auth_network_recovery(page)
        await page.locator('#stop-button').click()
        await page.locator('#stop-button').wait_for(state='hidden', timeout=30000)
        await settled_quota(session['context'], base)
        partial = await page.locator(CONTENT).last.inner_text()
        await page.wait_for_timeout(1500)
        assert (await page.locator(CONTENT).last.inner_text()) == partial, 'Output kept growing after stop'
        assert await page.locator('#chat-input').is_enabled()
        result['stop_and_quota_settled'] = True
        # A new thread keeps the follow-up independent of the interrupted instruction.
        await page.goto(base + '/chat', wait_until='domcontentloaded')
        await page.locator('#chat-input').wait_for()
        session['errors']['websocket_closed'] = 0
        await page.evaluate(OBSERVER)
        followup = await send(session, base, '只回答：停止后可以继续聊天。', artifacts, 'after-stop', 0)
        assert followup['ok'], followup
        result['chat_after_stop'] = followup
        await page.goto(base + '/chat', wait_until='domcontentloaded')
        await page.locator('#chat-input').wait_for()
        await page.locator('#chat-input').fill(prompt)
        await page.locator('#chat-submit').click()
        await page.wait_for_function("() => (document.querySelector('[data-step-type=\"assistant_message\"] .message-content')?.textContent || '').length > 120", timeout=120000)
        before = len(await page.locator(CONTENT).last.inner_text())
        import psycopg
        async with await psycopg.AsyncConnection.connect(env['DATABASE_URL']) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute('SELECT id::text FROM threads WHERE "userIdentifier" = %s ORDER BY "createdAt" DESC LIMIT 1', (session['username'],))
                row = await cursor.fetchone()
                assert row, 'Thread not persisted'
        await page.goto(base + '/chat/thread/' + row[0], wait_until='domcontentloaded')
        await page.wait_for_function("minimum => (document.querySelector('[data-step-type=\"assistant_message\"] .message-content')?.textContent || '').length > minimum", arg=before + 100, timeout=120000)
        result['refresh_resumed_and_grew'] = True
        result['resume_length_before'] = before
        result['resume_length_after'] = len(await page.locator(CONTENT).last.inner_text())
        await page.locator('#stop-button').wait_for(state='visible', timeout=10000)
        await page.locator('#stop-button').click()
        await page.locator('#stop-button').wait_for(state='hidden', timeout=30000)
        await settled_quota(session['context'], base)
        partial = await page.locator(CONTENT).last.inner_text()
        await page.wait_for_timeout(2000)
        assert (await page.locator(CONTENT).last.inner_text()) == partial, 'Resumed output kept growing after stop'
        assert await page.locator('#chat-input').is_enabled()
        result['stop_after_refresh_and_quota_settled'] = True
        copy_button = page.locator(ASSISTANT).last.locator('button').first
        await copy_button.click()
        await copy_button.locator('svg.lucide-check').wait_for(timeout=5000)
        result['copy_confirmed'] = True
        assert not session['errors']['page_errors'], session['errors']['page_errors']
        assert not session['errors']['http_5xx'], session['errors']['http_5xx']
        assert not session['errors']['crashes'], 'Browser renderer crashed'
        await page.screenshot(path=str(artifacts / 'chat-latency-lifecycle.png'), full_page=True)
        result['ok'] = True
    except Exception as error:
        result.update(ok=False, error=str(error), errors=copy.deepcopy(session['errors']))
        await page.screenshot(path=str(artifacts / 'chat-latency-lifecycle-failed.png'), full_page=True)
    finally:
        await session['context'].close()
    return result


async def main(args):
    env = devctl.load_env('host')
    base = args.base_url.rstrip('/')
    artifacts = ROOT / '.local/acceptance'
    artifacts.mkdir(parents=True, exist_ok=True)
    prefix = f'chat-latency-{int(time.time())}'
    password = secrets.token_urlsafe(24)
    report = {'base_url': base, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'measurement': 'browser click to first rendered model content', 'groups': []}
    path = artifacts / args.output_name
    async with async_playwright() as p:
        admin = await p.request.new_context()
        deadline = time.monotonic() + 120
        for endpoint in ('/chat/health', '/admin/healthz'):
            while True:
                try:
                    ready = await admin.get(base + endpoint, timeout=5000)
                    if ready.status == 200:
                        break
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Frontend entrypoint did not become ready: {endpoint}')
                await asyncio.sleep(1)
        response = await admin.post(base + '/admin/api/auth/login', data={
            'username': env.get('ADMIN_APP_USERNAME', 'admin'),
            'password': env.get('ADMIN_APP_PASSWORD', 'admin123'),
        })
        assert response.status == 200, f'Admin login: HTTP {response.status}'
        browser = await p.chromium.launch(headless=True)
        report['browser_version'] = browser.version
        try:
            for concurrency in ([] if args.lifecycle_only else args.concurrency):
                count = args.single_requests if concurrency == 1 else concurrency
                label = f'{concurrency}c'
                gate = asyncio.Semaphore(8)
                print(f'Preparing {count} real browser sessions for {label}', flush=True)
                sessions = await asyncio.gather(*(prepare(browser, admin, base, prefix + '-' + label, index, password, gate) for index in range(count)))
                prompt = ('只用一句话说明为什么服务启动后需要预热。' if concurrency == 1 else
                          'Write exactly 40 numbered lines. Each line must follow this format: <number>. Measure queue latency and verify streaming response under load. Do not add a preamble or conclusion.')
                async def one(index, session):
                    async with limiter:
                        result = await send(session, base, f'Request {index}. {prompt}', artifacts, label, index)
                    print(f'{label}: request {index} ' + ('ok' if result['ok'] else result.get('error', 'failed')), flush=True)
                    return result
                limiter = asyncio.Semaphore(concurrency)
                try:
                    rows = await asyncio.gather(*(one(i, s) for i, s in enumerate(sessions)))
                finally:
                    await asyncio.gather(*(s['context'].close() for s in sessions))
                good = [r for r in rows if r['ok']]
                summary = {'concurrency': concurrency, 'requests': count, 'success': len(good), 'failed': count - len(good), 'queued': sum(r['queued'] for r in good)}
                if good:
                    for metric in ('ttft_s', 'feedback_s', 'total_s'):
                        values = [r[metric] for r in good]
                        summary[metric] = {'mean': statistics.mean(values), 'p50': percentile(values, .5), 'p95': percentile(values, .95), 'max': max(values)}
                    submitted = [r['browser']['submitted_utc_ms'] for r in good]
                    summary['submission_spread_s'] = (max(submitted) - min(submitted)) / 1000
                import psycopg
                async with await psycopg.AsyncConnection.connect(env['DATABASE_URL']) as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute('SELECT user_identifier, prompt_tokens, completion_tokens, total_tokens, status, usage_source FROM quota_usage_ledger WHERE user_identifier = ANY(%s)', ([r['username'] for r in rows],))
                        usage = {r[0]: dict(zip(('prompt_tokens', 'completion_tokens', 'total_tokens', 'status', 'usage_source'), r[1:])) for r in await cursor.fetchall()}
                for row in rows:
                    row['usage'] = usage.get(row['username'])
                completions = [r['usage']['completion_tokens'] for r in good if r.get('usage')]
                if completions:
                    summary['completion_tokens'] = {'mean': statistics.mean(completions), 'min': min(completions), 'max': max(completions)}
                report['groups'].append({'summary': summary, 'requests': rows})
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if len(good) != count:
                    raise RuntimeError(f'Browser failures in {label}; see {path}')
            if not args.skip_lifecycle:
                print('Checking stop, follow-up, refresh recovery, and copy', flush=True)
                report['lifecycle'] = await lifecycle(browser, admin, base, prefix, password, artifacts, env)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(report['lifecycle'], ensure_ascii=False), flush=True)
                assert report['lifecycle']['ok'], f'Chat lifecycle failed; see {path}'
        finally:
            await browser.close()
            await admin.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.getenv('ACCEPTANCE_ORIGIN', 'http://127.0.0.1:8088'))
    parser.add_argument('--concurrency', type=int, nargs='+', default=[1, 16, 100])
    parser.add_argument('--single-requests', type=int, default=3)
    parser.add_argument('--skip-lifecycle', action='store_true')
    parser.add_argument('--lifecycle-only', action='store_true')
    parser.add_argument('--output-name', default='chat-latency.json')
    args = parser.parse_args()
    if any(c < 1 for c in args.concurrency) or args.single_requests < 1:
        parser.error('Concurrency and request counts must be positive')
    asyncio.run(main(args))
