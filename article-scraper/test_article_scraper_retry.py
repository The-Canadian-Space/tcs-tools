"""Tests for tcs-workflows#255: a NASASpaceFlight page that hits a Cloudflare JS challenge gets one more try after a
10-second pause; other anti-bot errors and other sites still fail fast (#61), and transient errors keep their 1 s retry.

Run:
    python working-files/python-scripts/test_article_scraper_retry.py

Crawl4AI and lib_index are stubbed, and asyncio.sleep is recorded instead of waited, so this runs anywhere in a moment.
"""
import asyncio
import contextlib
import io
import os
import sys
import types
import unittest

_crawl4ai = types.ModuleType('crawl4ai')


class _Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


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

NSF = 'https://www.nasaspaceflight.com/2026/10/blue-origin-update-2/'
CHALLENGE = 'Blocked by anti-bot protection: Cloudflare JS challenge'


def _run(url, answers):
    """_fetch_with_retry with _fetch answering from the list in turn; returns (result, sleeps, fetch calls)."""
    calls, sleeps = [], []
    res = types.SimpleNamespace(success=True)

    async def fake_fetch(crawler, u, cfg):
        calls.append(u)
        err = answers[min(len(calls), len(answers)) - 1]
        return (None if err == 'NONE' else res), (None if err in (None, 'NONE') else err)

    async def fake_sleep(s):
        sleeps.append(s)

    saved = A._fetch, A.asyncio.sleep
    A._fetch, A.asyncio.sleep = fake_fetch, fake_sleep
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            out = asyncio.run(A._fetch_with_retry(None, url, None))
    finally:
        A._fetch, A.asyncio.sleep = saved
    return out, sleeps, len(calls)


class ChallengeRetryTests(unittest.TestCase):
    def test_nsf_challenge_gets_one_more_try_after_ten_seconds(self):
        (res, err, attempts), sleeps, calls = _run(NSF, [CHALLENGE, None])
        self.assertIsNone(err)
        self.assertEqual((attempts, calls, sleeps), (2, 2, [10]))

    def test_nsf_challenge_twice_fails_after_the_second_try(self):
        (res, err, attempts), sleeps, calls = _run(NSF, [CHALLENGE, CHALLENGE])
        self.assertEqual(err, CHALLENGE)
        self.assertEqual((attempts, calls, sleeps), (2, 2, [10]))

    def test_other_sites_still_fail_fast_on_a_challenge(self):
        (res, err, attempts), sleeps, calls = _run('https://spacenews.com/some-article/', [CHALLENGE, None])
        self.assertEqual(err, CHALLENGE)
        self.assertEqual((attempts, calls, sleeps), (1, 1, []))

    def test_other_anti_bot_errors_on_nsf_still_fail_fast(self):
        other = 'Blocked by anti-bot protection: captcha page'
        (res, err, attempts), sleeps, calls = _run(NSF, [other, None])
        self.assertEqual(err, other)
        self.assertEqual((attempts, calls), (1, 1))

    def test_transient_errors_keep_their_one_second_retry(self):
        (res, err, attempts), sleeps, calls = _run(NSF, ['Crawler exception: TimeoutError: page load', None])
        self.assertIsNone(err)
        self.assertEqual((attempts, sleeps), (2, [1]))

    def test_host_match_needs_the_real_domain(self):
        self.assertTrue(A._is_challenge_retryable('https://nasaspaceflight.com/x/', CHALLENGE))
        self.assertFalse(A._is_challenge_retryable('https://notnasaspaceflight.com/x/', CHALLENGE))
        self.assertFalse(A._is_challenge_retryable(NSF, ''))



class BlockedPageTests(unittest.TestCase):
    def _scrape(self, url, answers):
        calls = []
        blocked = types.SimpleNamespace(success=False, html='<html>challenge</html>', markdown='', media={'images': []},
                                        error_message=answers[0], url=url)

        async def fake_fetch(crawler, u, cfg):
            calls.append(u)
            err = answers[min(len(calls), len(answers)) - 1]
            return blocked, err

        async def fake_sleep(s):
            pass

        saved = A._fetch, A.asyncio.sleep
        A._fetch, A.asyncio.sleep = fake_fetch, fake_sleep
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                item = asyncio.run(A.scrape_one(None, {'url': url, 'title': 'T'}))
        finally:
            A._fetch, A.asyncio.sleep = saved
        return item, len(calls)

    def test_a_blocked_nsf_page_costs_two_requests_not_four(self):
        item, calls = self._scrape(NSF, [CHALLENGE, CHALLENGE])
        self.assertEqual(calls, 2)
        self.assertFalse(item.get('has_content'))
        self.assertIn('Cloudflare JS challenge', item.get('error', ''))

    def test_a_blocked_page_elsewhere_costs_one_request(self):
        item, calls = self._scrape('https://spacenews.com/some-article/', [CHALLENGE])
        self.assertEqual(calls, 1)
        self.assertFalse(item.get('has_content'))


if __name__ == '__main__':
    unittest.main()
