#!/usr/bin/env node
/**
 * TCS X (Twitter) scraper — replaces twitterapi.io.
 *
 * Called from n8n SSH node:
 *   node /opt/tcs/scripts/x-scraper/x_scraper.js <BASE64_JSON>
 *
 * Input JSON shape:
 *   {
 *     "feeds": [
 *       {
 *         "username":     "elonmusk",       // required, no @
 *         "display_name": "Elon Musk",      // for markdown header
 *         "key":          "elon_musk",      // suffix for markdown_<key>_tweets field
 *         "any_words":    ["Starship","Falcon"],  // optional: OR keyword filter
 *         "exclude_retweets": true          // post-filter retweets
 *       },
 *       ...
 *     ],
 *     "since_days":   7,                    // 7-day rolling window
 *     "max_per_feed": 200,                  // safety cap; effective only if window doesn't bound first
 *     "run_id":       "568"                 // optional. set => FILE mode
 *   }
 *
 * Two output modes (matching article_scraper.py pattern):
 *
 *   1) INLINE (no run_id): emits JSON with full markdown bodies inline.
 *   2) FILE   (with run_id): writes tweets-<key>.md per feed under
 *      /opt/tcs/n8n/local_files/scraper-runs/<run_id>/ and emits a slim
 *      index.json on stdout.
 *
 * In FILE mode the index.json shape mirrors article_scraper.py for easy
 * agent integration:
 *   {
 *     "run_id": "568",
 *     "feed_count": 5,
 *     "feeds": [
 *       {
 *         "key": "elon_musk", "username": "elonmusk",
 *         "display_name": "Elon Musk", "tweet_count": 42,
 *         "content_file": "/opt/tcs/n8n/local_files/scraper-runs/568/tweets-elon_musk.md",
 *         "markdown_field": "markdown_elon_musk_tweets",
 *         "error": null
 *       },
 *       ...
 *     ]
 *   }
 *
 * Reads ./rettiwt-auth (relative to this script's directory, chmod 600) for
 * the apiKey.
 */
import { Rettiwt, TweetFilter } from 'rettiwt-api';
import { readFileSync, writeFileSync, mkdirSync, existsSync, realpathSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const AUTH_PATH = join(__dirname, '.rettiwt-auth');

// Twitter search caps at 20 tweets per page for user-timeline-style queries.
// We page until empty or older-than-window or safety cap is hit.
const PAGE_SIZE = 20;
const DEFAULT_MAX_PER_FEED = 200;

// X_SCRAPER_TIMELINE_FALLBACK_V1 (tcs-workflows#296, 2026-11-21).
// X moved its web app to a new client and retired the old search operation
// that rettiwt-api 7.2.0 calls (SearchTimeline M1jEez78PEfVfbQLvlWMvQ now
// answers 404; the new SearchByRawQuery answers "Internal server error" to
// rettiwt's requests). The account, timeline and tweet operations still work.
// So when search fails with anything but an auth error, the feed is read from
// the account's own timeline instead, with the rules search applied:
//   - UserTweetsAndReplies: the account's tweets AND replies, like `from:`
//     search (500 calls per 15 min; UserTweets has only 50);
//   - the same date window, exclude_replies and any_words rules;
//   - no native retweets: X search leaves them out by default (0 of the 936
//     tweets kept in scraper-runs/ on 11-21 were retweets);
//   - at most PAGE_SIZE tweets, newest first: the search path only ever reads
//     one page, because rettiwt 7.x gives `next` as a string and the loop
//     below reads `next.value` (left as it is here; changing it changes what
//     every blog gets).
// Once search fails in a run, the run's later feeds skip it.
// 20 pages read about a week of @elonmusk (the busiest feed) on 11-21; a run
// of 6 feeds stays far under the 500 calls per 15 min.
const TIMELINE_MAX_PAGES = 20;
const TIMELINE_MAX_ATTEMPTS = 3;

// ─────────────────────────────────────────────────────────────────
// CLI parsing
// ─────────────────────────────────────────────────────────────────

function parseInput() {
  const arg = process.argv[2];
  if (!arg) die('Missing base64-encoded JSON argument');

  let raw;
  try {
    raw = Buffer.from(arg, 'base64').toString('utf-8');
  } catch (e) {
    die(`Could not base64-decode argv[2]: ${e.message}`);
  }

  let cfg;
  try {
    cfg = JSON.parse(raw);
  } catch (e) {
    die(`Could not JSON.parse input: ${e.message}`);
  }

  if (!Array.isArray(cfg.feeds) || cfg.feeds.length === 0) {
    die('Input must include a non-empty "feeds" array');
  }
  cfg.since_days = cfg.since_days ?? 7;
  cfg.max_per_feed = cfg.max_per_feed ?? DEFAULT_MAX_PER_FEED;
  return cfg;
}

function die(msg) {
  console.log(JSON.stringify({ error: msg }));
  process.exit(1);
}

function loadApiKey() {
  if (!existsSync(AUTH_PATH)) {
    die(`Auth file not found at ${AUTH_PATH}`);
  }
  return readFileSync(AUTH_PATH, 'utf-8').trim();
}

// ─────────────────────────────────────────────────────────────────
// Per-feed scrape
// ─────────────────────────────────────────────────────────────────

// Retry transient Twitter errors (404/429/5xx/network). Auth errors (401/403)
// fail-fast because they almost always mean the cookie has been invalidated.
async function searchWithRetry(rettiwt, filter, count, cursor, maxAttempts = 5) {
  // X_SCRAPER_RETRY_V2 (2026-06-27 after NASA V3 run 1950 partial-failure)
  // Expanded transient-error regex to include 'unknown error' / 'search failed'
  // (Rettiwt wraps some 5xx/timeout responses as 'Unknown error'). Bumped
  // attempts 3 -> 5. Exponential backoff with jitter to spread retries.
  let lastErr;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await rettiwt.tweet.search(filter, count, cursor);
    } catch (e) {
      lastErr = e;
      const msg = String(e.message || '').toLowerCase();
      // Auth errors — bail immediately, do not retry.
      if (/401|403|unauthorized|forbidden/.test(msg)) throw e;
      const transient = /404|429|5\d\d|timeout|network|econnreset|socket hang up|unknown error|unknown|search failed/i.test(msg);
      if (!transient || attempt === maxAttempts) throw e;
      const delay = Math.min(8000, 500 * Math.pow(2, attempt - 1)) + Math.random() * 200;
      await new Promise(r => setTimeout(r, delay));
    }
  }
  throw lastErr;
}

