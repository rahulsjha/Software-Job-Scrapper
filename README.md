# Custom_scrapper — Wellfound Lead Scraper

> Automate job lead discovery on Wellfound. Filter by skills, compensation, and startup stage — then export enriched contacts to CSV, Airtable, or Notion.

---

## How It Works

The scraper runs through a five-stage pipeline:

```
Query Builder  →  Google SERP (SerpAPI)  →  Playwright Scrape  →  Skill Match Filter  →  Email Inference  →  CSV / Airtable / Notion Output
```

| Stage | What happens |
|---|---|
| **1. Query Builder** | Constructs a targeted Google search query from your filters |
| **2. URL Discovery** | Uses SerpAPI to find relevant Wellfound job listing URLs |
| **3. Playwright Scrape** | Auto-scrolls and scrapes job/company data from each URL |
| **4. Skill Match Filter** | Scores and filters leads by how well they match your skill set |
| **5. Email Inference** | Infers probable contact email patterns from company domains |
| **6. Output** | Writes results to CSV, and optionally syncs to Airtable or Notion |

---

## Setup

**1. Clone and create a virtual environment**

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install firefox
```

**2. Create a `.env` file**

```env
SERP_API_KEY=YOUR_SERPAPI_KEY
```

---

## Running the Scraper

### Basic example

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

### CLI flags reference

| Flag | Description |
|---|---|
| `--skills` | Comma-separated skill keywords to match against |
| `--min-comp-usd` | Minimum compensation (affects query text only) |
| `--comp-period` | Compensation period: `month`, `year`, etc. |
| `--max-stage` | Max funding stage, e.g. `seed`, `series_a`, `series_b` |
| `--posted` | Recency filter: `today`, `week`, etc. |
| `--max-urls` | Maximum number of Wellfound URLs to scrape |
| `--min-match` | Minimum skill match score (0.0–1.0) |
| `--out` | Output CSV file path |
| `--browser` | Playwright engine: `firefox` (default), `webkit`, `chromium` |
| `--wellfound-sleep` | Base sleep time between requests (seconds) |
| `--wellfound-jitter` | Random jitter added to sleep (seconds) |
| `--stop-on-restricted` | Stop early if Wellfound returns an access restriction |
| `--print-query` | Print the generated Google query without making any API call |
| `--urls-file` | Path to a file with hand-picked URLs (skips SerpAPI billing) |
| `--serp-only` | Skip Wellfound scraping; enrich via company website instead |
| `--web-enrich` | Fill `company_website` + `contact_name` from company's own site |
| `--airtable` | Enable Airtable sync (requires env vars) |

---

## Common Scenarios

### Wellfound is rate-limiting you

If you see *"Access is temporarily restricted"*, add a sleep delay and stop early:

```bash
python -m custom_scrapper \
  --wellfound-sleep 3 \
  --wellfound-jitter 2 \
  --stop-on-restricted \
  ...
```

### Use a different browser engine

```bash
python -m custom_scrapper --browser webkit ...
```

### Preview the query without spending API credits

```bash
python -m custom_scrapper --print-query
```

### Skip SerpAPI — use your own URL list

```bash
python -m custom_scrapper --urls-file urls.txt --out output/jobs.csv
```

### Wellfound blocks your network entirely

If no CAPTCHA is shown but pages won't load, fall back to enriching from company websites directly:

```bash
python -m custom_scrapper --serp-only --web-enrich --out output/jobs.csv
```

---

## Integrations

### Airtable

Add `--airtable` to the command and set these env vars:

```env
AIRTABLE_API_KEY=...
AIRTABLE_BASE_ID=...
AIRTABLE_TABLE=...
```

### Notion

Your Notion database must have these properties: `Name` (or the value of `NOTION_TITLE_PROP`), `URL`, `Company`, `Contact`, `Emails`, `Match Score`.

```env
NOTION_API_KEY=...
NOTION_DATABASE_ID=...
NOTION_TITLE_PROP=Name
```

---

## Output

Results are written to the path specified by `--out` (e.g. `output/jobs.csv`).

Each row includes:

- Job title and URL
- Company name and website
- Inferred contact name and email patterns
- Skill match score
- Funding stage and compensation details

---

## Notes

- `--min-comp-usd` and `--comp-period` only affect the generated Google search query. Wellfound does not expose a clean numeric filter, so these are baked into the query text as hints.
- Scraping is **best-effort** — Wellfound's markup can change at any time. The scraper falls back to heuristics when CSS selectors stop matching.
- Always run with a realistic `--wellfound-sleep` value (2–5 seconds) to avoid triggering access restrictions.

---

## Project Structure

```
custom_scrapper/
├── __main__.py         # Entry point & CLI argument parsing
├── query_builder.py    # Constructs the Google search query
├── serp.py             # SerpAPI URL discovery
├── scraper.py          # Playwright scrape + auto-scroll
├── filter.py           # Skill match scoring
├── email_infer.py      # Email pattern inference
├── output.py           # CSV / Airtable / Notion export
└── ...
output/
└── jobs.csv
.env
requirements.txt
```

---

## License

MIT
