#!/usr/bin/env python3
"""HMRC manual change tracker — scraper.

Modes:
  scrape.py seed <slug>     full walk of one manual (onboarding; one-off)
  scrape.py daily           change-note fetch + rolling sweep + discovery
  scrape.py health          exit non-zero if the latest run had failures

What "daily" means: manual roots and HMRC change notes are checked daily;
every known page is re-verified roughly weekly (1/7th per day, by section-id
hash). Unknown pages found on any changed structural page are fetched through
a persistent discovery queue. A silent HMRC edit may therefore take up to
seven days to surface. This is daily monitoring with weekly full verification,
not a daily full scrape.

Design: BUILD_PLAN.md v2. Git is the version store; this script only writes
files and ledgers — committing is the workflow's job. Events carry bounded
text patches so the static site does not require a full-history checkout.
"""
import difflib
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from markdownify import markdownify

ROOT = Path(os.environ.get(
    "HMRC_TRACKER_ROOT", Path(__file__).resolve().parent.parent))
API = "https://www.gov.uk/api/content"
UA = "hmrc-manual-tracker/0.2 (+contact: {})".format(
    os.environ.get("TRACKER_CONTACT", "repository issues page"))
THROTTLE = 0.5          # seconds between requests (~2 req/s)
SWEEP_SHARDS = 7        # every known page conditionally re-checked weekly
DISCOVERY_CAP = 500     # max discovery fetches per manual per daily run
MASS_DELETE_FRACTION = 0.10
MASS_DELETE_MIN = 3     # allow up to 3 removals before percentage guard applies
PATCH_MAX_CHARS = 20_000
SCRAPER_VERSION = "0.2.0"
CONVERTER_VERSION = "markdownify==1.2.3"  # pinned; upgrades = explicit regen commit

session = requests.Session()
session.headers["User-Agent"] = UA


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def text_patch(before, after, label):
    patch = "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"{label}@before", tofile=f"{label}@after", lineterm=""))
    if len(patch) > PATCH_MAX_CHARS:
        return patch[:PATCH_MAX_CHARS], True
    return patch, False


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
    """Source-faithful stable subset; volatile publishing fields are dropped."""
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


