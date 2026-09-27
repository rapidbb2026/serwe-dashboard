# SERWE Field Monitor

Live dashboard for the SERWE survey. CSEntry tablets sync to Dropbox, a GitHub Action
reads the sync files every 15 minutes and writes `data/summary.json`, and `index.html` shows it.

Only counts go into `data/summary.json`. Names, business names and answers never leave Dropbox.

## Secrets (Settings > Secrets and variables > Actions)
- `DROPBOX_APP_KEY`
- `DROPBOX_APP_SECRET`
- `DROPBOX_REFRESH_TOKEN`

## Files
- `index.html` : the dashboard page
- `data/summary.json` : the numbers (updated automatically)
- `scripts/fetch_dropbox.py` : reads Dropbox and makes the numbers
- `.github/workflows/update-data.yml` : runs the script every 15 minutes
