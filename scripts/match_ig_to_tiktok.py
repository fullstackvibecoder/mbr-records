"""Backfill matcher: propose Instagram reels that mirror existing TikToks.

Run once locally (or whenever you want to refresh the proposal).  Does
NOT mutate dashboard.html — emits a JSON proposal for human review.
Apply with `scripts/apply_ig_matches.py` after eyeballing.

Pipeline:
  1. Enumerate reels from @<handle> via instaloader.
  2. Load existing TikToks from videos.jsonl + their transcripts.
  3. For each TikTok, find IG candidates whose upload_date is within
     ±MATCH_WINDOW_DAYS.
  4. If exactly one candidate → propose with confidence=high.
  5. If 2+ → download IG audio, transcribe with whisper, score
     transcript Jaccard against the TikTok's transcript; propose the
     winner above MIN_OVERLAP, else mark ambiguous.
  6. Emit proposed_ig_matches.json:
       [{ "tiktokId": "...", "instagramShortcode": "...",
          "confidence": "high|medium|low|ambiguous",
          "tiktokDate": "YYYY-MM-DD",
          "igDate": "YYYY-MM-DD",
          "reason": "single-candidate" | "jaccard=0.72" | ... }, ...]

Usage:
  $ python scripts/match_ig_to_tiktok.py
  $ python scripts/match_ig_to_tiktok.py --handle georgebrettolson
  $ python scripts/match_ig_to_tiktok.py --since 2026-03-01

Requires INSTAGRAM_USERNAME + (INSTALOADER_SESSION_FILE or
INSTAGRAM_SESSION_B64) in the environment.  See scripts/instagram.py.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

# Import the local helper module (sibling file)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from instagram import enumerate_reels, transcript_jaccard, DEFAULT_HANDLE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VIDEOS_JSONL = ROOT / "videos.jsonl"
TRANSCRIPTS_DIR = ROOT / "transcripts"
AUDIO_DIR = ROOT / "audio"
PROPOSAL_PATH = ROOT / "proposed_ig_matches.json"

# Tuning knobs (deliberately conservative).
MATCH_WINDOW_DAYS = 2          # how far apart upload dates can be
MIN_OVERLAP_HIGH = 0.55        # propose as "high" confidence
MIN_OVERLAP_MEDIUM = 0.30      # propose as "medium" confidence
MIN_OVERLAP_LOW = 0.15         # below this: ambiguous, don't propose


def log(msg: str) -> None:
    print(f"[match] {msg}", flush=True)


def load_tiktok_corpus() -> list[dict]:
    """Return [{id, upload_date, date_iso, transcript}] sorted by date."""
    if not VIDEOS_JSONL.exists():
        return []
    rows = []
    for line in VIDEOS_JSONL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = v.get("id")
        upload = v.get("upload_date")
        if not vid or not upload:
            continue
        date_iso = f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}"
        # Transcript file naming: <YYYYMMDD>_<id>.txt
        tx_path = TRANSCRIPTS_DIR / f"{upload}_{vid}.txt"
        transcript = tx_path.read_text() if tx_path.exists() else ""
        rows.append({
            "id": vid,
            "upload_date": upload,
            "date_iso": date_iso,
            "transcript": transcript,
            "url": v.get("webpage_url", ""),
        })
    rows.sort(key=lambda r: r["upload_date"])
    return rows


def download_ig_audio(reel: dict) -> Path | None:
    """Download IG reel audio via yt-dlp (works for single-reel URLs).
    Returns the m4a path, or None on failure."""
    AUDIO_DIR.mkdir(exist_ok=True)
    target = AUDIO_DIR / f"{reel['upload_date']}_IG_{reel['id']}.m4a"
    if target.exists():
        return target
    cmd = [
        "yt-dlp", "--cookies-from-browser", "chrome",
        "-x", "--audio-format", "m4a", "--no-warnings",
        "-o", str(AUDIO_DIR / "%(upload_date)s_IG_%(id)s.%(ext)s"),
        reel["webpage_url"],
    ]
    try:
        subprocess.run(cmd, check=True, timeout=120, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log(f"   yt-dlp failed for {reel['id']}: {e}")
        return None
    return target if target.exists() else None


def transcribe(audio_path: Path) -> str:
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    tx_path = TRANSCRIPTS_DIR / f"{audio_path.stem}.txt"
    if tx_path.exists():
        return tx_path.read_text().strip()
    cmd = [
        "whisper", str(audio_path),
        "--model", "small.en",
        "--output_dir", str(TRANSCRIPTS_DIR),
        "--output_format", "txt",
        "--verbose", "False",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log(f"   whisper failed: {e}")
        return ""
    return tx_path.read_text().strip() if tx_path.exists() else ""


def find_candidates(tiktok: dict, reels: list[dict]) -> list[dict]:
    """IG reels within ±MATCH_WINDOW_DAYS of the TikTok's upload_date."""
    t_date = _dt.datetime.strptime(tiktok["upload_date"], "%Y%m%d").date()
    window = _dt.timedelta(days=MATCH_WINDOW_DAYS)
    out = []
    for r in reels:
        r_date = _dt.datetime.strptime(r["upload_date"], "%Y%m%d").date()
        if abs((r_date - t_date).days) <= MATCH_WINDOW_DAYS:
            out.append(r)
    return out


