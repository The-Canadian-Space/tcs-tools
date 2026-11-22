// Tests for x_scraper.js (tcs-workflows#296: the timeline fallback). No network: a fake client stands in for
// rettiwt-api. Run next to an installed rettiwt-api (x_scraper.js imports it):
//   node --test x_scraper.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  compileAnyWords,
  runFeeds,
  buildFeedMarkdown,
  PAGE_SIZE,
  TIMELINE_MAX_PAGES,
} from './x_scraper.js';

const HOUR = 3600 * 1000;
const ago = (hours) => new Date(Date.now() - hours * HOUR).toISOString();
const ACC = { id: '100', userName: 'Acct' };
const OTHER = { id: '200', userName: 'Someone' };
let seq = 0;
const tw = (hours, extra = {}) => ({
  id: String(++seq),
  createdAt: ago(hours),
  tweetBy: ACC,
  fullText: `post ${seq}`,
  media: [],
  ...extra,
});

// A fake rettiwt: search() is a function or an Error to throw; pages = the timeline pages, page i has cursor `c<i+1>`.
function fake({ search, users = { Acct: ACC, Other2: { id: '300', userName: 'Other2' } }, pages = {}, repliesError } = {}) {
  const calls = { search: 0, details: 0, replies: 0, cursors: [] };
  return {
    calls,
    tweet: {
      search: async (filter, count, cursor) => {
        calls.search++;
        if (search instanceof Error) throw search;
        return search(filter, count, cursor);
      },
    },
    user: {
      details: async (name) => {
        calls.details++;
        return users[name] || null;
      },
      replies: async (id, count, cursor) => {
        calls.replies++;
        calls.cursors.push(cursor ?? null);
        if (repliesError) throw repliesError;
        const list = pages[id] || [];
        const i = cursor ? Number(String(cursor).slice(1)) : 0;
        const page = list[i] || [];
        return { list: page, next: i + 1 < list.length ? `c${i + 1}` : '' };
      },
    },
  };
}
const feedOf = (extra = {}) => ({ username: 'Acct', display_name: 'Account', key: 'acct', ...extra });
const cfgOf = (feeds, extra = {}) => ({ feeds, since_days: 7, max_per_feed: 200, ...extra });
// Not one of the transient messages, so the search retries don't sleep in tests.
const SEARCH_DOWN = new Error('Request failed with status code 400');

test('compileAnyWords: words and phrases stand alone, any case, with # @ and punctuation around them', () => {
  const re = compileAnyWords(['spacex', 'super heavy', 'IFT-12']);
  for (const s of ['SpaceX launches', '#SpaceX', 'thanks @SpaceX!', 'see spacex.com', 'Super  Heavy booster', 'IFT-12 soon', '(spacex)']) {
    assert.ok(re.test(s), s);
  }
  for (const s of ['spacexplorer', 'superheavy', 'xspacex', 'IFT-123']) assert.ok(!re.test(s), s);
  assert.equal(compileAnyWords([]), null);
  assert.equal(compileAnyWords(undefined), null);
  assert.equal(compileAnyWords(['', '  ']), null);
  assert.ok(compileAnyWords(['a+b']).test('try a+b now'), 'regex characters are escaped');
});

test('search works: feed read by search, timeline never called, old fields kept', async () => {
  const t1 = tw(2);
  const rt = fake({ search: async () => ({ list: [t1], next: '' }) });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  assert.equal(rt.calls.replies, 0);
  assert.equal(rt.calls.details, 0);
  for (const k of ['run_id', 'since_days', 'feed_count', 'auth_failed', 'suspected_auth_issue', 'error_count', 'error_ratio', 'feeds', 'index']) {
    assert.ok(k in out, `top-level ${k}`);
  }
  assert.equal(out.search_down, false);
  assert.equal(out.search_error, null);
  assert.equal(out.timeline_feeds, 0);
  const f = out.feeds[0];
  assert.equal(f.via, 'search');
  assert.equal(f.error, null);
  assert.equal(f.tweet_count, 1);
  assert.ok(!('coverage_partial' in f));
  for (const k of ['username', 'key', 'display_name', 'source_description', 'tweet_count', 'word_count', 'markdown_field', 'error', 'markdown']) {
    assert.ok(k in f, `feed ${k}`);
  }
});

