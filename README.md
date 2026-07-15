<p align="center">
  <img src="https://capsule-render.vercel.app/api?type=rect&height=200&color=0:03120F,45:10B981,100:22C55E&text=%20TCS%20Tools%20&fontColor=ffffff&fontAlignY=42&fontSize=30&textBg=true&desc=Utility%20tools%20for%20The%20Canadian%20Space&descAlignY=68&descSize=17" />
</p>

<p align="center">
  <a href="https://thecanadian.space"><img src="https://img.shields.io/badge/The%20Canadian%20Space-space%20blog-0EA5E9?style=for-the-badge&logo=rocket&logoColor=white" alt="TCS" /></a>
  <a href="https://github.com/The-Canadian-Space/tcs-tools/issues/new"><img src="https://img.shields.io/badge/Report%20a%20bug-red?style=for-the-badge&logo=github&logoColor=white" alt="Report a bug" /></a>
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
</p>

> **"Small tools, fewer headaches, better automation."**

## What's Here

A collection of utility tools built to support The Canadian Space operations. These are production-ready helpers that run as part of our publishing and research pipeline.

## Current Status

**Stable and in use**. Tools here have real responsibilities in the TCS ecosystem. Each tool is documented in its own directory.

## Tools

### article-scraper

**Status**: Production  
**Language**: Python  
**Purpose**: Extracts article content from news sources for analysis and processing

The article-scraper reads web pages (news sites, RSS feeds, etc.) and extracts structured article data: title, body, authors, publication date, featured image. Built to handle real-world messy HTML and common edge cases.

**What it extracts**:
- Full article text with formatting preserved
- Publication date and author information
- Featured/header image URL
- Source URL and metadata
- Structured output as JSON

**Built for these sources** (reference implementation):
- Crawl4AI-compatible sources (JavaScript-heavy sites, Cloudflare-protected)
- Standard HTTP + RSS feeds
- Custom extraction patterns for publisher quirks

**Recent improvements** (as of 2026-06-20):
- Hardened HTML parsing (unclosed tags, malformed content)
- Image URL rewriting for CDN compatibility
- Better handling of content wrapped in multiple div layers
- Improved filtering for non-article content (ads, navigation, etc.)

**Usage example**:

```python
from article_scraper import ArticleScraper

scraper = ArticleScraper()
article = scraper.fetch("https://example.com/article")

# Returns structured article object with:
# - title, body, author, published_date, featured_image, source_url
```

**Where it's used**: Part of the n8n publishing pipeline to gather source material for blog posts

### agency-search

**Status**: In development  
**Language**: Python  
**Purpose**: Search and analyse aerospace agencies and organisations

Query aerospace agencies by name, country, capabilities, and other attributes. Useful for researching actors in the space industry.

**Features** (planned):
- Name and country-based search
- Capability filtering
- Organisation metadata
- JSON data export
- API-ready structure

## Philosophy

> "The right tool for the right job at the right time."

We believe in:
- ✅ **Single purpose**: Do one thing well, no bloat
- ✅ **Reusable**: Write once, use everywhere (scripts, n8n, CI/CD)
- ✅ **Documented**: README with examples in every tool directory
- ✅ **Tested**: Works as advertised, fails gracefully
- ✅ **Simple before clever**: But clever when the job demands it

## Structure

```
tcs-tools/
├── article-scraper/       # Extract article content from web sources
│   ├── README.md         # Detailed usage and examples
│   ├── scraper.py        # Main implementation
│   └── requirements.txt   # Python dependencies
├── agency-search/         # Search aerospace organisations
│   ├── README.md
│   └── search.py
└── README.md             # This file
```

## Contributing a New Tool

Got something that would help? Add it:

1. **Create a directory** with a clear, descriptive name (e.g., `cost-calculator`)
2. **Write a README** in that directory (purpose, usage examples, dependencies)
3. **Keep it focused**: One clear job, no feature creep
4. **Document it well**: Future you will thank you
5. **Test it**: Make sure it actually works before committing

## 🐛 Found a bug?

- **[Open an issue](https://github.com/The-Canadian-Space/tcs-tools/issues/new)** — reproducible bugs, feature requests, or ideas
- If you're seeing it in production (a live TCS blog post using this tool), mention which workflow triggered it

## 🔗 Related

- **Main site:** [thecanadian.space](https://thecanadian.space)
- **Public wiki:** [wiki.thecanadian.space](https://wiki.thecanadian.space)
- **[`tcs-scripts`](https://github.com/The-Canadian-Space/tcs-scripts)** — the n8n code nodes that call these tools
- **[`tcs-workflows`](https://github.com/The-Canadian-Space/tcs-workflows)** *(private)* — backups of the n8n workflows themselves

## 🧡 Support

TCS is a personal project + portfolio piece. If you like what we're building, **Patreon** is where the running project log lives — behind-the-scenes notes, dev diaries, cost breakdowns.

[![Support on Patreon](https://raw.githubusercontent.com/Godimas101/personal-projects/main/patreon/images/buttons/patreon-medium.png)](https://patreon.com/Godimas101)

---

*"In tools we trust, in automation we must."* 🔧