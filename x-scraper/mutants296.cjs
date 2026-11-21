// tcs-workflows#296: mutation check for x_scraper.test.mjs. Each mutant breaks one rule of the timeline fallback;
// the tests must fail for every one. Run in a folder holding x_scraper.js, x_scraper.test.mjs, package.json and a
// node_modules with rettiwt-api:  node mutants296.cjs
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const SRC = fs.readFileSync('x_scraper.js', 'utf8');
const MUTANTS = [
  ['keeps native retweets', "      if (t.retweetedTweet || /^RT @\\w{1,15}:/.test(t.fullText || '')) continue;\n", ''],
  ['keeps other authors (reply parents)', "      if (!t.tweetBy || String(t.tweetBy.id) !== String(user.id)) continue;\n", ''],
  ['no date window', '      if (created < startDate) continue;\n', ''],
  ['no end-of-window stop', 'batch.length === 0 || ownInWindow === 0 || !next', 'batch.length === 0 || !next'],
  ['later feeds still search', '  if (ctx.searchDown) return fromTimeline(rettiwt, feed, startDate, maxPerFeed, ctx);\n', ''],
  ['falls back on auth errors', ' && !isAuthError(e.message)) {', ') {'],
  ['no newest-first sort', '  collected.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));\n', ''],
  ['never partial', '  const partial = !reachedEnd && collected.length < cap;', '  const partial = false;'],
  ['covered_since from page 1', '    if (pageOldest) lastPageOldest = pageOldest;', '    if (pageOldest && !lastPageOldest) lastPageOldest = pageOldest;'],
  ['no any_words filter', "      if (words && !words.test(t.fullText || '')) continue;\n", ''],
  ['no exclude_replies in fallback', "      if (feed.exclude_replies && t.replyTo && /^@\\w/.test((t.fullText || '').trim())) continue;\n      if (words", '      if (words'],
  ['cap is max_per_feed', '  const cap = Math.min(PAGE_SIZE, maxPerFeed);', '  const cap = maxPerFeed;'],
  ['no word boundary', '(?:^|[^\\\\p{L}\\\\p{N}_])(?:${parts.join', '(?:${parts.join'],
  ['phrases need exact spaces', ".replace(/\\s+/g, '\\\\s+'));", '));'],
  ['runs main() on import', 'if (isMainModule()) {', 'if (true) {'],
  ['no coverage note', "    md += `> Coverage note:", "    md += ``; void `"],
  ['search_down never set', '        ctx.searchDown = true;\n        ctx.searchError = ctx.searchError || searchError;\n', '        ctx.searchError = ctx.searchError || searchError;\n'],
  ['no empty-search check', '  if (collected.length === 0) {\n    try {\n      const fb = await timelineFallback', '  if (false) {\n    try {\n      const fb = await timelineFallback'],
  ['empty search does not mark search down', '        ctx.searchDown = true;\n        ctx.searchError = ctx.searchError ||\n          `Search returned', '        ctx.searchError = ctx.searchError ||\n          `Search returned'],
  ['no RT-text check', "      if (t.retweetedTweet || /^RT @\\w{1,15}:/.test(t.fullText || '')) continue;", '      if (t.retweetedTweet) continue;'],
  ['page cap ignored', 'while (collected.length < cap && pages < TIMELINE_MAX_PAGES) {', 'while (collected.length < cap && pages < TIMELINE_MAX_PAGES + 3) {'],
];
const dir = fs.mkdtempSync(path.join(process.cwd(), 'mut-'));
for (const f of ['x_scraper.test.mjs', 'package.json']) fs.copyFileSync(f, path.join(dir, f));
fs.symlinkSync(fs.realpathSync('node_modules'), path.join(dir, 'node_modules'));
let caught = 0;
for (const [name, from, to] of MUTANTS) {
  const n = SRC.split(from).length - 1;
  if (n !== 1) { console.log(`SKIP (pattern found ${n}x): ${name}`); continue; }
  fs.writeFileSync(path.join(dir, 'x_scraper.js'), SRC.replace(from, to));
  const r = spawnSync(process.execPath, ['--test', 'x_scraper.test.mjs'], { cwd: dir, encoding: 'utf8', timeout: 120000 });
  const fails = (r.stdout.match(/^# fail (\d+)/m) || [])[1];
  const ok = r.status !== 0;
  if (ok) caught++;
  console.log(`${ok ? 'caught' : 'MISSED'}: ${name} (exit ${r.status}, failing tests ${fails ?? '?'})`);
}
fs.rmSync(dir, { recursive: true, force: true });
console.log(`mutants caught: ${caught}/${MUTANTS.length}`);
