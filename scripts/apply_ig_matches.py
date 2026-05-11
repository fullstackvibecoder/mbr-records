"""Apply a reviewed IG-match proposal to dashboard.html.

Reads `proposed_ig_matches.json` (or whichever path is passed), and for
each row inserts `instagramId: "<shortcode>"` into the dashboard item
that carries the matching `videoId`.  Deterministic; never auto-runs.

Usage:
  $ python scripts/apply_ig_matches.py
  $ python scripts/apply_ig_matches.py proposed_ig_matches.json
  $ python scripts/apply_ig_matches.py --min-confidence high

Conservatives:
  - Only patches items found by exact videoId match.
  - Skips items that already carry an instagramId.
  - Never removes or changes other fields.
  - Default min confidence is "medium"; pass --min-confidence high to be
    stricter, or low/ambiguous to include everything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard.html"
DEPLOY_HTML = ROOT / "mbr-records" / "index.html"
DEFAULT_PROPOSAL = ROOT / "proposed_ig_matches.json"

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "ambiguous": 0}


def log(msg: str) -> None:
    print(f"[apply] {msg}", flush=True)


def patch_item(html: str, tiktok_id: str, ig_shortcode: str) -> tuple[str, str]:
    """Insert `instagramId` into the single item with the given videoId.

    Returns (new_html, status) where status is one of:
      - "patched"     – instagramId added
      - "exists"      – item already had an instagramId
      - "not-found"   – no item with that videoId
      - "duplicate"   – more than one item has that videoId (skipped)
    """
    # Find ALL items matching this videoId.
    pattern = re.compile(
        r'\{\s*name:\s*"[^"]*"[^{}]*?videoId:\s*"' + re.escape(tiktok_id) + r'"[^{}]*?\}',
        re.DOTALL,
    )
    matches = list(pattern.finditer(html))
    if not matches:
        return html, "not-found"
    if len(matches) > 1:
        # Multiple items can share a videoId (a single TikTok may introduce
        # several phrases).  We patch ALL of them.
        new_html = html
        offset = 0
        patched_any = False
        for m in matches:
            block = m.group(0)
            if "instagramId:" in block:
                continue
            # Insert `instagramId: "<sc>"` immediately before `videoId:` in the block.
            new_block = re.sub(
                r'(videoId:\s*"[^"]*")',
                r'instagramId: "' + ig_shortcode + r'", \1',
                block,
                count=1,
            )
            start = m.start() + offset
            end = m.end() + offset
            new_html = new_html[:start] + new_block + new_html[end:]
            offset += len(new_block) - len(block)
            patched_any = True
        return (new_html, "patched") if patched_any else (html, "exists")
    # Single match
    m = matches[0]
    block = m.group(0)
    if "instagramId:" in block:
        return html, "exists"
    new_block = re.sub(
        r'(videoId:\s*"[^"]*")',
        r'instagramId: "' + ig_shortcode + r'", \1',
        block,
        count=1,
    )
    new_html = html[:m.start()] + new_block + html[m.end():]
    return new_html, "patched"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal", nargs="?", default=str(DEFAULT_PROPOSAL))
    parser.add_argument("--min-confidence", default="medium",
                        choices=["high", "medium", "low", "ambiguous"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    args = parser.parse_args()

    proposal_path = Path(args.proposal)
    if not proposal_path.exists():
        log(f"Proposal not found: {proposal_path}")
        return 1

    proposals = json.loads(proposal_path.read_text())
    min_rank = CONFIDENCE_RANK[args.min_confidence]
    proposals = [p for p in proposals if CONFIDENCE_RANK.get(p.get("confidence", "ambiguous"), 0) >= min_rank]
    log(f"Loaded {len(proposals)} proposals at min-confidence={args.min_confidence}")

    if not DASHBOARD.exists():
        log(f"Dashboard missing: {DASHBOARD}")
        return 1

    html = DASHBOARD.read_text()
    counters = {"patched": 0, "exists": 0, "not-found": 0, "duplicate": 0}
    for p in proposals:
        tiktok_id = p.get("tiktokId", "")
        ig = p.get("instagramShortcode", "")
        if not tiktok_id or not ig:
            continue
        html, status = patch_item(html, tiktok_id, ig)
        counters[status] = counters.get(status, 0) + 1
        log(f"   {tiktok_id} → {ig}  [{status}] ({p.get('confidence')}, {p.get('reason','')})")

    log(f"Summary: {counters}")

    if args.dry_run:
        log("Dry run; not writing.")
        return 0

    if counters.get("patched", 0) > 0:
        DASHBOARD.write_text(html)
        shutil.copyfile(DASHBOARD, DEPLOY_HTML)
        log(f"Wrote {DASHBOARD.relative_to(ROOT)} and synced {DEPLOY_HTML.relative_to(ROOT)}")
    else:
        log("No changes applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
