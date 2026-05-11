# Multicultural Bureau of Records

> Live: **https://mbr-records.vercel.app**

An interactive ledger of governance decisions from [@georgebrettolson](https://www.tiktok.com/@georgebrettolson)'s ongoing TikTok satire — the "United Delegations of the MF Function." Every accord, ruling, trade, lease, and pending matter from his March 4 – April 30, 2026 series, sourced to the original communiqué.

This is a fan-made satirical archive. No actual federal agency exists by this name and no governmental authority is claimed or implied.

![dashboard preview](https://mbr-records.vercel.app)

## What's in here

- **`dashboard.html`** — the working copy of the single-file dashboard
- **`mbr-records/`** — the deployed copy (`index.html` + `vercel.json`)
- **`DECISIONS.md`** — the same ledger in plain Markdown
- **`corpus.md`** — all 49 transcripts in chronological order
- **`transcripts/`** — one `.txt` per video, named `YYYYMMDD_<videoid>.txt`
- **`filtered.jsonl`** / **`videos.jsonl`** — TikTok metadata (titles, view counts, timestamps)
- **`urls.txt`** — the 49 video URLs

The dashboard is a single self-contained HTML file (~130KB) with embedded JSON and vanilla JS. No build step, no dependencies, no framework. Drop it in any folder and open it in a browser.

## Auto-refresh

The dashboard updates itself. A GitHub Actions workflow at
`.github/workflows/update.yml` runs every six hours and:

1. Enumerates new posts on `@georgebrettolson` via `yt-dlp`
2. Diffs against `videos.jsonl` to find what's new
3. Downloads audio for each new video, transcribes locally with `whisper`
4. Calls Claude (Opus, tool-use mode) to (a) decide whether the video
   is part of the MF Function continuity and (b) extract structured
   items in the existing schema
5. Appends new items to the `ITEMS` array in `dashboard.html`,
   adds the videoId→date entry to `VIDEO_DATES`, drops the new
   transcript into `transcripts/`, and updates `LAST_UPDATED`
6. Commits, pushes, and triggers a Vercel redeploy

The pipeline only **appends** — it never modifies or removes existing
records. If Claude is uncertain about a video, the video is skipped.

### Required GitHub secrets

Set these in **Settings → Secrets and variables → Actions** for the
repo:

| Secret | Source | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys | yes |
| `VERCEL_TOKEN` | https://vercel.com/account/tokens (scope: `bottlenecklabs` team) | yes |
| `INSTAGRAM_USERNAME` | Plain username for the IG account that will scrape (NOT @georgebrettolson — use a separate account you control) | optional — IG ingest skips if absent |
| `INSTAGRAM_SESSION_B64` | `base64 -w0 < ~/.config/instaloader/session-<user>` after running `instaloader --login=<user>` locally once | optional — see above |

### Optional: Instagram ingest

The cron auto-ingests reels from `@georgebrettolson` per policy P1 (only
George's own account; third-party reels go through
`scripts/file_submission.py`).  If `INSTAGRAM_USERNAME` and
`INSTAGRAM_SESSION_B64` are both set, the cron will also enumerate his
IG reels each tick, dedupe against TikTok mirrors (transcript-overlap
Jaccard ≥ 0.55 within ±2 days → patch `instagramId` onto the existing
item), and ingest IG-only reels (like the Blaxican filing) as new items.
If either secret is missing the IG step is skipped silently.

To populate the session secret once locally:

```bash
pip install instaloader
instaloader --login=<your-scraper-account>   # prompts for password + 2FA
base64 -w0 < ~/.config/instaloader/session-<your-scraper-account>
# paste output into the INSTAGRAM_SESSION_B64 GitHub secret
```

### Manual run

Trigger a refresh manually from the **Actions** tab → *Refresh MBR
Records* → *Run workflow*. Useful for testing or after a long gap.

## Manual pipeline (no cron)

```bash
# 1. Enumerate all videos for an account
yt-dlp --flat-playlist --dump-json "https://www.tiktok.com/@<handle>" > videos.jsonl

# 2. Filter to the date range you want, write URLs
python3 -c "
import json, datetime
cutoff = int(datetime.datetime(2026, 3, 3, tzinfo=datetime.timezone.utc).timestamp())
with open('videos.jsonl') as f:
    for line in f:
        d = json.loads(line)
        if d.get('timestamp', 0) >= cutoff:
            print(d['webpage_url'])
" > urls.txt

# 3. Download audio only
yt-dlp -x --audio-format m4a -o "audio/%(upload_date)s_%(id)s.%(ext)s" -a urls.txt

# 4. Transcribe locally
whisper audio/*.m4a --model small.en --output_dir transcripts --output_format txt

# 5. Open dashboard.html → fill in ITEMS / ACCORDS / OPEN_MATTERS by hand
```

The structured data (the `ITEMS`, `ACCORDS`, `OPEN_MATTERS`, `RITUALS` arrays in the script) was hand-curated by reading every transcript and extracting decisions. That's the part you can't automate — it requires understanding the bit.

## Backfilling Instagram sources

The cron only matches IG↔TikTok going forward.  To backfill IG
shortcodes against the existing 100+ TikTok items, run the matcher
locally and review the proposal:

```bash
# Enumerate George's reels and propose matches.  Lazily downloads +
# transcribes IG audio only when a date window has multiple candidates.
python scripts/match_ig_to_tiktok.py

# Eyeball proposed_ig_matches.json.  Each row has confidence:
# high (single candidate in window) / medium / low / ambiguous.

# Apply the high-confidence matches.  --dry-run first if cautious.
python scripts/apply_ig_matches.py --min-confidence high --dry-run
python scripts/apply_ig_matches.py --min-confidence high
```

For one-off third-party submissions (e.g. delegate reels filed *to* the
bureau, like the Blaxican Delegation proposal from `@madhouse4real`):

```bash
python scripts/file_submission.py https://www.instagram.com/reels/<shortcode>/
# Writes a draft into pending_submissions/<shortcode>.json for review.
```

## Fork it for another creator

To do this for a different TikTok account:

1. Clone this repo
2. Replace the handle in the pipeline (step 1)
3. Re-run steps 1–4
4. Open `dashboard.html` and rewrite the `ITEMS`, `ACCORDS`, `OPEN_MATTERS`, etc. arrays with the decisions from *that* account's series
5. Update copy: `<title>`, agency name, seal text, page-header lede, footer
6. Deploy

The pipeline is generic. The structured data is the creative work.

## Deploying

```bash
cd mbr-records
vercel --prod --scope <your-team>
```

Or just open `dashboard.html` directly in a browser — works the same.

## Stack

- **`yt-dlp`** for enumeration + audio download
- **`whisper`** (OpenAI, `small.en`) for transcription
- Vanilla HTML/CSS/JS, no framework
- Federal aesthetic loosely inspired by the [U.S. Web Design System](https://designsystem.digital.gov/) — Public Sans + Source Serif Pro typography, navy + gold palette, USA-banner-style top strip
- Hosted on **Vercel**

## Credits

Built by Ara — [BottleneckLabs](https://bottlenecklabs.ai) · [TryEchoMe](https://tryechome.com). Repo lives at [github.com/fullstackvibecoder/mbr-records](https://github.com/fullstackvibecoder/mbr-records).

All credit for the actual content goes to **[@georgebrettolson](https://www.tiktok.com/@georgebrettolson)** — the bit is the bit, this is just a fan archive.

## License

MIT. Use it, fork it, do whatever. See `LICENSE`.
