# Osprey Travels — Duty Slip Generator

Flask app for generating, managing, signing, and exporting vehicle duty slips.

## Deployment

This project uses **git-based deployments** via Vercel:

- Push to `main` → auto-deploys to **production** (ospreytravels.in).
- Push any other branch / open a PR → Vercel builds a **preview** deployment.

No manual `vercel deploy` is needed — every `git push` to `main` ships.

### Environment variables (Vercel → Project → Settings → Environment Variables)

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Flask session signing key |
| `ADMIN_PASSWORD` | Admin login password (synced into the DB on startup) |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Production database (libSQL/Turso) |
| `GOOGLE_MAPS_API_KEY` | Maps + Places for the route Map Builder |

## Local development

```bash
pip install -r requirements.txt
python app.py        # serves on http://127.0.0.1:5050
```

Locally (no Turso env) the app uses a local SQLite `invoices.db`, and the admin
login falls back to `admin` / `admin` for convenience.
