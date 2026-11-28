"""Tests for tcs-workflows#317: Aerospace America's plain fetch (AA_PLAIN_FETCH_V1).

Run:
    python working-files/python-scripts/test_article_scraper_317.py

From 2026-11-25 Aerospace America answered the scraper's headless browser with a Cloudflare JS challenge, so the
Healthcheck's Canada Rocket Company row got no article. A plain GET with the same Chrome user agent from the same VPS got
the whole page. A "plain_fetch" site is now fetched once with curl, and that HTML goes to the browser as raw: HTML with
process_in_browser and base_url, so the selector and the extractors run as on a live page. A failed plain fetch falls
back to the browser fetch as before.

Crawl4AI, lib_index and curl are stubbed, so this runs anywhere and fetches nothing.
"""
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest

_crawl4ai = types.ModuleType('crawl4ai')


class _Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)


_crawl4ai.AsyncWebCrawler = object
_crawl4ai.BrowserConfig = _Config
_crawl4ai.CrawlerRunConfig = _Config
_crawl4ai.CacheMode = types.SimpleNamespace(BYPASS='bypass')
sys.modules.setdefault('crawl4ai', _crawl4ai)
_lib_index = types.ModuleType('lib_index')
_lib_index.upsert_entries = lambda *a, **k: None
sys.modules.setdefault('lib_index', _lib_index)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import article_scraper as A  # noqa: E402

AA_URL = 'https://aerospaceamerica.aiaa.org/a-made-up-story-for-tests/'
AA_HEADLINE = 'A made-up headline for tests'
AA_BODY = ('This paragraph is made up for the tests and is long enough to count as an article body. ') * 6
PAGE = ('<!DOCTYPE html><html><head><meta property="og:title" content="' + AA_HEADLINE + '"></head><body>'
        + '<div class="post-content cf entry-content content-normal"><p>' + AA_BODY + '</p></div>'
        + '<!-- padding -->' * 200 + '</body></html>')
CHALLENGE_PAGE = ('<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>'
                  '<script>window._cf_chl_opt={cvId: "3"};</script>' + ' ' * 3000 + '</body></html>')
OLD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")


def _scoped():
    """The shape Crawl4AI returns for AA's css_selector (see test_article_scraper_299.py)."""
    return ("<div class='crawl4ai-result'>\n"
            '<div class="post-content cf entry-content content-normal"><p>' + AA_BODY + '</p></div>\n'
            '<meta property="og:title" content="' + AA_HEADLINE + '">\n</div>')


def _result(html='', markdown=''):
    return types.SimpleNamespace(html=html, markdown=markdown, success=True, media={'images': []})