test('search 404 (the 11-21 failure): the timeline is read with the search rules', async () => {
  const pinnedOld = tw(24 * 20); // pinned, 20 days old, first on page 1
  const own1 = tw(1);
  const retweet = tw(2, { retweetedTweet: { id: 'x' } });
  const parent = tw(3, { tweetBy: OTHER }); // the post being replied to
  const reply = tw(3.5, { replyTo: parent.id, fullText: '@Someone yes, on schedule' });
  const selfThread = tw(4, { replyTo: own1.id, fullText: 'and part two' });
  const own2 = tw(30);
  const old = tw(24 * 8); // outside the 7-day window, on page 2
  const rt = fake({
    search: new Error('Request failed with status code 404'),
    pages: { 100: [[pinnedOld, own1, retweet, parent, reply, selfThread], [own2, old]] },
  });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  const f = out.feeds[0];
  assert.equal(f.error, null);
  assert.equal(f.via, 'timeline');
  assert.equal(f.coverage_partial, false);
  assert.equal(f.covered_since, null);
  assert.equal(f.timeline_pages, 2);
  assert.equal(out.search_down, true);
  assert.match(out.search_error, /^Search failed on page 1: .*404/);
  assert.equal(out.timeline_feeds, 1);
  assert.equal(out.error_count, 0);
  assert.equal(out.suspected_auth_issue, false);
  assert.equal(out.auth_failed, false);
  // newest first; no retweet, no other author, no old posts; replies kept (exclude_replies is off)
  const ids = f.markdown.match(/status\/(\d+)\)/g).map((s) => s.replace(/\D/g, ''));
  assert.deepEqual(ids, [own1.id, reply.id, selfThread.id, own2.id]);
  assert.equal(f.tweet_count, 4);
  assert.ok(!f.markdown.includes('Coverage note'));
});

test('search answers an empty success but the timeline has posts (seen 11-21): the timeline wins, later feeds skip search', async () => {
  const rt = fake({
    search: async () => ({ list: [], next: '' }),
    users: { Acct: ACC, Two: { id: '300', userName: 'Two' } },
    pages: { 100: [[tw(1), tw(2)]], 300: [[tw(3, { tweetBy: { id: '300', userName: 'Two' } })]] },
  });
  const out = await runFeeds(rt, cfgOf([feedOf(), { username: 'Two', display_name: 'Two', key: 'two' }]));
  const [a, b] = out.feeds;
  assert.equal(a.via, 'timeline');
  assert.equal(a.search_empty, true);
  assert.equal(a.tweet_count, 2);
  assert.equal(b.via, 'timeline');
  assert.equal(rt.calls.search, 1, 'the second feed skips search');
  assert.equal(out.search_down, true);
  assert.match(out.search_error, /^Search returned no tweets for @Acct; its timeline has 2$/);
  assert.equal(out.error_count, 0);
});

test('search empty and the timeline empty too: search\'s answer stands', async () => {
  const rt = fake({ search: async () => ({ list: [], next: '' }), pages: { 100: [[tw(24 * 9)]] } });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  const f = out.feeds[0];
  assert.equal(f.via, 'search');
  assert.equal(f.timeline_checked, true);
  assert.equal(f.tweet_count, 0);
  assert.equal(out.search_down, false);
  assert.equal(out.search_error, null);
  assert.match(f.markdown, /No tweets from Account in the past week\./);
});

test('search empty and the timeline check fails: search\'s answer stands, the reason is kept', async () => {
  const rt = fake({ search: async () => ({ list: [], next: '' }), users: {} });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  const f = out.feeds[0];
  assert.equal(f.via, 'search');
  assert.equal(f.error, null);
  assert.equal(f.timeline_checked, false);
  assert.match(f.timeline_check_error, /account @Acct not found/);
  assert.equal(out.error_count, 0);
});

test('a retweet rettiwt did not link (text "RT @...") is still dropped', async () => {
  const unlinked = tw(1, { fullText: 'RT @NASA: Liftoff! The crew is on its way' });
  const own = tw(2, { fullText: 'Great day. RT @NASA: is how people used to quote' });
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [[unlinked, own]] } });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  assert.equal(out.feeds[0].tweet_count, 1);
  assert.ok(!out.feeds[0].markdown.includes('Liftoff'));
});