function isAuthError(message) {
  return /401|403|unauthorized|forbidden/i.test(String(message || ''));
}

// Same retry rules as searchWithRetry, fewer attempts.
async function timelineWithRetry(rettiwt, userId, count, cursor, maxAttempts = TIMELINE_MAX_ATTEMPTS) {
  let lastErr;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await rettiwt.user.replies(userId, count, cursor);
    } catch (e) {
      lastErr = e;
      const msg = String(e.message || '').toLowerCase();
      if (isAuthError(msg)) throw e;
      const transient = /404|429|5\d\d|timeout|network|econnreset|socket hang up|unknown error|unknown/i.test(msg);
      if (!transient || attempt === maxAttempts) throw e;
      const delay = Math.min(8000, 500 * Math.pow(2, attempt - 1)) + Math.random() * 200;
      await new Promise(r => setTimeout(r, delay));
    }
  }
  throw lastErr;
}

// any_words as one case-insensitive test, like search's (w1 OR w2 ...): each
// word or quoted phrase must stand alone, so "spacex" matches "SpaceX",
// "#SpaceX", "@SpaceX" and "spacex.com" but not "spacexplorer".
function compileAnyWords(words) {
  if (!Array.isArray(words)) return null;
  const parts = words
    .map(w => String(w ?? '').trim())
    .filter(Boolean)
    .map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+'));
  if (parts.length === 0) return null;
  return new RegExp(`(?:^|[^\\p{L}\\p{N}_])(?:${parts.join('|')})(?=$|[^\\p{L}\\p{N}_])`, 'iu');
}

