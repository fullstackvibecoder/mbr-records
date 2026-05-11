"""Manual third-party reel submission helper.

Per the cron policy (P1), only @georgebrettolson reels are auto-ingested.
Third-party submissions (e.g. Marla & Dave Thomas' Blaxican Delegation
reel from @madhouse4real) need a human in the loop.

This script automates the mechanical parts of that loop:
  1. Download the reel's audio (yt-dlp + Chrome cookies).
  2. Transcribe with whisper.
  3. Call Claude to extract candidate ITEMS in the existing schema.
  4. Drop a DRAFT JSON into `pending_submissions/<shortcode>.json` for
     human review.

It does NOT modify dashboard.html.  After reviewing the draft, you can
hand-edit it and apply via the existing path (manual paste, or future
tool).

Usage:
  $ python scripts/file_submission.py https://www.instagram.com/reels/DYC2oCkNmoX/
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from anthropic import Anthropic
except ImportError:
    print("anthropic SDK not installed.  pip install anthropic", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = ROOT / "audio"
TRANSCRIPTS_DIR = ROOT / "transcripts"
PENDING_DIR = ROOT / "pending_submissions"

# Reuse the cron's extraction tool definition.  We import it here rather
# than redefine, so the schema stays in lock-step with update.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from update import EXTRACTION_TOOL, MODEL  # noqa: E402


def log(msg: str) -> None:
    print(f"[submission] {msg}", flush=True)


def extract_shortcode(url: str) -> str:
    """https://www.instagram.com/reels/DYC2oCkNmoX/ → DYC2oCkNmoX"""
    m = re.search(r"/reels?/([A-Za-z0-9_-]+)/?", url)
    if not m:
        raise ValueError(f"Could not parse shortcode from URL: {url}")
    return m.group(1)


def probe_metadata(url: str) -> dict:
    """yt-dlp --print to fetch upload_date, uploader, etc."""
    res = subprocess.run(
        ["yt-dlp", "--cookies-from-browser", "chrome", "--no-warnings",
         "--print", "%(id)s\t%(upload_date)s\t%(uploader)s\t%(duration)s\t%(webpage_url)s",
         url],
        capture_output=True, text=True, check=True, timeout=60,
    )
    parts = res.stdout.strip().split("\t")
    return {
        "id": parts[0],
        "upload_date": parts[1],
        "uploader": parts[2] if len(parts) > 2 else "",
        "duration": parts[3] if len(parts) > 3 else "",
        "webpage_url": parts[4] if len(parts) > 4 else url,
    }


def download_audio(url: str, upload_date: str, shortcode: str) -> Path:
    AUDIO_DIR.mkdir(exist_ok=True)
    target = AUDIO_DIR / f"{upload_date}_IG_{shortcode}.m4a"
    if target.exists():
        return target
    subprocess.run(
        ["yt-dlp", "--cookies-from-browser", "chrome", "--no-warnings",
         "-x", "--audio-format", "m4a",
         "-o", str(AUDIO_DIR / "%(upload_date)s_IG_%(id)s.%(ext)s"),
         url],
        check=True, timeout=180,
    )
    return target


def transcribe(audio_path: Path) -> str:
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    tx_path = TRANSCRIPTS_DIR / f"{audio_path.stem}.txt"
    if tx_path.exists():
        return tx_path.read_text().strip()
    subprocess.run(
        ["whisper", str(audio_path),
         "--model", "small.en",
         "--output_dir", str(TRANSCRIPTS_DIR),
         "--output_format", "txt",
         "--verbose", "False"],
        check=True, timeout=600,
    )
    return tx_path.read_text().strip() if tx_path.exists() else ""


def analyze(meta: dict, transcript: str, client: Anthropic) -> dict:
    """Re-uses the cron's tool definition for consistency."""
    date_iso = f"{meta['upload_date'][:4]}-{meta['upload_date'][4:6]}-{meta['upload_date'][6:8]}"
    system = (
        "You analyze short videos for the Multicultural Bureau of Records, "
        "a satirical archive of @georgebrettolson's MF Function bit. This "
        "video may be a THIRD-PARTY submission to the bureau (e.g. a "
        "delegate filing a motion).  Treat it the same as a primary video: "
        "extract any new items, status updates, or bulletins.  Note in "
        "`summary` who the submitter appears to be."
    )
    user = (
        f"Submitter handle: @{meta.get('uploader','unknown')}\n"
        f"Date: {date_iso}\n"
        f"Source URL: {meta['webpage_url']}\n\n"
        f"Transcript:\n{transcript}\n"
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "extract_decisions"},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "extract_decisions":
            return block.input
    return {"is_relevant": False, "summary": "no tool_use block returned",
            "items": [], "status_updates": [], "bulletin_items": []}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Instagram reel URL")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in __import__("os").environ:
        log("ANTHROPIC_API_KEY not set; aborting.")
        return 1

    shortcode = extract_shortcode(args.url)
    log(f"Shortcode: {shortcode}")

    meta = probe_metadata(args.url)
    log(f"Uploader: @{meta['uploader']}; date: {meta['upload_date']}; duration: {meta['duration']}s")

    audio = download_audio(args.url, meta["upload_date"], shortcode)
    log(f"Audio: {audio}")

    transcript = transcribe(audio)
    if not transcript:
        log("No transcript produced; aborting.")
        return 1
    log(f"Transcript ({len(transcript)} chars):\n{transcript[:500]}{'…' if len(transcript) > 500 else ''}")

    client = Anthropic()
    analysis = analyze(meta, transcript, client)

    PENDING_DIR.mkdir(exist_ok=True)
    out = {
        "submission_url": args.url,
        "instagramShortcode": shortcode,
        "uploader": meta["uploader"],
        "upload_date": meta["upload_date"],
        "date_iso": f"{meta['upload_date'][:4]}-{meta['upload_date'][4:6]}-{meta['upload_date'][6:8]}",
        "duration_seconds": meta.get("duration", ""),
        "transcript": transcript,
        "claude_analysis": analysis,
        "status": "pending-review",
        "review_notes": "",
    }
    out_path = PENDING_DIR / f"{shortcode}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    log(f"Draft written to {out_path.relative_to(ROOT)}")
    log("Review, hand-edit if needed, then add the curated items to dashboard.html.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
