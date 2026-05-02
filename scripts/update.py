#!/usr/bin/env python3
"""
Refresh pipeline for MBR Records.

Triggered by .github/workflows/update.yml on a 6-hourly cron.

Steps:
  1. Enumerate the @georgebrettolson account via yt-dlp.
  2. Diff against videos.jsonl to find new posts since the last run.
  3. For each new video: download audio, transcribe with whisper.
  4. Ask Claude (tool-use mode) whether the video is part of the MF Function
     continuity and, if so, to extract any new items in the existing schema.
  5. Append relevant items to the ITEMS array in dashboard.html, append the
     videoId→date entry to VIDEO_DATES, append the video record to videos.jsonl
     and filtered.jsonl, and commit the new transcript file.
  6. Update the LAST_UPDATED timestamp.
  7. Sync mbr-records/index.html from the working dashboard.html.

Conservative by design: the pipeline only APPENDS items. It never modifies
the 97 hand-curated records that were extracted at v1. If Claude is uncertain
about a video, it skips it. Logs are verbose so failures are easy to debug
from the Actions UI.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
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
DASHBOARD = ROOT / "dashboard.html"
DEPLOY_HTML = ROOT / "mbr-records" / "index.html"
VIDEOS_JSONL = ROOT / "videos.jsonl"
FILTERED_JSONL = ROOT / "filtered.jsonl"
TRANSCRIPTS_DIR = ROOT / "transcripts"
AUDIO_DIR = ROOT / "audio"

ACCOUNT = "georgebrettolson"
CUTOFF = _dt.datetime(2026, 3, 3, tzinfo=_dt.timezone.utc)
MODEL = "claude-opus-4-7"

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[refresh] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""))
    return subprocess.run(cmd, check=True, **kw)


# ---------------------------------------------------------------------------
# enumeration & diff
# ---------------------------------------------------------------------------


def existing_video_ids() -> set[str]:
    if not VIDEOS_JSONL.exists():
        return set()
    ids: set[str] = set()
    for line in VIDEOS_JSONL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ids.add(json.loads(line)["id"])
        except (KeyError, json.JSONDecodeError):
            continue
    return ids


def enumerate_account() -> list[dict]:
    res = subprocess.run(
        [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            f"https://www.tiktok.com/@{ACCOUNT}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    rows: list[dict] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def find_new(all_videos: list[dict], known: set[str]) -> list[dict]:
    out = []
    for v in all_videos:
        vid = v.get("id")
        ts = v.get("timestamp", 0) or 0
        if not vid or vid in known:
            continue
        if ts < CUTOFF.timestamp():
            continue
        out.append(v)
    out.sort(key=lambda v: v.get("timestamp", 0))
    return out


# ---------------------------------------------------------------------------
# audio + transcript
# ---------------------------------------------------------------------------


def download_audio(video: dict) -> Path:
    AUDIO_DIR.mkdir(exist_ok=True)
    upload = video["upload_date"]
    vid = video["id"]
    target = AUDIO_DIR / f"{upload}_{vid}.m4a"
    if target.exists():
        return target
    run(
        [
            "yt-dlp",
            "-x",
            "--audio-format",
            "m4a",
            "-o",
            str(AUDIO_DIR / "%(upload_date)s_%(id)s.%(ext)s"),
            video["webpage_url"],
        ],
        timeout=300,
    )
    return target


def transcribe(audio: Path) -> str:
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    txt = TRANSCRIPTS_DIR / f"{audio.stem}.txt"
    if txt.exists():
        return txt.read_text().strip()
    run(
        [
            "whisper",
            str(audio),
            "--model",
            "small.en",
            "--output_dir",
            str(TRANSCRIPTS_DIR),
            "--output_format",
            "txt",
            "--verbose",
            "False",
        ],
        timeout=600,
    )
    return txt.read_text().strip()


# ---------------------------------------------------------------------------
# Claude: relevance + extraction
# ---------------------------------------------------------------------------


EXTRACTION_TOOL = {
    "name": "extract_decisions",
    "description": (
        "Decide whether a TikTok video is part of George Brett Olson's MF "
        "Function bit. If so, extract three things: (1) NEW items "
        "introduced, (2) status UPDATES to items already on the record, "
        "(3) one or two short BULLETIN-style lore items for the news ticker."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_relevant": {
                "type": "boolean",
                "description": (
                    "True only if the video clearly references named bodies "
                    "from the MF Function bit (Caucasian Caucus, Black "
                    "Delegation, Council of Elder Aunties, Gabagoo Guild, "
                    "League of Latinos, MF Function itself, etc.) or is a "
                    "follow-up to an ongoing accord. False for unrelated "
                    "standup, song clips, impressions, or one-off jokes."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One-sentence summary of the video's content.",
            },
            "items": {
                "type": "array",
                "description": (
                    "NEW items, trades, leases, gifts, rulings, or pending "
                    "matters introduced for the first time in this video. "
                    "Empty array if the video only references existing items."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "phrase", "food", "person", "service", "rule",
                                "ritual", "event", "body", "gesture",
                                "hairstyle", "recipe", "request",
                            ],
                        },
                        "status": {
                            "type": "string",
                            "enum": [
                                "ratified", "pending", "proposed", "leased",
                                "gifted", "banned", "retired", "contested",
                                "rejected",
                            ],
                        },
                        "kind": {
                            "type": "string",
                            "enum": [
                                "trade", "lease", "gift", "concession",
                                "ruling", "claim", "request", "protocol",
                            ],
                        },
                        "owner": {"type": "string"},
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "terms": {"type": "string"},
                        "conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "timeLimit": {"type": ["string", "null"]},
                        "ratifiedBy": {"type": ["string", "null"]},
                        "sourceQuote": {"type": "string"},
                    },
                    "required": [
                        "name", "type", "status", "kind", "owner", "terms",
                    ],
                },
            },
            "status_updates": {
                "type": "array",
                "description": (
                    "Status changes to items ALREADY on the record. Match "
                    "by item_name exactly as it appears in the existing "
                    "ledger. Only include if the new video CLEARLY moves "
                    "an existing item (e.g., a pending matter is ratified, "
                    "a contested matter is resolved). When in doubt, omit."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "item_name": {
                            "type": "string",
                            "description": "Exact name of the existing item, as it appears on the ledger.",
                        },
                        "new_status": {
                            "type": "string",
                            "enum": [
                                "ratified", "pending", "proposed", "leased",
                                "gifted", "banned", "retired", "contested",
                                "rejected",
                            ],
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief justification for the change.",
                        },
                        "source_quote": {
                            "type": "string",
                            "description": "Direct quote from the transcript supporting this change.",
                        },
                    },
                    "required": ["item_name", "new_status", "reason", "source_quote"],
                },
            },
            "bulletin_items": {
                "type": "array",
                "description": (
                    "One or two SHORT (under 140 char) news-ticker items "
                    "in the dry CSPAN voice of the Bureau. Treat the bit's "
                    "world as if it were real: dispatches, advisories, "
                    "procurement notes, court rulings. Do not state it is "
                    "fictional. Do not break the frame. Each item has a "
                    "category tag and body text."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {
                            "type": "string",
                            "description": "One-word category in the style of: Intelligence, Markets, Advisory, Transport, Schedule, Personnel, Court, Procurement, Security, Diplomacy, Jurisprudence, Infrastructure, Weather, Trade, Standing, Dispatch, Notice, Briefing.",
                        },
                        "text": {
                            "type": "string",
                            "description": "Bulletin body in formal procedural voice, under 140 characters. No exclamation points. No emoji. Treat the bit's universe as real.",
                        },
                    },
                    "required": ["tag", "text"],
                },
            },
        },
        "required": ["is_relevant", "summary", "items"],
    },
}


SYSTEM_PROMPT = """You analyze videos from George Brett Olson's TikTok \
@georgebrettolson, where he runs an ongoing satirical bit called \"the MF \
Function\" — a fictional UN-style governance system between cultural \
delegations.