// Reads one feed from the account's timeline (see the note at the top).
// Returns { tweets, pages, partial, coveredSince }. partial = the page cap
// stopped it before it reached the start of the window; coveredSince = the
// oldest of the account's own posts on the last page read (ISO), set only
// when partial. (Not the oldest overall: page 1 can open with an old pinned
// post.)
async function timelineFallback(rettiwt, feed, startDate, cap) {
  const user = await rettiwt.user.details(feed.username);
  if (!user || !user.id) throw new Error(`account @${feed.username} not found`);
  const words = compileAnyWords(feed.any_words);
  const collected = [];
  const seen = new Set();
  let cursor;
  let pages = 0;
  let lastPageOldest = null;
  let reachedEnd = false;

  while (collected.length < cap && pages < TIMELINE_MAX_PAGES) {
    pages++;
    const result = await timelineWithRetry(rettiwt, user.id, PAGE_SIZE, cursor);
    const batch = result?.list || [];
    let ownInWindow = 0;
    let pageOldest = null;
    for (const t of batch) {
      if (!t || !t.id || seen.has(t.id)) continue;
      // Reply threads also carry the posts being replied to: keep the account's own.
      if (!t.tweetBy || String(t.tweetBy.id) !== String(user.id)) continue;
      seen.add(t.id);
      const created = new Date(t.createdAt);
      if (Number.isNaN(created.getTime())) continue;
      if (pageOldest === null || created < pageOldest) pageOldest = created;
      // The timeline can start with an old pinned post: skip it, keep paging.
      if (created < startDate) continue;
      ownInWindow++;
      // rettiwt can miss the retweet link on these timelines; the text still says it.
      if (t.retweetedTweet || /^RT @\w{1,15}:/.test(t.fullText || '')) continue;
      if (feed.exclude_replies && t.replyTo && /^@\w/.test((t.fullText || '').trim())) continue;
      if (words && !words.test(t.fullText || '')) continue;
      collected.push(t);
    }
    if (pageOldest) lastPageOldest = pageOldest;
    const next = typeof result?.next === 'string' ? result.next : result?.next?.value;
    // A page with none of the account's posts inside the window means the
    // window is done (the timeline is newest first).
    if (batch.length === 0 || ownInWindow === 0 || !next || next === cursor) {
      reachedEnd = true;
      break;
    }
    cursor = next;
  }

  collected.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  const partial = !reachedEnd && collected.length < cap;
  return {
    tweets: collected.slice(0, cap),
    pages,
    partial,
    coveredSince: partial && lastPageOldest ? lastPageOldest.toISOString() : null,
  };
}

// ctx is shared across one run's feeds: { searchDown, searchError }.
async function scrapeFeed(rettiwt, feed, startDate, maxPerFeed, ctx = {}) {
  if (ctx.searchDown) return fromTimeline(rettiwt, feed, startDate, maxPerFeed, ctx);

  const filter = new TweetFilter({
    fromUsers: [feed.username],
    startDate,
    ...(Array.isArray(feed.any_words) && feed.any_words.length > 0
      ? { optionalWords: feed.any_words }
      : {}),
  });

  const collected = [];
  let cursor;
  let page = 0;

  while (collected.length < maxPerFeed) {
    page++;
    let result;
    try {
      result = await searchWithRetry(rettiwt, filter, PAGE_SIZE, cursor);
    } catch (e) {
      const searchError = `Search failed on page ${page}: ${e.message}`;
      // Auth errors stay errors: the timeline needs the same session.
      if (page === 1 && collected.length === 0 && !isAuthError(e.message)) {
        ctx.searchDown = true;
        ctx.searchError = ctx.searchError || searchError;
        return fromTimeline(rettiwt, feed, startDate, maxPerFeed, ctx);
      }
      // Rate-limit, auth, or transient error — bail out with what we have
      return { tweets: collected, error: searchError, via: 'search' };
    }

    const batch = result.list || [];
    if (batch.length === 0) break;

    let oldestInBatch = null;
    for (const t of batch) {
      // Post-filter retweets if requested
      if (feed.exclude_retweets && t.retweetedTweet) continue;
      // Post-filter conversational replies if requested. Twitter sets replyTo
      // even on author self-threads, so we additionally require the text to
      // start with an @-mention — that distinguishes "reply to someone else"
      // from "next post in my own thread".
      if (feed.exclude_replies && t.replyTo && /^@\w/.test((t.fullText || '').trim())) continue;
      collected.push(t);
      const created = new Date(t.createdAt);
      if (oldestInBatch === null || created < oldestInBatch) oldestInBatch = created;
    }

    // If we paged into tweets older than the window, stop.
    if (oldestInBatch && oldestInBatch < startDate) break;

    const nextCursor = result.next?.value;
    if (!nextCursor || nextCursor === cursor) break;
    cursor = nextCursor;
  }

  // Search said "no tweets". On 11-21 X also answered the retired search with
  // an empty success now and then, which looks the same, so check the
  // account's timeline before believing it.
  if (collected.length === 0) {
    try {
      const fb = await timelineFallback(rettiwt, feed, startDate, Math.min(PAGE_SIZE, maxPerFeed));
      if (fb.tweets.length > 0) {
        ctx.searchDown = true;
        ctx.searchError = ctx.searchError ||
          `Search returned no tweets for @${feed.username}; its timeline has ${fb.tweets.length}`;
        return {
          tweets: fb.tweets,
          error: null,
          via: 'timeline',
          search_empty: true,
          timeline_pages: fb.pages,
          coverage_partial: fb.partial,
          covered_since: fb.coveredSince,
        };
      }
      return { tweets: [], error: null, via: 'search', timeline_checked: true };
    } catch (e) {
      // The check is extra: search's answer stands, the reason is kept.
      return {
        tweets: [],
        error: null,
        via: 'search',
        timeline_checked: false,
        timeline_check_error: String(e.message || e).slice(0, 200),
      };
    }
  }

  return { tweets: collected.slice(0, maxPerFeed), error: null, via: 'search' };
}

