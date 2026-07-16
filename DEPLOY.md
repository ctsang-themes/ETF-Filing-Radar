# Deploying the ETF Filing Radar API

## Quick local test (do this before deploying)

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name your@email.com"   # SEC requires a real contact
uvicorn app.api:app --reload --port 8000
```

Then in another terminal:

```bash
curl "http://localhost:8000/health"
curl "http://localhost:8000/scrape?start=2026-07-01&end=2026-07-10"
```

The first `/scrape` call for a new range will actually hit EDGAR and
parse documents -- expect it to take anywhere from a few seconds to a
couple minutes depending on how many filings fall in the window and how
many documents `MAX_DOCS_PER_REQUEST` lets it fetch. Repeat calls for
the same range return from cache instantly.

If `/scrape` comes back empty for a range you know has filings, check
that `SEC_USER_AGENT` is set to a real contact string -- SEC rejects
generic or missing User-Agents.

---

## Deploying to Render (free tier)

1. Push this repo to GitHub.
2. Sign in to render.com.
3. **New -> Web Service -> Connect repository.**
4. Render reads `render.yaml` automatically. Confirm:
   - Runtime: Docker
   - Plan: Free
   - Health check: `/health`
5. **Environment -> Add Secret:**
   - Key: `SEC_USER_AGENT`
   - Value: `Your Name your@email.com`
6. Click **Create Web Service**. First build takes ~3 minutes.

You'll get a URL like `https://etf-filing-radar-api.onrender.com`.
Free tier sleeps after 15 min idle -- first request after a gap takes
~30s to wake up.

---

## Pointing the frontend at it

Open the tracker HTML, paste the Render URL into the "API URL" field at
the top, and click "Fetch live filings." It's saved to localStorage so
you only have to do this once per browser.

---

## Known limitations, honestly

- **Ticker isn't extracted yet.** It's not reliably present on the
  facing sheet -- it usually shows up later in the prospectus body or
  in a separate 8-A12B filing. The frontend will show blank tickers
  until a second parsing pass is added for that.
- **Checkbox detection is heuristic.** EDGAR renders the Rule 485
  facing sheet checkboxes inconsistently across filers and years
  (Unicode ballot boxes, bracketed X's, image-mapped glyphs). The
  parser looks for a few common patterns and returns `unresolved`
  rather than guessing when it can't tell -- treat any `unresolved`
  basis as a manual-check item, not a silent default.
- **`MAX_DOCS_PER_REQUEST` caps how many filings get their documents
  fetched per call** (default 60), to keep request time reasonable on
  Render's free tier. Filings beyond that cap in a busy date range
  won't be included -- narrow the date range or raise the cap (and
  your patience for wait times) if you need everything.
- **Full-index quarterly file is the discovery mechanism**, not EDGAR
  full-text search -- this means results are exact for the form types
  requested, but it fetches one file per quarter touched, so multi-year
  ranges will be slower on the first call.
