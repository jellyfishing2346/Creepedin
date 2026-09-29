#!/usr/bin/env python3
"""
Creepedin: hunts SWE / AI / ML roles straight from company job boards.

Instead of scraping LinkedIn (against its ToS, and it bans accounts fast), this
pulls from the public JSON APIs of the applicant-tracking systems most tech
companies post to: Greenhouse, Lever, and Ashby. Those are the same listings
LinkedIn mirrors, usually posted there first.

Usage:
    pip install requests
    python creepedin.py                          # all matching roles
    python creepedin.py --location "new york|remote"
    python creepedin.py --only-new               # only roles not seen on a previous run
"""
import argparse
import csv
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIG: edit these to taste
# ---------------------------------------------------------------------------

# Company "slugs" per ATS. Find one by opening a company's careers page and
# looking at the job-board URL, e.g. boards.greenhouse.io/<slug>,
# jobs.lever.co/<slug>, jobs.ashbyhq.com/<slug>. Wrong slugs are just
# reported and skipped.
COMPANIES = {
    "greenhouse": ["anthropic", "stripe", "databricks", "figma", "airbnb", "scaleai", "discord"],
    "lever": ["palantir", "spotify"],
    "ashby": ["openai", "ramp", "notion", "linear", "cohere"],
}

# A title must match at least one of these (case-insensitive).
INCLUDE = [
    r"software engineer", r"\bswe\b", r"software developer",
    r"machine learning", r"\bml\b", r"\bai\b", r"\bllm", r"deep learning",
    r"research engineer", r"applied scientist", r"research scientist",
    r"data scientist", r"backend", r"full[- ]?stack", r"infrastructure engineer",
]

# ...and must match none of these. Trim this list if you're senior.
EXCLUDE = [
    r"\bstaff\b", r"\bprincipal\b", r"\bdirector\b", r"\bmanager\b",
    r"\bhead of\b", r"\bvp\b", r"\blead\b",
]

# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "creepedin/1.0 (personal job search)"
TIMEOUT = 20


@dataclass
class Job:
    uid: str
    source: str
    company: str
    title: str
    location: str
    posted: str
    url: str
    is_new: bool = False


def fetch_greenhouse(slug):
    r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=TIMEOUT)
    r.raise_for_status()
    return [
        Job(
            uid=f"gh:{slug}:{j['id']}",
            source="greenhouse",
            company=slug,
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            posted=(j.get("updated_at") or "")[:10],
            url=j.get("absolute_url", ""),
        )
        for j in r.json().get("jobs", [])
    ]


def fetch_lever(slug):
    r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        created = j.get("createdAt")
        posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if created else ""
        jobs.append(Job(
            uid=f"lv:{slug}:{j['id']}",
            source="lever",
            company=slug,
            title=j.get("text", ""),
            location=(j.get("categories") or {}).get("location", "") or "",
            posted=posted,
            url=j.get("hostedUrl", ""),
        ))
    return jobs


def fetch_ashby(slug):
    r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        loc = j.get("location", "") or ""
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append(Job(
            uid=f"ab:{slug}:{j['id']}",
            source="ashby",
            company=slug,
            title=j.get("title", ""),
            location=loc,
            posted=(j.get("publishedAt") or "")[:10],
            url=j.get("jobUrl", ""),
        ))
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def matches(job, location_re=None):
    title = job.title.lower()
    if not any(re.search(p, title) for p in INCLUDE):
        return False
    if any(re.search(p, title) for p in EXCLUDE):
        return False
    if location_re and not re.search(location_re, job.location, re.IGNORECASE):
        return False
    return True


def mark_new(jobs, db_path):
    """Flag jobs never seen before and remember them for next time."""
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY, first_seen TEXT)")
    seen = {row[0] for row in con.execute("SELECT uid FROM seen")}
    today = datetime.now().strftime("%Y-%m-%d")
    for job in jobs:
        if job.uid not in seen:
            job.is_new = True
            con.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (job.uid, today))
    con.commit()
    con.close()


def scrape():
    tasks = [(src, slug) for src, slugs in COMPANIES.items() for slug in slugs]
    all_jobs, failures = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(FETCHERS[src], slug): (src, slug) for src, slug in tasks}
        for fut in as_completed(futures):
            src, slug = futures[fut]
            try:
                jobs = fut.result()
                all_jobs.extend(jobs)
                print(f"  ok   {src:<10} {slug:<14} {len(jobs):>4} postings")
            except Exception as e:
                failures.append((src, slug))
                print(f"  FAIL {src:<10} {slug:<14} {type(e).__name__}: {e}"[:120])
    return all_jobs, failures


def main():
    ap = argparse.ArgumentParser(description="Creepedin: find SWE/AI/ML roles")
    ap.add_argument("--location", help='regex on location, e.g. "new york|nyc|remote"')
    ap.add_argument("--only-new", action="store_true", help="only show roles not seen on a previous run")
    ap.add_argument("--out", default="creepedin_jobs.csv")
    ap.add_argument("--db", default="creepedin.db")
    args = ap.parse_args()

    print("Scraping job boards...")
    all_jobs, failures = scrape()

    hits = [j for j in all_jobs if matches(j, args.location)]
    mark_new(hits, args.db)
    if args.only_new:
        hits = [j for j in hits if j.is_new]
    # newest postings first, then new-to-you ones float to the top
    hits.sort(key=lambda j: j.posted, reverse=True)
    hits.sort(key=lambda j: not j.is_new)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(hits[0]).keys()) if hits else ["uid"])
        writer.writeheader()
        for j in hits:
            writer.writerow(asdict(j))

    new_count = sum(j.is_new for j in hits)
    print(f"\n{len(all_jobs)} postings scanned, {len(hits)} matched, {new_count} new.")
    for j in hits[:25]:
        flag = "NEW " if j.is_new else "    "
        print(f"{flag}{j.company:<12} {j.title[:55]:<55} {j.location[:25]:<25} {j.url}")
    if len(hits) > 25:
        print(f"...and {len(hits) - 25} more in {args.out}")
    if failures:
        print(f"\n{len(failures)} board(s) failed; check those slugs in COMPANIES.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