async function fromTimeline(rettiwt, feed, startDate, maxPerFeed, ctx) {
  const cap = Math.min(PAGE_SIZE, maxPerFeed);
  try {
    const fb = await timelineFallback(rettiwt, feed, startDate, cap);
    return {
      tweets: fb.tweets,
      error: null,
      via: 'timeline',
      timeline_pages: fb.pages,
      coverage_partial: fb.partial,
      covered_since: fb.coveredSince,
    };
  } catch (e) {
    // Keeps the search error first, so "Search failed" readers still match
    // and a 401/403 here still trips auth_failed below.
    return {
      tweets: [],
      error: `${ctx.searchError || 'Search skipped'}; timeline fallback failed: ${e.message}`,
      via: 'timeline',
    };
  }
}

// ─────────────────────────────────────────────────────────────────
// Markdown formatting (matches existing per-feed format)
// ─────────────────────────────────────────────────────────────────

function formatDate(iso) {
  if (!iso) return 'Unknown Date';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'Unknown Date';
  return d.toLocaleString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
    timeZone: 'UTC',
  }) + ' UTC';
}

function tweetMediaUrls(tweet) {
  const urls = new Set();
  const media = tweet.media || [];
  for (const m of media) {
    // Rettiwt's TweetMedia: { type, url, thumbnailUrl? }
    if (m.url) urls.add(m.url);
    if (m.thumbnailUrl) urls.add(m.thumbnailUrl);
  }
  return Array.from(urls);
}

function decodeEntities(s) {
  if (!s) return s;
  return String(s)
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&nbsp;/g, ' ');
}

function tweetWordCount(t) {
  const text = (t.fullText || '').trim();
  if (!text) return 0;
  return text.split(/\s+/).filter(Boolean).length;
}

// coverage (optional): { partial, coveredSince } from the timeline fallback.
// When the fallback stopped before the start of the window, say how far back
// it read, so the writer doesn't take "no tweets" or a short list as the
// whole window.
function buildFeedMarkdown(feed, tweets, coverage = null) {
  const desc = feed.source_description ? ` — ${feed.source_description}` : '';
  let md = `### ${feed.display_name}${desc} (Past Week)\n\n`;
  if (feed.header_note) {
    // Render as a blockquote so the LLM treats it as authoritative guidance
    // rather than article body text.
    const lines = String(feed.header_note).split('\n').map(l => `> ${l}`).join('\n');
    md += lines + '\n\n';
  }
  const partialSince = coverage && coverage.partial && coverage.coveredSince ? formatDate(coverage.coveredSince) : null;
  if (partialSince) {
    md += `> Coverage note: X search was unavailable, so these posts come from the account's timeline, which was only read back to ${partialSince}. Posts before then are not listed.\n\n`;
  }
  if (tweets.length === 0) {
    md += partialSince
      ? `No tweets from ${feed.display_name} since ${partialSince}.\n`
      : `No tweets from ${feed.display_name} in the past week.\n`;
    return md;
  }

  for (const t of tweets) {
    md += `#### Tweet from ${formatDate(t.createdAt)}\n\n`;
    md += `**Summary:** ${decodeEntities(t.fullText) || 'No text available'}\n\n`;

    const mediaUrls = tweetMediaUrls(t);
    if (mediaUrls.length > 0) {
      md += '**Media Images:**\n';
      mediaUrls.forEach((u, i) => {
        md += `- Image ${i + 1}: ${u}\n`;
      });
      md += '\n';
    }

    md += `**Source URL:** [View Tweet](https://x.com/${feed.username}/status/${t.id})\n\n---\n\n`;
  }
  return md;
}

