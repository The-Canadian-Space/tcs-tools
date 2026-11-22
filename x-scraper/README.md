# X Scraper

Node.js scraper for X (Twitter) feeds, invoked over SSH from the V3 blog
workflows. Replaces the old paid `twitterapi.io` integration.

## Where it runs

**Not this repo.** This folder mirrors what actually runs on the OVH VPS at:

```
/opt/tcs/scripts/x-scraper/
├── x_scraper.js           # main entry point
├── package.json           # dependencies (rettiwt-api only)
├── package-lock.json
├── node_modules/
└── .rettiwt-auth          # chmod 600, holds the Rettiwt apiKey
```

Sync this repo copy after any prod edit.

## How it's called

The Daily Broadcast V3 workflow (and any V3 workflow that pulls X posts) hits
it via an n8n SSH node with:

```
node /opt/tcs/scripts/x-scraper/x_scraper.js <BASE64_JSON>
```

Input JSON shape:

```json
{
  "feeds": [
    {
      "username":     "elonmusk",
      "display_name": "Elon Musk",
      "key":          "elon_musk",
      "any_words":    ["Starship","Falcon"],
      "exclude_retweets": true
    }
  ],
  "since_days":   7,
  "max_per_feed": 200,
  "run_id":       "568"
}
```

Two output modes matching `article_scraper.py`'s pattern:

- **INLINE** (no `run_id`): full markdown bodies inline in stdout JSON
- **FILE** (with `run_id`): per-feed `tweets-<key>.md` under
  `/opt/tcs/n8n/local_files/scraper-runs/<run_id>/`, slim `index.json` on
  stdout

## Stack

- **[Rettiwt-API](https://github.com/Rishikant181/Rettiwt-API)** 7.2.0 —
  self-hosted Node library that speaks X's undocumented internal API using an
  authenticated session (no paid tier, no rate-limit quota to buy)

That's the entire dependency graph. `package.json` (the VPS copy, 7.2.0 since
tcs-workflows#278):

```json
{
  "name": "tcs-x-scraper",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "description": "TCS X (Twitter) scraper via Rettiwt-API. Replaces twitterapi.io.",
  "dependencies": {
    "rettiwt-api": "^7.2.0"
  }
}
```

## Auth

The `.rettiwt-auth` file (chmod 600, not committed) holds the Rettiwt session
apiKey. Regenerate via `rettiwt auth login` on the VPS if X invalidates the
session.

`rettiwt_probe.js` (here and on the VPS) checks every call the scraper needs,
read-only: account lookup, both timelines, search, a tweet, and the burner's
own account. Run it with `node /opt/tcs/scripts/x-scraper/rettiwt_probe.js`.
- **Everything fails, the own-account call too:** the session is dead.
- **Only search fails:** X search is down. The scraper falls back to timelines.
- **Some calls give 404:** X retired them. Look for a rettiwt-api release.

It never prints response headers, which can carry cookies.

## Search, and the timeline fallback (tcs-workflows#296)

Each feed is a search first: `(from:<user>) since:<start>`, plus
`(w1 OR w2 ...)` when the feed has `any_words`.

**Search reads only one page.** rettiwt 7.x gives the next-page cursor as a
string, and the loop reads `next.value`. So every feed has at most 20 tweets
(X's page size) and stops there, whatever `max_per_feed` says. This is left
as it is on purpose: changing it would change what every blog gets.

**X search leaves native retweets out by default.** On 2026-11-21, 0 of the
936 tweets kept in `scraper-runs/` were retweets, even for feeds with
`exclude_retweets: false`.

**On 2026-11-21 X retired the search operation rettiwt-api 7.2.0 calls.** Its
new web client uses a new one, which rettiwt can't call yet. Since then:
- **When search fails** with anything but an auth error, the feed is read from
  the account's own tweets-and-replies timeline (UserTweetsAndReplies). The
  run's later feeds skip search.
- **When search returns no tweets,** the timeline is checked before the empty
  answer is believed. X has also answered the retired search with an empty
  success.
- **The timeline gets search's rules:**
  - the same window;
  - `exclude_replies` and `any_words` (whole words and quoted phrases, any
    case, also inside `#tags` and `@names`);
  - no native retweets;
  - at most 20 tweets, newest first;
  - posts by other accounts in a reply thread are dropped.
- **It reads up to 20 pages a feed.** That was about a week of @elonmusk, the
  busiest feed. If the cap stops it before the start of the window, the
  feed's markdown says how far back it read (a "Coverage note" line).
- **Rate limits per 15 minutes** (2026-11-21): 500 for UserTweetsAndReplies,
  50 for UserTweets, 150 for account lookups, 50 for search.

**New output fields** (added; nothing is renamed or removed):

| Field | Where | Meaning |
|---|---|---|
| `via` | each feed | `search` or `timeline` |
| `timeline_pages`, `coverage_partial`, `covered_since` | each feed read from the timeline | pages read; whether the page cap stopped it early; how far back it got, when it did |
| `search_empty` | each feed | search said 0, and the timeline had tweets |
| `timeline_checked`, `timeline_check_error` | each feed | search said 0, and whether the check ran |
| `search_down`, `search_error`, `timeline_feeds` | the run | search failed in this run, the first error, and how many feeds used the timeline |

The Healthcheck's X row shows yellow while `search_down` is true. When
rettiwt-api can search again, nothing needs changing: the scraper uses search
first on every run.

## Tests

`x_scraper.test.mjs` drives the scraper with a fake X client (no network).
It needs rettiwt-api installed next to it, because `x_scraper.js` imports it.
`mutants296.cjs` breaks one rule at a time and checks that the tests catch it.

```
node --test x_scraper.test.mjs      # 20 tests
node mutants296.cjs                 # 21 mutants
```

On the VPS, run them in a scratch copy that links `node_modules` to
`/opt/tcs/scripts/x-scraper/node_modules`.

## Editing

Edit prod directly on the VPS, test with a manual run, then `cat` back into
this repo. `package.json` diffs should be treated the same way — bump on the
VPS, sync back.
