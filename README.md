# slipdesk

Back office for a vehicle-hire operator. Duty slips go in, customer-signed GST invoices come out.

A driver finishes a trip and the slip gets recorded — vehicle, route, opening and closing km,
times, tolls. The customer signs off on a batch of slips through a link, without an account.
Signed slips roll up into a monthly GST invoice. That whole path is one app.

Built for and running in production at [ospreytravels.in](https://ospreytravels.in).

## What it does

**Duty slips** — Record trips with vehicle, driver, route, km, times, and remarks. Bulk import
a month at once. Save reusable templates for routes you run repeatedly, and set up recurring
trips that generate themselves. Recompute km and totals across a selection with a preview
before applying. Soft-delete with restore.

**Customer e-signature** — Issue a signing link for one slip or a whole batch. The customer
opens `/sign/<token>`, reviews, and signs in the browser — no account, no app. Links expire,
and can be reissued, extended, or revoked. Signer IP and user-agent are recorded with the
signature.

**GST invoicing** — Pull a customer's slips for a month and prefill an editable invoice.
Per-day or fixed-vehicle line items, CGST + SGST or IGST toggle, tolls, and an invoice number
assigned per financial year on first save. Totals are recomputed server-side on save — the
browser's arithmetic is never trusted.

**AI slip parsing** — Paste a slip as text, or upload a photo of a paper one, and it fills the
form. `POST /ai/parse_slip` and `/ai/parse_slip_image`. Works with Anthropic or OpenAI,
whichever key you provide.

**Exports** — PDF slips via ReportLab, Excel via openpyxl, and bulk downloads as a zip.

**Admin** — Customers (with merge, for when the same client gets entered twice), drivers with
per-driver reports, vehicles, and a slip management view with month counts.

## Stack

Flask · SQLite locally, Turso/libSQL in production · ReportLab · openpyxl · pypdf · Pillow ·
Google Maps Places · Vercel

No ORM and no build step. It is a Flask app with SQL in it, which is the right size for this.

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env     # optional — it runs without any of it
python app.py            # http://127.0.0.1:5050
```

With no Turso credentials it creates a local SQLite `invoices.db` and the admin login falls
back to `admin` / `admin`. That fallback is local-only convenience — in production
`ADMIN_PASSWORD` is synced into the database on startup.

## Configuration

Everything is optional except in production. See [`.env.example`](.env.example).

| Variable | |
|---|---|
| `SECRET_KEY` | Flask session signing. A random one is generated per process if unset, which logs everyone out on restart. |
| `ADMIN_PASSWORD` | Admin password, synced into the DB on startup |
| `TURSO_DATABASE_URL` · `TURSO_AUTH_TOKEN` | Production database. `STORAGE_URL` / `STORAGE_AUTH_TOKEN` are accepted as aliases. |
| `GOOGLE_MAPS_API_KEY` | Maps and Places for the route builder |
| `ANTHROPIC_API_KEY` | Enables AI slip parsing via `claude-3-5-sonnet-latest` |
| `OPENAI_API_KEY` | Enables AI slip parsing via `gpt-4o` |
| `AI_PROVIDER` | `anthropic` or `openai`. Optional — otherwise whichever key is present wins, Anthropic first. |
| `AI_MODEL` | Override the default model for the chosen provider |
| `FLASK_DEBUG` | `1` to enable debug locally |

AI parsing is a feature flag by omission: provide no key and the endpoints simply stay off.

## Deploying

Git-based deployment through Vercel. Push to `main` and it ships to production; any other
branch or PR gets a preview build. There is no manual `vercel deploy` step.

Set the environment variables under Vercel → Project → Settings → Environment Variables.

## Data

Ten tables, created and migrated on startup — `invoices` (the slips), `monthly_invoices`,
`signature_requests`, `customers`, `drivers`, `vehicles`, `slip_templates`, `recurring_trips`,
`users`, `config`. Schema changes are applied as guarded `ALTER TABLE`s at boot, so deploying
is the migration.

## A note on what is public

The code is open; the operator's data is not. There are no customer records, rates, or
credentials in this repository — everything sensitive comes from the environment. The one
exception is historical: an early commit included a development `invoices.db` containing four
driver first names and a since-rotated login. It carries no customer or invoice rows.

## License

MIT — see [LICENSE](LICENSE).