def is_valid_root(doc):
    details = doc.get("details")
    return bool(
        doc.get("title")
        and doc.get("base_path")
        and isinstance(details, dict)
        and isinstance(details.get("child_section_groups"), list)
        and isinstance(details.get("change_notes"), list)
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

    def append(self, obj):
        self.dir.mkdir(exist_ok=True)
        f = self.dir / (datetime.now(timezone.utc).strftime("%Y-%m") + ".jsonl")
        with f.open("a") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def last(self):
        files = sorted(self.dir.glob("*.jsonl")) if self.dir.exists() else []
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
        self.initial_live = sum(
            1 for sec in self.manifest["sections"].values()
            if sec.get("status") == "live")
        # Unknown pages found on structural pages remain queued across runs.
        self.queue = [
            tuple(item) for item in self.manifest.get("pending_discovery", [])]
        self.queued = {sid for sid, _ in self.queue}
        self.writes = {}     # relpath -> content
        self.deletes = []    # relpaths
        self.events = []
        self.stats = {"requests": 0, "n200": 0, "n304": 0, "added": 0,
                      "changed": 0, "removed": 0, "moved": 0,
                      "restored": 0, "discovered": 0, "errors": 0}
        self.error_msgs = []

    # ---------- discovery ----------
    def enqueue(self, sid, path):
        if sid not in self.manifest["sections"] and sid not in self.queued:
            self.queue.append((sid, path))
            self.queued.add(sid)

    def enqueue_unknown(self, c):
        for sid, path in children_of(c):
            self.enqueue(sid, path)

    def drain_discovery(self, cap=None, via="discovery"):
        fetched = 0
        while self.queue:
            if cap is not None and fetched >= cap:
                self.error_msgs.append(
                    f"discovery cap {cap} hit; {len(self.queue)} pending "
                    f"(carried to next run)")
                break
            sid, path = self.queue.pop(0)
            self.queued.discard(sid)
            if sid in self.manifest["sections"]:
                continue
            self.get_section(sid, path, conditional=False, via=via)
            fetched += 1
        self.stats["discovered"] += fetched

    # ---------- staging ----------
    def stage_section(self, sid, doc, via, previous_status):
        """Validate and stage a page, returning its canonical representation."""
        c = canonical(doc)
        if not is_valid(c):
            self.stats["errors"] += 1
            self.error_msgs.append(f"invalid section payload: {sid}")
            return None
        self.enqueue_unknown(c)
        data_rel = f"data/{self.slug}/{sid}.json"
        view_rel = f"view/{self.slug}/{sid}.md"
        new = json.dumps(c, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
        old_file = ROOT / data_rel
        old = old_file.read_text() if old_file.exists() else None
        new_view = to_view(c)
        old_view_file = ROOT / view_rel
        old_view = old_view_file.read_text() if old_view_file.exists() else ""
        restoring = previous_status in ("tombstone", "moved")

        if new == old and not restoring:
            return c

        self.writes[data_rel] = new
        self.writes[view_rel] = new_view
        if restoring:
            etype = "restored"
        elif old is None:
            etype = "added"
        else:
            etype = "changed"
        self.stats[etype] += 1
        patch, truncated = text_patch(old_view, new_view, sid)
        event = {
            "ts": now_iso(),
            "type": etype,
            "slug": self.slug,
            "section_id": sid,
            "title": c["title"],
            "run_id": self.run_id,
            "via": via,
            "after_sha": sha(new),
            "patch": patch,
        }
        if old is not None:
            event["before_sha"] = sha(old)
        if truncated:
            event["patch_truncated"] = True
        self.events.append(event)
        return c

    def stage_missing(self, sid, redirect_to=None, via="sweep"):
        sec = self.manifest["sections"].get(sid)
        if not sec:
            return
        if redirect_to:
            if (sec.get("status") == "moved"
                    and sec.get("redirect_to") == redirect_to):
                return
            sec["status"] = "moved"
            sec["redirect_to"] = redirect_to
            self.stats["moved"] += 1
            self.events.append({"ts": now_iso(), "type": "moved",
                                "slug": self.slug, "section_id": sid,
                                "redirect_to": redirect_to,
                                "run_id": self.run_id, "via": via})
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
            data_file = ROOT / f"data/{self.slug}/{sid}.json"
            view_file = ROOT / f"view/{self.slug}/{sid}.md"
            if data_file.exists():
                old = data_file.read_text()
                old_view = view_file.read_text() if view_file.exists() else ""
                patch, truncated = text_patch(old_view, "", sid)
                self.deletes += [f"data/{self.slug}/{sid}.json",
                                 f"view/{self.slug}/{sid}.md"]
                self.stats["removed"] += 1
                event = {
                    "ts": now_iso(),
                    "type": "removed",
                    "slug": self.slug,
                    "section_id": sid,
                    "last_path": sec.get("path"),
                    "run_id": self.run_id,
                    "via": via,
                    "before_sha": sha(old),
                    "patch": patch,
                }
                if truncated:
                    event["patch_truncated"] = True
                self.events.append(event)

    def get_section(self, sid, path, conditional=True, via="sweep"):
        sec = self.manifest["sections"].setdefault(
            sid, {"path": path, "etag": None, "status": "pending",
                  "children": []})
        previous_status = sec.get("status")
        # Never send stale ETags for missing, moved, or invalid pages.
        conditional = conditional and previous_status == "live"
        status, doc, new_etag = fetch(
            path, sec.get("etag") if conditional else None)
        self.stats["requests"] += 1
        if status == 304:
            self.stats["n304"] += 1
            sec["last_ok"] = now_iso()
            sec["miss_count"] = 0
            return None
        if status == 404:
            self.stage_missing(
                sid, (doc or {}).get("redirect_to"), via=via)
            return None
        self.stats["n200"] += 1
        c = self.stage_section(sid, doc, via, previous_status)
        if c is None:
            sec["status"] = "invalid"
            sec["etag"] = None
            return None
        sec.update({"etag": new_etag, "status": "live", "last_ok": now_iso(),
                    "miss_count": 0, "path": path})
        sec.pop("removed_at", None)
        sec.pop("redirect_to", None)
        sec["children"] = [s for s, _ in children_of(c)]
        return doc

    # ---------- flush ----------
    def flush(self):
        baseline = self.initial_live or sum(
            1 for sec in self.manifest["sections"].values()
            if sec.get("status") == "live")
        threshold = max(MASS_DELETE_MIN, MASS_DELETE_FRACTION * baseline)
        if self.stats["removed"] > threshold:
            raise RuntimeError(
                f"mass-deletion guard: {self.stats['removed']} removals "
                f"vs {baseline} baseline live sections "
                f"(threshold {threshold:.1f})")
        for rel, content in self.writes.items():
            p = ROOT / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        for rel in self.deletes:
            (ROOT / rel).unlink(missing_ok=True)
        self.manifest["pending_discovery"] = [
            list(item) for item in self.queue]
        self.mpath.parent.mkdir(exist_ok=True)
        self.mpath.write_text(json.dumps(self.manifest, indent=1,
                                         sort_keys=True) + "\n")
        for e in self.events:
            events.append(e)


def root_path(slug):
    return f"/hmrc-internal-manuals/{slug}"


def process_root(m, conditional=True):
    """Fetch the manual root and enqueue unknown top-level sections."""
    status, doc, etag = fetch(root_path(m.slug),
                              m.manifest.get("root_etag") if conditional else None)
    m.stats["requests"] += 1
    if status == 304:
        m.stats["n304"] += 1
        return None, []
    if status == 404:
        raise RuntimeError(f"manual root missing: {m.slug}")
    if not is_valid_root(doc):
        raise RuntimeError(f"invalid manual root payload: {m.slug}")
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
    c = canonical(doc)
    m.enqueue_unknown(c)
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
    process_root(m, conditional=False)
    m.drain_discovery(cap=None, via="seed")
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
        m = None
        try:
            m = Manual(slug, run_id)
            doc, cands = process_root(m)
            # 1. HMRC change-note candidates.
            for n in cands:
                sid, path = n.get("section_id"), n.get("base_path")
                if not (sid and path):
                    continue
                is_new = sid not in m.manifest["sections"]
                d = m.get_section(
                    sid, path, conditional=False, via="change_note")
                if d and is_new:
                    crumbs = canonical(d)["breadcrumbs"]
                    if crumbs:
                        parent = crumbs[-1]
                        psid = parent.get("section_id")
                        if psid and parent.get("base_path"):
                            m.get_section(psid, parent["base_path"],
                                          conditional=False,
                                          via="change_note")
            # 2. Rolling verification. Invalid pages retry on every run;
            # all other known pages, including tombstones/moves, are sharded.
            for sid, sec in sorted(list(m.manifest["sections"].items())):
                if (sec.get("status") == "invalid"
                        or sweep_shard(sid) == shard):
                    m.get_section(sid, sec["path"], via="sweep")
            # 3. Fetch unknown children found on structural 200 responses.
            m.drain_discovery(cap=DISCOVERY_CAP)
            m.flush()
            all_stats[slug] = m.stats | {"status": "ok"}
            if m.error_msgs:
                all_stats[slug]["status"] = "warnings"
                all_stats[slug]["messages"] = m.error_msgs
        except Exception as exc:   # per-manual isolation: record, continue
            failed.append(slug)
            stats = m.stats if m is not None else {
                "requests": 0, "n200": 0, "n304": 0, "added": 0,
                "changed": 0, "removed": 0, "moved": 0, "restored": 0,
                "discovered": 0, "errors": 0,
            }
            all_stats[slug] = stats | {
                "status": "failed", "error": str(exc)}
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
    bad = list(last.get("failed") or [])
    # Validation failures skip or retain stale content and are therefore
    # integrity failures, not tolerable warnings.
    for slug, stats in (last.get("manuals") or {}).items():
        if stats.get("errors"):
            bad.append(f"{slug} ({stats['errors']} validation errors)")
    if bad:
        print(f"UNHEALTHY run {last['run_id']}: {bad}")
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
