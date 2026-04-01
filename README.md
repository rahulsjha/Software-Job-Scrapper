# custom_scrapper — Wellfound lead scraper

Pipeline:
1) Query builder
2) Google SERP (SerpAPI) URL discovery
3) Playwright scrape (auto-scroll)
4) Skill match filter
5) Email pattern inference
6) CSV output

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Create a `.env` file:

```env
SERP_API_KEY=YOUR_SERPAPI_KEY
```

## Run

Example:

```bash
python -m custom_scrapper \
  --skills "nodejs,django,fastapi,mongodb,postgresql,aws,docker,kubernetes,genai,llm" \
  --min-comp-usd 20000 \
  --comp-period month \
  --max-stage series_b \
  --posted today \
  --max-urls 20 \
  --min-match 0.35 \
  --out output/jobs.csv
```

Print the generated Google query (no API call):

```bash
python -m custom_scrapper --print-query
```

Run with a hand-picked URL list (skips SerpAPI billing):

```bash
python -m custom_scrapper --urls-file urls.txt --out output/jobs.csv
```

## Optional: Airtable

Set env vars and add `--airtable`:

```env
AIRTABLE_API_KEY=...
AIRTABLE_BASE_ID=...
AIRTABLE_TABLE=...
```

## Optional: Notion

Your Notion database must have properties named: `Name` (or set `NOTION_TITLE_PROP`), `URL`, `Company`, `Contact`, `Emails`, `Match Score`.

```env
NOTION_API_KEY=...
NOTION_DATABASE_ID=...
NOTION_TITLE_PROP=Name
```

Notes:
- `--min-comp-usd` and `--comp-period` only affect the Google query text (since Wellfound/Google don’t provide a clean numeric filter).
- Scraping is “best effort” because Wellfound markup can change; the scraper falls back to heuristics when selectors don’t match.
# Job-Scrapper
