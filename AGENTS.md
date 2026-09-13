# AGENTS.md — tcs-tools

**Article scraper + agency search utilities shared across TCS blog workflows.**

> **First-time context:** start with the top-level [AGENTS.md](../AGENTS.md) in the working directory. This file is repo-specific.

## What lives here

- **`article_scraper/`** — The canonical article-scraper implementation. Called via SSH from n8n workflows. Extracts article text from source URLs, handles paywall detection, JavaScript fallbacks, and cleanup.
- **`agency_search/`** — Agency + entity lookup helpers.

Ground truth for scraper behavior is here, not in n8n Code nodes. Code nodes call these via SSH.

## Working principles

TCS-wide practices (test-before-commit, docs-in-same-PR) live in the parent — [`../AGENTS.md`](../AGENTS.md). Repo-specific below:

1. **Read the current implementation before editing.** Scrapers have accumulated edge cases (Cloudflare-blocked sources like Space Daily, dynamic-JS content requiring browserless, per-source hostname routing). Don't remove a case just because it looks odd — read the commit that added it.
2. **Preserve return shape.** The scraper's output structure is consumed by n8n Code nodes. Adding fields is safe; renaming/removing them breaks callers silently.
3. **Test with a live URL.** `python article_scraper/main.py <url>` from your local machine works for HTTP paths; SSH-only paths need to run on the VPS.
4. **Update the audit results.** Every article-scraper change should be validated against the source-coverage matrix — the overnight audit at `[project_tcs_scraper_overnight_fixes]` memory documents the source-quality tiers.

## Common failure modes

- **Cloudflare cookie-wall / SPA JS** — first choice: CRW self-hosted at `http://127.0.0.1:3010/v1/scrape` (deployed 2026-08-08, see `tcs-docs/docs/scrapers.md`). Handles Space Daily, SpaceX, Starlink cleanly. Historically Browserless was used for these — the migration to CRW is in [tcs-workflows#38](https://github.com/The-Canadian-Space/tcs-workflows/issues/38).
- **Cloudflare Turnstile / Vercel Security Checkpoint** — no self-hosted tool bypasses these in 2026. Rocket Lab (Cloudflare UAM) → Firecrawl (live as of 2026-08-09; swapped from ScrapingBee same day after Firecrawl proved ~5× faster on the same URLs); Blue Origin (Vercel) → ScraperAPI (staying put).
- **Paywall detection** — heuristic-based; false positives happen. Log them; don't crash.

## How to verify (before flagging In QA or closing)

TCS-wide verification (run-locally, cost-check, reversibility) lives in the parent's Six Working Principles — [`../AGENTS.md`](../AGENTS.md). Scraper-specific below:

- **Run against a real URL from each tier** in the source-coverage matrix before closing — a change that fixes Space Daily can break Blue Origin silently. Hit at least: one plain-HTTP source, one Cloudflare-UAM source, one Vercel-Checkpoint source, one paywalled source.
- **Check the audit output.** If the overnight audit runs after your change, read its diff — silent regressions surface there before users see them.

## Related docs

- [Docs site](https://docs.thecanadian.space/)
- Top-level [AGENTS.md](../AGENTS.md) for env access