def _curl(stdout=b'', returncode=0, stderr=b'', raises=None):
    """A subprocess.run stand-in that records the command."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run, calls


def _plain(run):
    saved = A.subprocess.run
    A.subprocess.run = run
    try:
        return A._plain_get(AA_URL)
    finally:
        A.subprocess.run = saved


class PlainGetTests(unittest.TestCase):
    def test_a_200_page_is_returned(self):
        run, calls = _curl(PAGE.encode() + b'\n200')
        self.assertEqual(_plain(run), (PAGE, ''))
        self.assertEqual(len(calls), 1)

    def test_the_request_is_the_measured_one(self):
        run, calls = _curl(PAGE.encode() + b'\n200')
        _plain(run)
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[0], 'curl')
        self.assertEqual(cmd[cmd.index('-A') + 1], A.BROWSER_UA)
        self.assertIn('--compressed', cmd)
        self.assertEqual(cmd[-2:], ['--', AA_URL])   # the URL can't be read as an option
        self.assertEqual(cmd[cmd.index('--max-time') + 1], str(A.PLAIN_FETCH_TIMEOUT_S))
        self.assertGreater(kwargs['timeout'], A.PLAIN_FETCH_TIMEOUT_S)
        self.assertTrue(kwargs['capture_output'])

    def test_not_200_is_a_failure(self):
        for code in (b'403', b'503', b'000'):
            with self.subTest(code=code):
                run, _ = _curl(b'<html>denied</html>\n' + code)
                self.assertEqual(_plain(run), ('', 'plain fetch: HTTP ' + code.decode()))

    def test_a_challenge_page_is_a_failure(self):
        run, _ = _curl(CHALLENGE_PAGE.encode() + b'\n200')
        self.assertEqual(_plain(run), ('', 'plain fetch: a challenge page'))

    def test_a_near_empty_page_is_a_failure(self):
        run, _ = _curl(b'<html><body>ok</body></html>\n200')
        html, why = _plain(run)
        self.assertEqual(html, '')
        self.assertTrue(why.startswith('plain fetch: only '))

    def test_curl_errors_are_failures_not_exceptions(self):
        run, _ = _curl(b'', returncode=28, stderr=b'curl: (28) Operation timed out')
        self.assertEqual(_plain(run), ('', 'plain fetch: curl exit 28: curl: (28) Operation timed out'))
        for exc in (FileNotFoundError('curl'), subprocess.TimeoutExpired('curl', 25)):
            with self.subTest(exc=type(exc).__name__):
                run, _ = _curl(raises=exc)
                html, why = _plain(run)
                self.assertEqual(html, '')
                self.assertTrue(why.startswith('plain fetch: ' + type(exc).__name__))


class ScrapeOneTests(unittest.TestCase):
    """scrape_one with _plain_get and _fetch_with_retry replaced; records what the browser was asked to load."""

    def _scrape(self, url, plain, *results):
        queue, fetches, plains = list(results), [], []

        def fake_plain(u):
            plains.append(u)
            return plain

        async def fake_fetch(crawler, u, cfg):
            fetches.append((u, cfg.kwargs))
            return queue.pop(0), None, 1

        saved = A._fetch_with_retry, A._plain_get
        A._fetch_with_retry, A._plain_get = fake_fetch, fake_plain
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                item = asyncio.run(A.scrape_one(None, {'url': url, 'title': ''}))
        finally:
            A._fetch_with_retry, A._plain_get = saved
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
        return item, fetches, plains, events

    def test_the_browser_gets_the_plain_html_with_the_same_settings(self):
        item, fetches, plains, events = self._scrape(AA_URL, (PAGE, ''), _result(_scoped(), AA_BODY))
        self.assertEqual(plains, [AA_URL])
        self.assertEqual(len(fetches), 1)
        u, kw = fetches[0]
        self.assertEqual(u, 'raw:' + PAGE)
        self.assertIs(kw['process_in_browser'], True)
        self.assertEqual(kw['base_url'], AA_URL)
        live = A.run_config_for(AA_URL).kwargs
        self.assertEqual({k: v for k, v in kw.items() if k not in ('process_in_browser', 'base_url')}, live)
        self.assertEqual(item['title'], AA_HEADLINE)
        self.assertTrue(item['has_content'])
        self.assertEqual(item['url'], AA_URL)
        self.assertEqual(events, [])

    def test_the_whole_page_retry_uses_the_same_html(self):
        # An empty scoped pass is retried unscoped; with the plain HTML that's no second request to the site.
        item, fetches, _, events = self._scrape(AA_URL, (PAGE, ''), _result('', ''), _result(_scoped(), AA_BODY))
        self.assertEqual(len(fetches), 2)
        for u, kw in fetches:
            self.assertEqual(u, 'raw:' + PAGE)
            self.assertIs(kw['process_in_browser'], True)
            self.assertEqual(kw['base_url'], AA_URL)
        self.assertNotIn('css_selector', fetches[1][1])
        self.assertEqual(item['scope'], 'whole_page')
        self.assertEqual([e['event'] for e in events], ['scrape_recovered', 'scrape_whole_page'])

    def test_a_failed_plain_fetch_falls_back_to_the_browser(self):
        item, fetches, plains, events = self._scrape(AA_URL, ('', 'plain fetch: HTTP 403'), _result(_scoped(), AA_BODY))
        self.assertEqual(plains, [AA_URL])
        self.assertEqual(fetches, [(AA_URL, A.run_config_for(AA_URL).kwargs)])
        self.assertEqual(events, [{'event': 'plain_fetch_fail', 'url': AA_URL, 'error': 'plain fetch: HTTP 403',
                                   'scraper': 'article_scraper.py', 'ts': events[0]['ts']}])
        self.assertEqual(item['title'], AA_HEADLINE)

    def test_other_sites_skip_the_plain_fetch(self):
        url = 'https://spacenews.com/a-made-up-story/'
        html = '<html><body><div class="entry-content"><p>' + AA_BODY + '</p></div></body></html>'
        _, fetches, plains, _ = self._scrape(url, ('never', ''), _result(html, AA_BODY))
        self.assertEqual(plains, [])
        self.assertEqual(fetches[0][0], url)
        self.assertNotIn('process_in_browser', fetches[0][1])


class RetryLogTests(unittest.TestCase):
    """_fetch_with_retry on raw: HTML logs the page's URL, never the HTML."""

    def test_the_retry_event_names_the_page(self):
        answers, sleeps = ['browser has been closed', None], []

        async def fake_fetch(crawler, u, cfg):
            return _result(), answers.pop(0)

        async def fake_sleep(s):
            sleeps.append(s)

        saved = A._fetch, A.asyncio.sleep
        A._fetch, A.asyncio.sleep = fake_fetch, fake_sleep
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                res, e, attempts = asyncio.run(A._fetch_with_retry(
                    None, 'raw:' + PAGE, A.run_config_for(AA_URL, process_in_browser=True, base_url=AA_URL)))
        finally:
            A._fetch, A.asyncio.sleep = saved
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual((e, attempts, sleeps), (None, 2, [1]))
        self.assertEqual(events[0]['event'], 'scrape_retry')
        self.assertEqual(events[0]['url'], AA_URL)
        self.assertNotIn(AA_BODY[:40], err.getvalue())


class ConfigTests(unittest.TestCase):
    def test_only_aerospace_america_is_plain_fetched(self):
        self.assertEqual([k for k, v in A.SITE_CONFIG.items() if v.get('plain_fetch')], ['aerospaceamerica.aiaa.org'])

    def test_the_browser_user_agent_is_unchanged(self):
        self.assertEqual(A.BROWSER_UA, OLD_UA)

    def test_extra_settings_reach_the_config(self):
        kw = A.run_config_for(AA_URL, process_in_browser=True, base_url=AA_URL).kwargs
        self.assertEqual(kw['base_url'], AA_URL)
        self.assertEqual({k: v for k, v in kw.items() if k not in ('process_in_browser', 'base_url')},
                         A.run_config_for(AA_URL).kwargs)


if __name__ == '__main__':
    unittest.main(verbosity=2)