Recognized bodies in the bit include the Caucasian Caucus (the narrator), \
the Black Delegation (with subgroups: Bay Area, Atlanta, Caribbean Brigade, \
Auntie Army, Black Barber Caucus, Black Betterment Consortium), the Council \
of Elder Aunties (supreme ratifying body), the Council of OGs, the League of \
Latinos (formerly Mexican Delegation), the Asian Association, the Arab \
Delegation, the Gabagoo Guild (Italian), the Irish Association, the Ginger \
Gang Gang, the Rainbow Syndicate (lesbian delegation), the Jamaican \
Delegation, the Hawaiian Delegation, the Indian Delegation, and the \
Multicultural Bureau of Safety.

Decisions in the bit take the form of accords (multi-clause agreements), \
rulings (decrees from the Aunties or OGs), trades (slang exchanged for \
slang), leases (temporary cultural transfers, e.g., \"Stone Cold Steve \
Austin leased to Italians for 60 days\"), and concessions (full ownership \
transfers, e.g., \"No Way José ceded to League of Latinos\").

Your job: determine whether a given video is part of this MF Function \
continuity, and if so, extract any decisions/items introduced or resolved \
in it.

Be conservative. If the video is purely standup, a song clip, an \
impression, or a one-off joke that doesn't reference the bit's bodies or \
accords, set is_relevant=false and return an empty items array. The bit's \
universe has consistent terminology — only mark relevant if that terminology \
appears."""


def existing_item_summary() -> str:
    """A compact summary of the current ledger so Claude can match status updates."""
    html = DASHBOARD.read_text()
    # Pull each item's name + current status from the ITEMS array
    pattern = re.compile(r'\{\s*name:\s*"([^"]+)"[^}]*?status:\s*"([^"]+)"', re.DOTALL)
    rows = pattern.findall(html)
    if not rows:
        return "(no existing items found)"
    # Trim to keep the prompt small
    lines = [f"  - {name} [{status}]" for name, status in rows]
    return "\n".join(lines[:120])


def analyze(video: dict, transcript: str, client: Anthropic) -> dict:
    existing = existing_item_summary()
    user_msg = (
        f"Video metadata:\n"
        f"Title: {video.get('title', '')}\n"
        f"Description: {video.get('description', '')}\n"
        f"Date: {video.get('upload_date', '')}\n"
        f"Duration: {video.get('duration', 0)}s\n\n"
        f"Transcript:\n{transcript}\n\n"
        f"Existing items on the record (NAME [status]) — use these EXACT "
        f"names if proposing status updates:\n{existing}\n\n"
        f"Tasks: (1) decide relevance; (2) extract NEW items if any; "
        f"(3) flag any STATUS UPDATES to existing items; (4) write one or "
        f"two SHORT bulletin items for the chyron, in the Bureau's voice."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "extract_decisions"},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    return {
        "is_relevant": False,
        "summary": "",
        "items": [],
        "status_updates": [],
        "bulletin_items": [],
    }


# ---------------------------------------------------------------------------
# dashboard.html mutation
# ---------------------------------------------------------------------------


def _format_iso_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _js_string(s: str) -> str:
    """Escape a string for embedding in a JS double-quoted literal."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _item_to_js(it: dict, video_id: str, date_iso: str) -> str:
    """Render an item dict as a JS object literal matching the existing schema."""
    parts: list[str] = []
    parts.append(f'name: "{_js_string(it["name"])}"')
    parts.append(f'type: "{_js_string(it["type"])}"')
    parts.append(f'status: "{_js_string(it["status"])}"')
    parts.append(f'kind: "{_js_string(it["kind"])}"')
    parts.append(f'owner: "{_js_string(it.get("owner", "") or "")}"')

    pf = it.get("from", "") or ""
    pt = it.get("to", "") or ""
    parts.append(
        'parties: { from: "' + _js_string(pf) + '", to: "' + _js_string(pt) + '" }'
    )
    parts.append(f'terms: "{_js_string(it.get("terms", "") or "")}"')

    conds = it.get("conditions") or []
    if conds:
        cond_str = ", ".join('"' + _js_string(c) + '"' for c in conds)
        parts.append("conditions: [" + cond_str + "]")
    else:
        parts.append("conditions: []")

    tl = it.get("timeLimit")
    parts.append("timeLimit: " + ("null" if not tl else '"' + _js_string(tl) + '"'))
    rb = it.get("ratifiedBy")
    parts.append("ratifiedBy: " + ("null" if not rb else '"' + _js_string(rb) + '"'))

    parts.append(f'date: "{date_iso}"')
    parts.append(f'videoId: "{video_id}"')
    sq = it.get("sourceQuote") or ""
    parts.append(f'sourceQuote: "{_js_string(sq)}"')

    return "  { " + ", ".join(parts) + " }"


def append_items_to_dashboard(items: list[dict], video_id: str, date_iso: str) -> int:
    """Append new ITEMS entries before the closing `];` of the ITEMS array."""
    if not items:
        return 0

    html = DASHBOARD.read_text()
    # Find: const ITEMS = [\n  ...\n];
    # Insert before the closing `];` that follows the ITEMS opening.
    m = re.search(r"const\s+ITEMS\s*=\s*\[", html)
    if not m:
        log("WARNING: ITEMS array not found in dashboard.html; skipping insertion.")
        return 0
    start = m.end()
    # Walk to the matching closing `];`. We need to find it at top level.
    depth = 1
    i = start
    while i < len(html) and depth > 0:
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        log("WARNING: ITEMS array close not found.")
        return 0
    # i is the index of the closing `]`.
    rendered = ",\n".join(_item_to_js(it, video_id, date_iso) for it in items)
    insertion = ",\n" + rendered + "\n"
    new_html = html[:i] + insertion + html[i:]
    DASHBOARD.write_text(new_html)
    return len(items)


def append_video_date(video_id: str, date_iso: str) -> None:
    """Add a new entry to the VIDEO_DATES JS object."""
    html = DASHBOARD.read_text()
    m = re.search(r'const\s+VIDEO_DATES\s*=\s*\{', html)
    if not m:
        log("WARNING: VIDEO_DATES not found.")
        return
    # Find the closing `}` of the object.
    start = m.end()
    depth = 1
    i = start
    while i < len(html) and depth > 0:
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        log("WARNING: VIDEO_DATES close not found.")
        return
    # Skip if already present.
    obj_text = html[start:i]
    if f'"{video_id}"' in obj_text:
        return
    # Insert before the closing brace.
    sep = "" if obj_text.strip().endswith("{") else ","
    insertion = f'{sep}"{video_id}":"{date_iso}"'
    new_html = html[:i] + insertion + html[i:]
    DASHBOARD.write_text(new_html)


def apply_status_updates(updates: list[dict]) -> int:
    """Apply status changes to existing ITEMS entries. Conservative: only
    matches by exact item name. Returns number of updates applied."""
    if not updates:
        return 0
    html = DASHBOARD.read_text()
    applied = 0
    for u in updates:
        name = u.get("item_name", "").strip()
        new_status = u.get("new_status", "").strip()
        if not name or not new_status:
            continue
        # Find the item block with this exact name and update its status field.
        # Pattern: { name: "<name>", ... status: "<old>" ... }
        # Must be careful: only update the status field WITHIN this single item's braces.
        escaped = re.escape(name)
        # Match: name: "X", … status: "Y" with content in between but NOT crossing a closing brace
        item_re = re.compile(
            r'(\{\s*name:\s*"' + escaped + r'"[^{}]*?status:\s*")([^"]+)(")',
            re.DOTALL,
        )
        m = item_re.search(html)
        if not m:
            log(f"   status_update: item not found exactly: '{name}'")
            continue
        if m.group(2) == new_status:
            log(f"   status_update: '{name}' already '{new_status}', skipping")
            continue
        log(f"   status_update: '{name}' {m.group(2)} → {new_status} (reason: {u.get('reason','')[:60]})")
        html = html[:m.start()] + m.group(1) + new_status + m.group(3) + html[m.end():]
        applied += 1
    if applied:
        DASHBOARD.write_text(html)
    return applied


def append_bulletin_items(bulletins: list[dict]) -> int:
    """Append new bulletins to the LORE_BULLETINS array. Caps total at 30,
    rotating out the oldest (front of array) when exceeded."""
    if not bulletins:
        return 0
    html = DASHBOARD.read_text()
    m = re.search(r"const\s+LORE_BULLETINS\s*=\s*\[", html)
    if not m:
        log("WARNING: LORE_BULLETINS array not found.")
        return 0
    start = m.end()
    depth = 1
    i = start
    while i < len(html) and depth > 0:
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        log("WARNING: LORE_BULLETINS close not found.")
        return 0
    # Render new items
    rendered = []
    for b in bulletins:
        tag = _js_string(b.get("tag", "Notice"))
        text = _js_string(b.get("text", ""))
        if not text:
            continue
        rendered.append(f'  {{ tag: "{tag}", text: "{text}" }}')
    if not rendered:
        return 0
    insertion = ",\n" + ",\n".join(rendered) + "\n"
    new_html = html[:i] + insertion + html[i:]
    DASHBOARD.write_text(new_html)
    return len(rendered)


def update_last_updated() -> None:
    iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    iso = iso.replace("+00:00", "Z")
    html = DASHBOARD.read_text()
    new_html, n = re.subn(
        r'const\s+LAST_UPDATED\s*=\s*"[^"]*";',
        f'const LAST_UPDATED = "{iso}";',
        html,
        count=1,
    )
    if n:
        DASHBOARD.write_text(new_html)
        log(f"LAST_UPDATED → {iso}")


def append_videos_jsonl(videos: list[dict]) -> None:
    """Append new video records to videos.jsonl (preserving prior order)."""
    if not videos:
        return
    with VIDEOS_JSONL.open("a", encoding="utf-8") as f:
        for v in videos:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")


def append_filtered_jsonl(videos: list[dict]) -> None:
    """Append filtered records (the lighter shape used by other tooling)."""
    if not videos:
        return
    with FILTERED_JSONL.open("a", encoding="utf-8") as f:
        for v in videos:
            row = {
                "id": v["id"],
                "url": v["webpage_url"],
                "upload_date": v["upload_date"],
                "timestamp": v.get("timestamp", 0),
                "duration": v.get("duration"),
                "title": (v.get("title") or "").strip(),
                "description": (v.get("description") or "").strip(),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sync_deploy() -> None:
    DEPLOY_HTML.write_bytes(DASHBOARD.read_bytes())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    if "ANTHROPIC_API_KEY" not in os.environ:
        log("ANTHROPIC_API_KEY not set; aborting.")
        return 1

    client = Anthropic()
    log(f"Enumerating @{ACCOUNT}")
    all_videos = enumerate_account()
    known = existing_video_ids()
    new_videos = find_new(all_videos, known)
    log(f"Total: {len(all_videos)}; known: {len(known)}; new: {len(new_videos)}")

    if not new_videos:
        update_last_updated()
        sync_deploy()
        return 0

    relevant = 0
    items_added = 0
    statuses_changed = 0
    bulletins_added = 0

    for v in new_videos:
        vid = v["id"]
        date_iso = _format_iso_date(v["upload_date"])
        title = (v.get("title") or "")[:60]
        log(f"-- {vid} {v['upload_date']}: {title}")

        try:
            audio = download_audio(v)
            transcript = transcribe(audio)
        except subprocess.CalledProcessError as e:
            log(f"   download/transcribe failed: {e}; skipping.")
            continue

        analysis = analyze(v, transcript, client)
        log(f"   relevant={analysis.get('is_relevant')} summary={analysis.get('summary', '')[:80]}")

        if analysis.get("is_relevant"):
            relevant += 1
            # 1. Append new items
            items = analysis.get("items") or []
            n = append_items_to_dashboard(items, vid, date_iso)
            items_added += n
            log(f"   items appended: {n}")
            # 2. Apply status updates to existing items
            status_updates = analysis.get("status_updates") or []
            s = apply_status_updates(status_updates)
            statuses_changed += s
            if s:
                log(f"   status updates applied: {s}")
            # 3. Append bulletin items to the chyron feed
            bulletins = analysis.get("bulletin_items") or []
            b = append_bulletin_items(bulletins)
            bulletins_added += b
            if b:
                log(f"   bulletin items appended: {b}")
            append_video_date(vid, date_iso)
        else:
            # Still record video-date so future links can resolve.
            append_video_date(vid, date_iso)

    append_videos_jsonl(new_videos)
    append_filtered_jsonl(new_videos)
    update_last_updated()
    sync_deploy()

    log(
        f"Done. {relevant}/{len(new_videos)} relevant; "
        f"{items_added} items added; {statuses_changed} statuses changed; "
        f"{bulletins_added} bulletins added."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
