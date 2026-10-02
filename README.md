# seo-aeo-site-audit

Status: Demo, version 0.1. Tested on local fixtures (13 pytest tests, no network). One live run on https://example.com on 2026-10-02 completed: score 35/100, grade F (sample in `samples/live-example-com/`).

A small Python tool for step 1 of a lead-generation flow: a prospect enters a URL, the tool checks the page, scores it, writes a report a business owner can read, and produces a JSON payload that a CRM (GoHighLevel) can use to start follow-ups.

```
URL  ->  fetch page + robots.txt + sitemap.xml + llms.txt  ->  23 checks  ->  score 0-100, grade A-F
     ->  report.html (for the prospect)  +  audit.json (full data)  +  ghl_payload.json (for the CRM)
```

## What it checks

23 checks in 4 sections. Every check has an id, section, plain-English label, status (`pass`, `warn`, `fail`, `not_checked`), weight (3 high, 2 medium, 1 low), what was found, and a one-sentence fix.

| Section | Checks |
|---|---|
| SEO basics | HTTPS, title length 30-60, meta description 70-160, exactly one H1, canonical tag, no `noindex`, image alt text ratio, 300+ words, Open Graph title and image |
| Technical | robots.txt (and whether it blocks everything), sitemap.xml or a sitemap listed in robots.txt, mobile viewport tag, HTML size, response time of the main request |
| Local SEO | tap-to-call phone link (or phone in text), address in LocalBusiness/Organization schema (or in text), Google Maps embed or link (bonus, only a warning if missing) |
| AEO (AI search readiness) | JSON-LD present and valid, schema types found (listed), FAQPage/QAPage schema, question-style H2/H3 headings, Organization/LocalBusiness with name and sameAs, llms.txt (warning only: it is an emerging convention, not a confirmed ranking factor) |

If something cannot be checked (offline mode, a network error, no images on the page), the status is `not_checked` with the reason. The tool never fills in a guess.

## How scoring works

- Good (`pass`) earns the full weight, Needs work (`warn`) half, Problem (`fail`) zero.
- `not_checked` is left out of the denominator, so it neither helps nor hurts.
- Section score = points earned / points possible in that section x 100. Overall score uses all checks the same way (it is not an average of the four sections).
- Grades: A 90+, B 80+, C 70+, D 60+, F below 60.
- Top 5 fixes are ordered by points lost (weight x missing credit), so high-weight problems come first.

Note: an offline run skips 5 network checks, so its score is computed on fewer checks than a live run of the same page.

## How to run

```bash
pip install -r requirements.txt

# live
python site_audit.py https://example.com --out output/

# offline, on a saved HTML file
python site_audit.py --html tests/fixtures/weak_page.html --base-url https://joes-roofing.test/ --out output/

# options
#   --email owner@business.com   adds the prospect email to the GHL payload
#   --skip-local                 marks local SEO checks as not applicable (non-local businesses)
#   --timeout 15                 seconds per request

python -m pytest -q
```

Live mode uses a browser-like User-Agent and a 15 second timeout. If the page itself returns an HTTP error (for example 403 from a firewall), the tool stops with exit code 2 instead of scoring the error page.

## Outputs

| File | What it is |
|---|---|
| `report.html` | Self-contained page (no external CSS or JS): site, date, overall score and grade, section scores, top 5 fixes, full table of checks. Em dashes in quoted page text are shown as hyphens; `audit.json` keeps the raw text. |
| `audit.json` | Everything: checks, scores, counts, extracted facts, robots/sitemap/llms responses. |
| `ghl_payload.json` | Example payload for GoHighLevel (see below). |

Samples generated from the fixtures and the live run are in `samples/`:

| Sample | Mode | Score |
|---|---|---|
| `samples/weak-page/` | offline | 17, F |
| `samples/local-faq-page/` | offline | 85, B |
| `samples/good-page/` | offline | 99, A |
| `samples/live-example-com/` | live | 35, F |

The fixture businesses (Joe's Roofing, Rapid Rooter Plumbing, Northwind Bookkeeping) are made up for testing.

## GoHighLevel integration

`ghl_payload.json` contains:

- `contact`: `website` (and `email` if passed with `--email`)
- `customFields`: `audit_score`, `audit_grade`, `seo_score`, `technical_score`, `local_score`, `aeo_score`, `top_issue_1..3`, `audit_date`, `audit_mode`
- `tags`: `website-audit-done`, `audit-grade-x`, and `audit-score-below-60` when the score is under 60
- `api_v2_contacts_upsert_body`: the same data shaped like the body of the GHL API v2 `POST /contacts/upsert` call (custom fields as `{key, field_value}`, `locationId` placeholder), for setups that call the API directly instead of using a webhook

A GHL workflow could use it like this:

1. **Trigger: Inbound Webhook.** The audit service POSTs the payload to the workflow's webhook URL. GHL accepts any JSON body there and lets you map its fields.
2. **Create/Update Contact.** Match on email (from the opt-in form) and map `customFields.*` to custom fields created beforehand in the sub-account (`audit_score` as a number, `audit_date` as a date, the rest as text). Add the tags from `tags`.
3. **If/Else on `audit_score`.** For example, below 60: urgent sequence ("your site has 3 problems that cost you calls", using `top_issue_1..3` as merge fields) and an internal notification to sales. 60 or above: a softer nurture sequence.
4. **Follow-up.** Email/SMS steps, a task for the sales rep, or a booking link for a call.

This payload format has not been tested against a live GoHighLevel account. Field keys must match the custom fields you create in your sub-account.

## Limits

- One page only. No crawl of other pages.
- No JavaScript rendering. Sites that build their content in the browser (some Wix, React or Webflow setups) may look emptier than they are.
- Rules are heuristics, not a full SEO crawler. The phone and street address patterns are US-centric.
- Response time is one measurement from the machine running the audit. PageSpeed / Core Web Vitals are not included.
- Local SEO checks assume a local business; use `--skip-local` otherwise.
- Organization detection for the identity check reads JSON-LD only (microdata types are listed, not scored for this check).

## Next steps

- Google PageSpeed Insights API for real speed and Core Web Vitals scores.
- Multi-page crawl (home, services, contact, location pages) with a page-level summary.
- PDF export of the report.
- An AI-written summary paragraph for the prospect, generated from the same check results and guarded by them: it may only mention issues present in `audit.json`.
- Web form + small API endpoint that runs the audit and POSTs the payload to the GHL webhook.

## License

MIT, see `LICENSE`.
