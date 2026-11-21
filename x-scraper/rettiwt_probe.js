#!/usr/bin/env node
/**
 * Rettiwt burner-account diagnostic — v3 (tcs-workflows#296, 2026-11-21).
 *
 * Tries each call the X scraper depends on and prints one line per call:
 * ok + a small summary, or the error chain (name, message, code, HTTP status,
 * method, URL, the first 300 characters of the body). Read-only.
 *
 * v3: also probes search (what x_scraper.js tries first) and
 * UserTweetsAndReplies (its fallback since #296); looks up the NASAAdmin id
 * instead of hardcoding it (the old id had become another account); never
 * prints response headers, which can carry cookies.
 *
 * How to read it:
 *   - every call FAILS, the own-account one too      -> the burner session (auth)
 *   - only search fails, the timelines work          -> X search is down; the
 *     scraper falls back to timelines (#296), the Healthcheck shows yellow
 *   - only some calls fail with 404                  -> X retired those
 *     operations: check `npm view rettiwt-api version` for a fix
 */
import { Rettiwt, TweetFilter } from 'rettiwt-api';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const AUTH_PATH = join(__dirname, '.rettiwt-auth');

const ACCOUNT = 'NASAAdmin';
const TWEET_ID = '2079372476409082042';

function walkError(e) {
  const chain = [];
  let cur = e;
  let depth = 0;
  while (cur && depth < 6) {
    const node = { name: cur.name, message: cur.message, code: cur.code };
    if (cur.response) {
      node.response = {
        status: cur.response.status,
        statusText: cur.response.statusText,
        data: (() => {
          try {
            if (typeof cur.response.data === 'string') return cur.response.data.slice(0, 300);
            return JSON.stringify(cur.response.data).slice(0, 300);
          } catch { return '<unserializable>'; }
        })(),
      };
    }
    if (cur.config) node.request = { method: cur.config.method, url: String(cur.config.url || '').slice(0, 200) };
    if (cur.request && !cur.response) node.transport = 'request-made-no-response';
    chain.push(node);
    cur = cur.cause;
    depth++;
  }
  return chain;
}

function summarize(result) {
  if (result === null || result === undefined) return { type: 'nullish' };
  if (Array.isArray(result)) return { type: 'array', length: result.length };
  if (result.list) return { type: 'CursoredData', list_length: result.list.length, has_next: Boolean(result.next) };
  if (result.id && result.userName) return { type: 'User', id: result.id, userName: result.userName };
  if (result.id && result.fullText !== undefined) return { type: 'Tweet', id: result.id };
  return { type: 'other', keys: Object.keys(result).slice(0, 10) };
}

async function probe(label, op) {
  const start = Date.now();
  try {
    const result = await op();
    return { label, ok: true, ms: Date.now() - start, summary: summarize(result), result };
  } catch (e) {
    return { label, ok: false, ms: Date.now() - start, errorChain: walkError(e) };
  }
}

async function main() {
  const apiKey = readFileSync(AUTH_PATH, 'utf-8').trim();
  const rettiwt = new Rettiwt({ apiKey });
  const results = [];

  const user = await probe(`user.details("${ACCOUNT}")`, () => rettiwt.user.details(ACCOUNT));
  results.push(user);
  const id = user.ok && user.result ? user.result.id : null;
  if (id) {
    results.push(await probe('user.timeline(id) [UserTweets]', () => rettiwt.user.timeline(id, 10)));
    results.push(await probe('user.replies(id) [UserTweetsAndReplies, the #296 fallback]', () => rettiwt.user.replies(id, 10)));
  }
  const filter = new TweetFilter({ fromUsers: ['NASA'], startDate: new Date(Date.now() - 3 * 86400 * 1000) });
  results.push(await probe('tweet.search(from:NASA, 3 days) [what x_scraper.js tries first]', () => rettiwt.tweet.search(filter, 5)));
  results.push(await probe(`tweet.details(${TWEET_ID})`, () => rettiwt.tweet.details(TWEET_ID)));
  results.push(await probe('user.details() OWN', () => rettiwt.user.details()));

  for (const r of results) delete r.result;
  console.log(JSON.stringify({ probed_at: new Date().toISOString(), results }, null, 2));
}

main().catch((e) => {
  console.log(JSON.stringify({ fatal: e.message, chain: walkError(e) }, null, 2));
  process.exit(1);
});