def score_pair(tiktok: dict, reel: dict) -> tuple[float, str]:
    """Return (overlap_score, transcript_used).
    Lazily downloads + transcribes the reel only on demand."""
    if not tiktok["transcript"]:
        return 0.0, ""
    audio = download_ig_audio(reel)
    if not audio:
        return 0.0, ""
    reel_transcript = transcribe(audio)
    if not reel_transcript:
        return 0.0, ""
    return transcript_jaccard(tiktok["transcript"], reel_transcript), reel_transcript


def classify(overlap: float) -> str:
    if overlap >= MIN_OVERLAP_HIGH:
        return "high"
    if overlap >= MIN_OVERLAP_MEDIUM:
        return "medium"
    if overlap >= MIN_OVERLAP_LOW:
        return "low"
    return "ambiguous"


def build_proposals(tiktoks: list[dict], reels: list[dict], skip: set[str]) -> list[dict]:
    proposals: list[dict] = []
    matched_reels: set[str] = set()
    for t in tiktoks:
        if t["id"] in skip:
            continue
        candidates = [c for c in find_candidates(t, reels) if c["id"] not in matched_reels]
        if not candidates:
            continue

        if len(candidates) == 1:
            r = candidates[0]
            proposals.append({
                "tiktokId": t["id"],
                "instagramShortcode": r["id"],
                "confidence": "high",
                "tiktokDate": t["date_iso"],
                "igDate": f"{r['upload_date'][:4]}-{r['upload_date'][4:6]}-{r['upload_date'][6:8]}",
                "reason": "single-candidate (date window)",
            })
            matched_reels.add(r["id"])
            log(f"   {t['id']} ↔ {r['id']}  single-candidate")
            continue

        # 2+: score by transcript overlap
        scored: list[tuple[float, dict]] = []
        for c in candidates:
            score, _ = score_pair(t, c)
            scored.append((score, c))
        scored.sort(reverse=True, key=lambda x: x[0])
        best_score, best_reel = scored[0]
        conf = classify(best_score)
        if conf == "ambiguous":
            log(f"   {t['id']}: {len(candidates)} candidates, top jaccard={best_score:.2f} → ambiguous, skipping")
            continue
        proposals.append({
            "tiktokId": t["id"],
            "instagramShortcode": best_reel["id"],
            "confidence": conf,
            "tiktokDate": t["date_iso"],
            "igDate": f"{best_reel['upload_date'][:4]}-{best_reel['upload_date'][4:6]}-{best_reel['upload_date'][6:8]}",
            "reason": f"jaccard={best_score:.2f} ({len(candidates)} candidates)",
        })
        matched_reels.add(best_reel["id"])
        log(f"   {t['id']} ↔ {best_reel['id']}  jaccard={best_score:.2f} ({conf})")

    return proposals


def existing_instagram_ids(dashboard_html: Path | None = None) -> set[str]:
    """Pull instagramId values already in the dashboard so we skip them."""
    dashboard_html = dashboard_html or (ROOT / "dashboard.html")
    if not dashboard_html.exists():
        return set()
    import re
    text = dashboard_html.read_text()
    return set(re.findall(r'instagramId:\s*"([^"]+)"', text))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handle", default=DEFAULT_HANDLE)
    parser.add_argument("--since", default=None, help="ISO datetime; only enumerate reels at or after this date")
    parser.add_argument("--out", default=str(PROPOSAL_PATH))
    args = parser.parse_args()

    since: _dt.datetime | None = None
    if args.since:
        since = _dt.datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=_dt.timezone.utc)

    log(f"Loading TikTok corpus from {VIDEOS_JSONL.name}")
    tiktoks = load_tiktok_corpus()
    log(f"  {len(tiktoks)} TikToks loaded ({sum(1 for t in tiktoks if t['transcript'])} with transcripts)")

    log(f"Enumerating @{args.handle} reels via instaloader")
    reels = enumerate_reels(args.handle, since)
    log(f"  {len(reels)} reels enumerated")

    # Skip items already mapped to an IG shortcode in the dashboard.
    existing_ig = existing_instagram_ids()
    skip_tt = set()  # TikTok IDs whose items already carry an instagramId
    # We don't have a TikTok→IG reverse map yet, so skip-set lives at the IG side:
    matched_already = existing_ig
    reels = [r for r in reels if r["id"] not in matched_already]
    log(f"  {len(reels)} reels after removing {len(matched_already)} already-mapped")

    proposals = build_proposals(tiktoks, reels, skip_tt)
    log(f"Proposing {len(proposals)} matches")

    Path(args.out).write_text(json.dumps(proposals, indent=2) + "\n")
    log(f"Wrote {args.out}")
    log("Review, then apply with: python scripts/apply_ig_matches.py " + args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
