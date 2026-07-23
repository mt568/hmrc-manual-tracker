#!/usr/bin/env python3
"""HMRC manual change tracker — scraper.

Modes:
  scrape.py seed <slug>     full walk of one manual (onboarding; one-off)
  scrape.py daily           change_notes-driven fetch + rolling ETag sweep
  scrape.py health          exit non-zero if the latest run had failures

Design: BUILD_PLAN.md v2. Git is the version store; this script only writes
files and ledgers — committing is the workflow's job.
"""
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from markdownify import markdownify

ROOT = Path(__file__).resolve().parent.parent
API = "https://www.gov.uk/api/content"
UA = "hmrc-manual-tracker (contact: repository issues page)"
THROTTLE = 0.5          # seconds between requests (~2 req/s)
SWEEP_SHARDS = 7        # every page conditionally re-checked weekly
MASS_DELETE_FRACTION = 0.10
SCRAPER_VERSION = "0.1.0"
CONVERTER_VERSION = "markdownify==1.2.3"  # pinned; upgrades = explicit regen commit

session = requests.Session()
session.headers["User-Agent"] = UA


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(path, etag=None):
    """GET an API path. Returns (status, json_or_none, etag_or_none).
    status: 200, 304, 404, or raises after retries on 5xx/429."""
    url = API + path
    headers = {"If-None-Match": etag} if etag else {}
    for attempt in range(4):
        time.sleep(THROTTLE)
        r = session.get(url, headers=headers, timeout=30, allow_redirects=False)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code == 304:
            return 304, None, etag
        if r.status_code in (404, 410):
            return 404, None, None
        if 300 <= r.status_code < 400:
            return 404, {"redirect_to": r.headers.get("Location")}, None
        r.raise_for_status()
        doc = r.json()
        # the API can also express moves/removals as 200s with these types
        if doc.get("document_type") in ("redirect", "gone"):
            dest = None
            for red in doc.get("redirects", []):
                dest = red.get("destination")
            return 404, {"redirect_to": dest}, None
        return 200, doc, r.headers.get("ETag")
    raise RuntimeError(f"gave up after retries: {url}")


def canonical(doc):
    """Lossless-but-stable representation. Volatile publishing fields dropped."""
    det = doc.get("details", {})
    return {
        "section_id": det.get("section_id"),
        "title": doc.get("title"),
        "description": doc.get("description"),
        "base_path": doc.get("base_path"),
        "public_updated_at": doc.get("public_updated_at"),
        "breadcrumbs": det.get("breadcrumbs", []),
        "body": det.get("body") or "",
        "child_section_groups": det.get("child_section_groups", []),
    }


def is_valid(c):
    """Identity fields + (body or children). Contents pages have empty bodies."""
    if not (c["section_id"] and c["base_path"] and c["title"]):
        return False
    return bool(c["body"].strip()) or any(
        g.get("child_sections") for g in c["child_section_groups"]
    )


def to_view(c):
    fm = {k: c[k] for k in
          ("section_id", "title", "base_path", "public_updated_at")}
    lines = ["---", yaml.safe_dump(fm, sort_keys=True).strip(), "---", ""]
    if c["body"].strip():
        lines.append(markdownify(c["body"], heading_style="ATX").strip())
    for g in c["child_section_groups"]:
        if g.get("title"):
            lines.append(f"\n## {g['title']}\n")
        for ch in g.get("child_sections", []):
            lines.append(f"- [{ch.get('section_id', '')} — {ch['title']}]"
                         f"(https://www.gov.uk{ch['base_path']})")
    return "\n".join(lines) + "\n"


def children_of(c):
    """[(section_id, base_path), ...] from child_section_groups."""
    out = []
    for g in c["child_section_groups"]:
        for ch in g.get("child_sections", []):
            sid, bp = ch.get("section_id"), ch.get("base_path")
            if sid and bp:
                out.append((sid, bp))
    return out


def note_key(n):
    raw = f"{n.get('base_path')}|{n.get('published_at')}|{n.get('change_note')}"
    return hashlib.md5(raw.encode()).hexdigest()


def sweep_shard(section_id):
    return int(hashlib.md5(section_id.encode()).hexdigest(), 16) % SWEEP_SHARDS


class Ledger:
    def __init__(self, dirname):
        self.dir = ROOT / dirname
        self.dir.mkdir(exist_ok=True)

    def append(self, obj):
        f = self.dir / (datetime.now(timezone.utc).strftime("%Y-%m") + ".jsonl")
        with f.open("a") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def last(self):
        files = sorted(self.dir.glob("*.jsonl"))
        if not files:
            return None
        lines = files[-1].read_text().strip().splitlines()
        return json.loads(lines[-1]) if lines else None