test('exclude_replies drops replies to others but keeps self-threads, as the search path does', async () => {
  const own1 = tw(1);
  const reply = tw(2, { replyTo: 'p', fullText: '@Someone thanks' });
  const selfThread = tw(3, { replyTo: own1.id, fullText: 'part two' });
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [[own1, reply, selfThread]] } });
  const out = await runFeeds(rt, cfgOf([feedOf({ exclude_replies: true })]));
  assert.equal(out.feeds[0].tweet_count, 2);
  assert.ok(!out.feeds[0].markdown.includes('@Someone thanks'));
});

test('any_words filters the timeline like search did', async () => {
  const a = tw(1, { fullText: 'Starship flight 12 is next week' });
  const b = tw(2, { fullText: 'Lunch was great' });
  const c = tw(3, { fullText: 'Watch the #Falcon9 launch' });
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [[a, b, c]] } });
  const out = await runFeeds(rt, cfgOf([feedOf({ any_words: ['starship', 'falcon9'] })]));
  assert.equal(out.feeds[0].tweet_count, 2);
  assert.ok(!out.feeds[0].markdown.includes('Lunch'));
});

test('at most PAGE_SIZE tweets, newest first, like the search path (one page)', async () => {
  const page1 = Array.from({ length: 15 }, (_, i) => tw(1 + i));
  const page2 = Array.from({ length: 15 }, (_, i) => tw(20 + i));
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [page1, page2, [tw(40)]] } });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  const f = out.feeds[0];
  assert.equal(f.tweet_count, PAGE_SIZE);
  assert.equal(f.timeline_pages, 2, 'stops once it has PAGE_SIZE');
  assert.equal(f.coverage_partial, false, 'a full list is not partial');
  const ids = f.markdown.match(/status\/(\d+)\)/g).map((s) => Number(s.replace(/\D/g, '')));
  assert.deepEqual(ids, [...ids].sort((x, y) => x - y), 'newest first');
  assert.equal(ids[0], Number(page1[0].id));
});

test('max_per_feed below PAGE_SIZE still caps the timeline', async () => {
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [Array.from({ length: 12 }, (_, i) => tw(1 + i))] } });
  const out = await runFeeds(rt, cfgOf([feedOf()], { max_per_feed: 5 }));
  assert.equal(out.feeds[0].tweet_count, 5);
});

test('the page cap stops a busy account: partial, covered_since from the LAST page, and a note', async () => {
  // Every page is inside the window but nothing matches any_words, so it would page forever.
  const pages = Array.from({ length: TIMELINE_MAX_PAGES + 5 }, (_, p) => Array.from({ length: 5 }, (_, i) => tw(p * 5 + i + 1, { fullText: 'nothing to see' })));
  pages[0].unshift(tw(24 * 30, { fullText: 'pinned, a month old' }));
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: pages } });
  const out = await runFeeds(rt, cfgOf([feedOf({ any_words: ['starship'] })]));
  const f = out.feeds[0];
  assert.equal(f.timeline_pages, TIMELINE_MAX_PAGES);
  assert.equal(rt.calls.replies, TIMELINE_MAX_PAGES);
  assert.equal(f.tweet_count, 0);
  assert.equal(f.coverage_partial, true);
  const lastPage = pages[TIMELINE_MAX_PAGES - 1];
  const oldestOnLast = lastPage.map((t) => t.createdAt).sort()[0];
  assert.equal(f.covered_since, oldestOnLast);
  assert.match(f.markdown, /Coverage note: X search was unavailable/);
  assert.match(f.markdown, /No tweets from Account since /);
  assert.ok(!f.markdown.includes('in the past week'));
  assert.equal(f.error, null);
});

test('a page with none of the account\'s posts in the window ends the read', async () => {
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [[tw(1)], [tw(24 * 9), tw(24 * 10)], [tw(2)]] } });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  assert.equal(out.feeds[0].timeline_pages, 2);
  assert.equal(out.feeds[0].tweet_count, 1);
  assert.equal(out.feeds[0].coverage_partial, false);
});

test('an old pinned post at the top of page 1 does not end the read early, and is not listed', async () => {
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [[tw(24 * 40), tw(5)], [tw(6)], [tw(24 * 9)]] } });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  assert.equal(out.feeds[0].tweet_count, 2);
  assert.equal(out.feeds[0].timeline_pages, 3);
});