// ─────────────────────────────────────────────────────────────────
// File-mode output helpers
// ─────────────────────────────────────────────────────────────────

function runDirFor(runId) {
  return `/opt/tcs/n8n/local_files/scraper-runs/${runId}`;
}

function writeFeedFile(runId, key, markdown) {
  const dir = runDirFor(runId);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const path = `${dir}/article-tweets-${key}.md`;
  writeFileSync(path, markdown, 'utf-8');
  return path;
}

// ─────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────

async function main() {
  const cfg = parseInput();
  const apiKey = loadApiKey();

  let rettiwt;
  try {
    rettiwt = new Rettiwt({ apiKey });
  } catch (e) {
    // apiKey malformed or rejected at construction time — burner is dead.
    const out = {
      run_id: cfg.run_id || null,
      since_days: cfg.since_days,
      feed_count: 0,
      auth_failed: true,
      suspected_auth_issue: false,
      error_count: 1,
      error: `Rettiwt init failed: ${e.message}`,
      feeds: [],
    };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  const out = await runFeeds(rettiwt, cfg);
  const results = out.feeds;
  const fileMode = !!cfg.run_id;
  const authFailed = out.auth_failed;

  // Upsert per-feed BAI entries into the run's shared index.json so
  // downstream parsing + the read_article tool both work uniformly
  // alongside Pattern A articles, events, and SD.
  if (fileMode && !authFailed) {
    const baiEntries = results
      .filter(r => r.key && !r.error)
      .map(r => ({
        index: `tweets-${r.key}`,
        title: `${r.tweet_count} tweet${r.tweet_count === 1 ? '' : 's'} (Past Week)`,
        url: '',
        date: '',
        thumbnail: '',
        images: [],
        source: `X — ${r.display_name}${r.source_description ? ' (' + r.source_description + ')' : ''}`,
        word_count: r.word_count || 0,
        character_count: 0,
        has_content: (r.tweet_count || 0) > 0,
        content_file: r.content_file || null,
        scrape_path: 'twitter',
        category: 'context',
        username: r.username,
        key: r.key,
        display_name: r.display_name,
        source_description: r.source_description || null,
        tweet_count: r.tweet_count,
      }));
    if (baiEntries.length > 0) {
      const b64 = Buffer.from(JSON.stringify(baiEntries)).toString('base64');
      try {
        const stdout = execFileSync('python3', [
          '/opt/tcs/scripts/append_to_index.py',
          String(cfg.run_id),
          b64,
        ], { encoding: 'utf-8' });
        out.index = JSON.parse(stdout);
      } catch (e) {
        out.index_error = `append_to_index failed: ${e.message}`;
      }
    }
  }

  process.stdout.write(JSON.stringify(out));
}

// Scrapes every feed and builds the stdout object (everything but the index
// upsert). Split out of main() so the tests can drive it with a fake client.
async function runFeeds(rettiwt, cfg) {
  const startDate = new Date(Date.now() - cfg.since_days * 24 * 60 * 60 * 1000);
  const fileMode = !!cfg.run_id;
  const ctx = { searchDown: false, searchError: null };

  const results = [];
  for (const feed of cfg.feeds) {
    if (!feed.username || !feed.key || !feed.display_name) {
      results.push({
        username: feed.username || null,
        key: feed.key || null,
        display_name: feed.display_name || null,
        tweet_count: 0,
        error: 'Missing username/key/display_name',
      });
      continue;
    }

    const scraped = await scrapeFeed(rettiwt, feed, startDate, cfg.max_per_feed, ctx);
    const { tweets, error } = scraped;
    const markdownField = `markdown_${feed.key}_tweets`;
    const markdown = buildFeedMarkdown(feed, tweets, {
      partial: scraped.coverage_partial === true,
      coveredSince: scraped.covered_since || null,
    });
    const wordCount = tweets.reduce((s, t) => s + tweetWordCount(t), 0);

    const entry = {
      username: feed.username,
      key: feed.key,
      display_name: feed.display_name,
      source_description: feed.source_description || null,
      tweet_count: tweets.length,
      word_count: wordCount,
      markdown_field: markdownField,
      error,
      // tcs-workflows#296, additive: how this feed was read.
      via: scraped.via || 'search',
    };
    if (entry.via === 'timeline') {
      entry.timeline_pages = scraped.timeline_pages ?? null;
      entry.coverage_partial = scraped.coverage_partial === true;
      entry.covered_since = scraped.covered_since || null;
    }
    if (scraped.search_empty) entry.search_empty = true;
    if ('timeline_checked' in scraped) entry.timeline_checked = scraped.timeline_checked;
    if (scraped.timeline_check_error) entry.timeline_check_error = scraped.timeline_check_error;

    // Always include markdown inline so n8n can use it directly without a
    // separate file read. In file mode we also write it to disk for the
    // agent's read tool.
    entry.markdown = markdown;
    if (fileMode) {
      entry.content_file = writeFeedFile(cfg.run_id, feed.key, markdown);
    }

    results.push(entry);
  }

  // Burned-account / soft-ban detection.
  // Hard signal: any feed got 401/403 -> apiKey is dead.
  // Soft signal: half or more of the feeds errored after retries.
  //
  // tcs-workflows#104 — this used to be `errCount >= 3`, an absolute count
  // written when the feed list was ~10 accounts, where "3 of 10 failed" is a
  // real signal. That assumption stopped holding as lists shrank (tcs-docs#14
  // trimmed NASA 10 -> 7) and broke outright for the single-feed NASAAdmin
  // pipe (tcs-docs#11): Daily Broadcast runs ONE feed, so `>= 3` was
  // unreachable and on 2026-09-26 this reported suspected_auth_issue:false
  // while 100% of feeds were failing. The whole point of the flag is to fire
  // in exactly that situation.
  //
  // A ratio holds at any list size: 1/1, 2/3 and 5/10 all trip; 1/10 does not.
  // The `errCount >= 1` floor is what stops a zero-feed run (0/0 -> ratio 0,
  // but be explicit) from being read as a failure.
  const authFailed = results.some(r =>
    typeof r.error === 'string' && /401|403|unauthorized|forbidden/i.test(r.error)
  );
  const errCount = results.filter(r => r.error).length;
  const errRatio = results.length > 0 ? errCount / results.length : 0;
  // OR, not replace: the ratio adds coverage for short lists, it must not
  // REMOVE coverage for long ones. 3-of-10 tripped the original rule and
  // still should; a pure ratio would have quietly desensitised big lists
  // while fixing small ones.
  const suspectedAuthIssue =
    !authFailed && (errCount >= 3 || (errCount >= 1 && errRatio >= 0.5));

  const out = {
    run_id: cfg.run_id || null,
    since_days: cfg.since_days,
    feed_count: results.length,
    auth_failed: authFailed,
    suspected_auth_issue: suspectedAuthIssue,
    error_count: errCount,
    // tcs-workflows#105: downstream reports "N/M feeds errored" rather than
    // asserting a cause it cannot know. Additive - nothing reads it yet.
    error_ratio: Number(errRatio.toFixed(3)),
    // tcs-workflows#296, additive: search failed in this run and the feeds
    // after it were read from timelines. The Healthcheck shows it as yellow.
    search_down: ctx.searchDown,
    search_error: ctx.searchError,
    timeline_feeds: results.filter(r => r.via === 'timeline').length,
    feeds: results,
    index: null,
  };
  return out;
}

export {
  compileAnyWords,
  timelineFallback,
  scrapeFeed,
  buildFeedMarkdown,
  runFeeds,
  PAGE_SIZE,
  TIMELINE_MAX_PAGES,
};

// Run only as a script (`node x_scraper.js <b64>`), not when the tests import it.
function isMainModule() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1]);
  } catch {
    return import.meta.url === pathToFileURL(process.argv[1]).href;
  }
}
if (isMainModule()) {
  main().catch((e) => {
    console.log(JSON.stringify({ error: `Top-level: ${e.message}`, stack: e.stack }));
    process.exit(1);
  });
}