events = Ledger("events")
runs = Ledger("runs")


class Manual:
    """All state changes for one manual staged in memory; flushed atomically."""

    def __init__(self, slug, run_id):
        self.slug, self.run_id = slug, run_id
        self.mpath = ROOT / "manifest" / f"{slug}.json"
        self.manifest = (json.loads(self.mpath.read_text()) if self.mpath.exists()
                         else {"watermark": "", "seen_note_keys": [],
                               "root_etag": None, "sections": {}})
        self.writes = {}     # relpath -> content
        self.deletes = []    # relpaths
        self.events = []
        self.stats = {"requests": 0, "n200": 0, "n304": 0, "added": 0,
                      "changed": 0, "removed": 0, "moved": 0, "errors": 0}
        self.error_msgs = []

    # ---------- staging ----------
    def stage_section(self, sid, doc):
        c = canonical(doc)
        if not is_valid(c):
            self.stats["errors"] += 1
            self.error_msgs.append(f"invalid section payload: {sid}")
            return
        data_rel = f"data/{self.slug}/{sid}.json"
        new = json.dumps(c, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
        old_file = ROOT / data_rel
        old = old_file.read_text() if old_file.exists() else None
        if new == old:
            return
        self.writes[data_rel] = new
        self.writes[f"view/{self.slug}/{sid}.md"] = to_view(c)
        etype = "changed" if old is not None else "added"
        self.stats[etype] += 1
        self.events.append({"ts": now_iso(), "type": etype, "slug": self.slug,
                            "section_id": sid, "title": c["title"],
                            "run_id": self.run_id})

    def stage_missing(self, sid, redirect_to=None):
        sec = self.manifest["sections"].get(sid)
        if not sec:
            return
        if redirect_to:
            sec["status"] = "moved"
            sec["redirect_to"] = redirect_to
            self.stats["moved"] += 1
            self.events.append({"ts": now_iso(), "type": "moved",
                                "slug": self.slug, "section_id": sid,
                                "redirect_to": redirect_to,
                                "run_id": self.run_id})
            return
        misses = sec.get("miss_count", 0) + 1
        sec["miss_count"] = misses
        if misses == 1:
            sec["status"] = "provisionally_missing"   # no event, no deletion yet
        else:                                          # confirmed on 2nd run
            sec["status"] = "tombstone"
            sec["removed_at"] = now_iso()
            # dangling contents links 404 without ever having been stored;
            # only a page we actually held warrants a "removed" event
            if (ROOT / f"data/{self.slug}/{sid}.json").exists():
                self.deletes += [f"data/{self.slug}/{sid}.json",
                                 f"view/{self.slug}/{sid}.md"]
                self.stats["removed"] += 1
                self.events.append({"ts": now_iso(), "type": "removed",
                                    "slug": self.slug, "section_id": sid,
                                    "last_path": sec.get("path"),
                                    "run_id": self.run_id})

    def get_section(self, sid, path, conditional=True):
        sec = self.manifest["sections"].setdefault(
            sid, {"path": path, "etag": None, "status": "live", "children": []})
        etag = sec.get("etag") if conditional else None
        status, doc, new_etag = fetch(path, etag)
        self.stats["requests"] += 1
        if status == 304:
            self.stats["n304"] += 1
            sec["last_ok"] = now_iso()
            sec["miss_count"] = 0
            return None
        if status == 404:
            self.stage_missing(sid, (doc or {}).get("redirect_to"))
            return None
        self.stats["n200"] += 1
        sec.update({"etag": new_etag, "status": "live", "last_ok": now_iso(),
                    "miss_count": 0, "path": path})
        self.stage_section(sid, doc)
        sec["children"] = [s for s, _ in children_of(canonical(doc))]
        return doc

    # ---------- flush ----------
    def flush(self):
        live = sum(1 for s in self.manifest["sections"].values()
                   if s["status"] == "live")
        if live and self.stats["removed"] > MASS_DELETE_FRACTION * live:
            raise RuntimeError(
                f"mass-deletion guard: {self.stats['removed']} removals "
                f"vs {live} live sections")
        for rel, content in self.writes.items():
            p = ROOT / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        for rel in self.deletes:
            (ROOT / rel).unlink(missing_ok=True)
        self.mpath.parent.mkdir(exist_ok=True)
        self.mpath.write_text(json.dumps(self.manifest, indent=1,
                                         sort_keys=True) + "\n")
        for e in self.events:
            events.append(e)


def root_path(slug):
    return f"/hmrc-internal-manuals/{slug}"


def process_root(m, conditional=True):
    """Fetch manual root. Returns (doc_or_None, new_note_candidates)."""
    status, doc, etag = fetch(root_path(m.slug),
                              m.manifest.get("root_etag") if conditional else None)
    m.stats["requests"] += 1
    if status == 304:
        m.stats["n304"] += 1
        return None, []
    if status == 404:
        raise RuntimeError(f"manual root missing: {m.slug}")
    m.stats["n200"] += 1
    m.manifest["root_etag"] = etag
    det = doc.get("details", {})
    notes = det.get("change_notes", [])
    wm, seen = m.manifest["watermark"], set(m.manifest["seen_note_keys"])
    cands = [n for n in notes
             if n.get("published_at", "") > wm
             or (n.get("published_at") == wm and note_key(n) not in seen)]
    if notes:
        new_wm = max(n.get("published_at", "") for n in notes)
        m.manifest["watermark"] = max(wm, new_wm)
        m.manifest["seen_note_keys"] = [
            note_key(n) for n in notes
            if n.get("published_at") == m.manifest["watermark"]]
    # root body/child groups are content too (stored as _manual)
    c = canonical(doc)
    c["section_id"] = c["section_id"] or "_manual"
    rel = f"data/{m.slug}/_manual.json"
    slim = {k: c[k] for k in ("title", "description", "base_path",
                              "public_updated_at", "child_section_groups")}
    new = json.dumps(slim, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    old_f = ROOT / rel
    if not old_f.exists() or old_f.read_text() != new:
        m.writes[rel] = new
    return doc, cands


def seed(slug):
    run_id = f"seed-{slug}-{now_iso()}"
    started = now_iso()
    m = Manual(slug, run_id)
    doc, _ = process_root(m, conditional=False)
    queue = children_of(canonical(doc))
    seen = set()
    while queue:
        sid, path = queue.pop(0)
        if sid in seen:
            continue
        seen.add(sid)
        d = m.get_section(sid, path, conditional=False)
        if d:
            queue.extend(children_of(canonical(d)))
    m.flush()
    runs.append({"run_id": run_id, "mode": "seed", "started": started,
                 "finished": now_iso(), "manuals": {slug: m.stats},
                 "errors": m.error_msgs, "scraper": SCRAPER_VERSION,
                 "converter": CONVERTER_VERSION})
    print(f"seeded {slug}: {m.stats}")
    return 0


def daily():
    run_id = f"daily-{now_iso()}"
    started = now_iso()
    slugs = yaml.safe_load((ROOT / "manuals.yml").read_text())["manuals"]
    shard = datetime.now(timezone.utc).toordinal() % SWEEP_SHARDS
    all_stats, failed = {}, []
    for slug in slugs:
        m = Manual(slug, run_id)
        try:
            doc, cands = process_root(m)
            # 1. change_notes-driven fetches (+ parent discovery for new pages)
            for n in cands:
                sid, path = n.get("section_id"), n.get("base_path")
                if not (sid and path):
                    continue
                is_new = sid not in m.manifest["sections"]
                d = m.get_section(sid, path, conditional=False)
                if d and is_new:
                    crumbs = canonical(d)["breadcrumbs"]
                    if crumbs:
                        parent = crumbs[-1]
                        psid = parent.get("section_id")
                        if psid and parent.get("base_path"):
                            m.get_section(psid, parent["base_path"],
                                          conditional=False)
            # 2. rolling ETag sweep — 1/7th of known live sections daily
            for sid, sec in sorted(m.manifest["sections"].items()):
                if sec["status"] in ("tombstone", "moved"):
                    continue
                if sweep_shard(sid) == shard:
                    m.get_section(sid, sec["path"])
            m.flush()
            all_stats[slug] = m.stats | {"status": "ok"}
            if m.error_msgs:
                all_stats[slug]["status"] = "warnings"
                all_stats[slug]["messages"] = m.error_msgs
        except Exception as exc:   # per-manual isolation: record, continue
            failed.append(slug)
            all_stats[slug] = m.stats | {"status": "failed", "error": str(exc)}
    runs.append({"run_id": run_id, "mode": "daily", "started": started,
                 "finished": now_iso(), "shard": shard, "manuals": all_stats,
                 "failed": failed, "scraper": SCRAPER_VERSION,
                 "converter": CONVERTER_VERSION})
    print(json.dumps(all_stats, indent=1))
    return 0        # never non-zero: commit must proceed; `health` gates the workflow


def health():
    last = runs.last()
    if not last:
        print("no runs recorded")
        return 1
    bad = last.get("failed") or []
    if bad:
        print(f"FAILED manuals in {last['run_id']}: {bad}")
        return 1
    print(f"last run ok: {last['run_id']}")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "seed" and len(sys.argv) == 3:
        sys.exit(seed(sys.argv[2]))
    if mode == "daily":
        sys.exit(daily())
    if mode == "health":
        sys.exit(health())
    print(__doc__)
    sys.exit(2)