test('a pinned post inside the window is listed by its date, and the cap keeps the newest', async () => {
  const pinned = tw(100); // 4 days old, pinned at the top
  const newer = Array.from({ length: PAGE_SIZE }, (_, i) => tw(1 + i));
  const rt = fake({ search: SEARCH_DOWN, pages: { 100: [[pinned, ...newer]] } });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  const ids = out.feeds[0].markdown.match(/status\/(\d+)\)/g).map((s) => s.replace(/\D/g, ''));
  assert.equal(ids.length, PAGE_SIZE);
  assert.ok(!ids.includes(pinned.id), 'the 20 newest win over an older pinned post');
  const rt2 = fake({ search: SEARCH_DOWN, pages: { 100: [[pinned, newer[0], newer[1]]] } });
  const out2 = await runFeeds(rt2, cfgOf([feedOf()]));
  const ids2 = out2.feeds[0].markdown.match(/status\/(\d+)\)/g).map((s) => s.replace(/\D/g, ''));
  assert.deepEqual(ids2, [newer[0].id, newer[1].id, pinned.id]);
});

test('once search fails, later feeds in the run skip it', async () => {
  const rt = fake({
    search: SEARCH_DOWN,
    users: { Acct: ACC, Two: { id: '300', userName: 'Two' } },
    pages: { 100: [[tw(1)]], 300: [[tw(2, { tweetBy: { id: '300', userName: 'Two' } })]] },
  });
  const out = await runFeeds(rt, cfgOf([feedOf(), { username: 'Two', display_name: 'Two', key: 'two' }]));
  assert.equal(rt.calls.search, 1, 'search tried once, for the first feed only');
  assert.deepEqual(out.feeds.map((f) => f.via), ['timeline', 'timeline']);
  assert.equal(out.timeline_feeds, 2);
  assert.equal(out.error_count, 0);
});

test('a 401/403 from search stays an auth failure: no fallback', async () => {
  const rt = fake({ search: new Error('Request failed with status code 401') });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  assert.equal(rt.calls.replies, 0);
  assert.equal(out.auth_failed, true);
  assert.equal(out.search_down, false);
  assert.match(out.feeds[0].error, /^Search failed on page 1: .*401/);
  assert.equal(out.feeds[0].via, 'search');
});

test('a 403 from the timeline after search failed still trips auth_failed', async () => {
  const rt = fake({ search: SEARCH_DOWN, repliesError: new Error('Request failed with status code 403') });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  assert.equal(out.auth_failed, true);
  assert.match(out.feeds[0].error, /^Search failed on page 1: .*; timeline fallback failed: .*403/);
});

test('when the fallback fails too, the feed errors as before (suspected_auth_issue by ratio)', async () => {
  const rt = fake({ search: SEARCH_DOWN, users: {} });
  const out = await runFeeds(rt, cfgOf([feedOf()]));
  const f = out.feeds[0];
  assert.match(f.error, /^Search failed on page 1: .*; timeline fallback failed: account @Acct not found$/);
  assert.equal(f.tweet_count, 0);
  assert.equal(out.error_count, 1);
  assert.equal(out.error_ratio, 1);
  assert.equal(out.suspected_auth_issue, true);
  assert.equal(out.auth_failed, false);
});

test('buildFeedMarkdown: unchanged without a partial coverage', () => {
  const feed = feedOf({ source_description: 'desc', header_note: 'note' });
  const t = { id: '9', createdAt: '2026-11-20T15:00:00.000Z', fullText: 'Hello &amp; welcome', media: [] };
  const when = new Date(t.createdAt).toLocaleString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true, timeZone: 'UTC',
  }) + ' UTC';
  assert.match(when, /^Fri, Nov 20, 2026, 3:00\s?PM UTC$/u);
  const expected =
    `### Account — desc (Past Week)\n\n> note\n\n#### Tweet from ${when}\n\n` +
    '**Summary:** Hello & welcome\n\n**Source URL:** [View Tweet](https://x.com/Acct/status/9)\n\n---\n\n';
  assert.equal(buildFeedMarkdown(feed, [t]), expected);
  assert.equal(buildFeedMarkdown(feed, [t], { partial: false, coveredSince: null }), expected);
  assert.equal(buildFeedMarkdown(feed, []), '### Account — desc (Past Week)\n\n> note\n\nNo tweets from Account in the past week.\n');
});
