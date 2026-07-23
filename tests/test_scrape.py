"""Ingestion state-machine tests using a fake API and temporary repository."""

import importlib
import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

SLUG = "test-manual"
BASE = f"/hmrc-internal-manuals/{SLUG}"


def sec_doc(sid, body="<p>text</p>", children=(), title=None, breadcrumbs=()):
    return {
        "title": title or f"Title {sid}",
        "description": "",
        "base_path": f"{BASE}/{sid.lower()}",
        "public_updated_at": "2026-01-01T00:00:00Z",
        "details": {
            "section_id": sid,
            "body": body,
            "breadcrumbs": [
                {"section_id": parent,
                 "base_path": f"{BASE}/{parent.lower()}"}
                for parent in breadcrumbs
            ],
            "child_section_groups": [{
                "child_sections": [
                    {
                        "section_id": child,
                        "base_path": f"{BASE}/{child.lower()}",
                        "title": f"Title {child}",
                        "description": "",
                    }
                    for child in children
                ]
            }] if children else [],
        },
    }


def root_doc(children=(), notes=(), slug=SLUG):
    return {
        "title": "Test Manual",
        "description": "",
        "base_path": f"/hmrc-internal-manuals/{slug}",
        "public_updated_at": "2026-01-01T00:00:00Z",
        "details": {
            "body": "",
            "breadcrumbs": [],
            "change_notes": list(notes),
            "child_section_groups": [{
                "child_sections": [
                    {
                        "section_id": child,
                        "base_path":
                            f"/hmrc-internal-manuals/{slug}/{child.lower()}",
                        "title": f"Title {child}",
                        "description": "",
                    }
                    for child in children
                ]
            }] if children else [],
        },
    }


def note(sid, published_at, text="updated"):
    return {
        "section_id": sid,
        "base_path": f"{BASE}/{sid.lower()}",
        "published_at": published_at,
        "change_note": text,
        "title": f"Title {sid}",
    }


class FakeAPI:
    """Map API paths to documents and emulate ETags, 404s, and redirects."""

    def __init__(self):
        self.pages = {}
        self.calls = []

    def set(self, path, doc, etag="e1"):
        self.pages[path] = (doc, etag)

    def set_sec(self, doc, etag="e1"):
        self.pages[doc["base_path"]] = (doc, etag)

    def remove(self, path):
        self.pages.pop(path, None)

    def redirect(self, path, destination):
        self.pages[path] = ({"_redirect": destination}, None)

    def __call__(self, path, etag=None):
        self.calls.append(path)
        if path not in self.pages:
            return 404, None, None
        doc, current_etag = self.pages[path]
        if isinstance(doc, dict) and "_redirect" in doc:
            return 404, {"redirect_to": doc["_redirect"]}, None
        if etag is not None and etag == current_etag:
            return 304, None, current_etag
        return 200, doc, current_etag


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HMRC_TRACKER_ROOT", str(tmp_path))
    (tmp_path / "manuals.yml").write_text(f"manuals:\n  - {SLUG}\n")
    import scrape

    importlib.reload(scrape)
    api = FakeAPI()
    monkeypatch.setattr(scrape, "fetch", api)
    monkeypatch.setattr(scrape, "SWEEP_SHARDS", 1)
    return scrape, api, tmp_path


def seed_basic(scrape, api, children=("AAA100", "AAA200")):
    api.set(BASE, root_doc(children=children))
    for child in children:
        api.set_sec(sec_doc(child))
    assert scrape.seed(SLUG) == 0


def read_jsonl(root, dirname):
    records = []
    path = root / dirname
    if not path.exists():
        return records
    for file in sorted(path.glob("*.jsonl")):
        records.extend(json.loads(line) for line in file.read_text().splitlines())
    return records


def read_events(root):
    return read_jsonl(root, "events")


def last_run(root):
    return read_jsonl(root, "runs")[-1]


def manifest(root):
    return json.loads(
        (root / "manifest" / f"{SLUG}.json").read_text())


def test_empty_body_contents_page_is_stored(env):
    scrape, api, root = env
    api.set(BASE, root_doc(children=("CONT1",)))
    api.set_sec(sec_doc("CONT1", body="", children=("LEAF1",)))
    api.set_sec(sec_doc("LEAF1"))

    scrape.seed(SLUG)

    assert (root / "data" / SLUG / "CONT1.json").exists()
    assert (root / "data" / SLUG / "LEAF1.json").exists()
    content = json.loads(
        (root / "data" / SLUG / "CONT1.json").read_text())
    assert (
        content["child_section_groups"][0]["child_sections"][0]["section_id"]
        == "LEAF1"
    )


def test_unchanged_etags_produce_no_events(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    before = len(read_events(root))

    scrape.daily()

    assert len(read_events(root)) == before
    stats = last_run(root)["manuals"][SLUG]
    assert stats["n200"] == 0
    assert stats["n304"] >= 3


def test_changed_page_yields_self_contained_event(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    api.set_sec(sec_doc("AAA100", body="<p>reworded text</p>"), etag="e2")
    api.set(BASE, root_doc(children=("AAA100", "AAA200")), etag="e2")

    scrape.daily()

    changes = [event for event in read_events(root)
               if event["type"] == "changed"]
    assert len(changes) == 1
    event = changes[0]
    assert event["section_id"] == "AAA100"
    assert event["via"] == "sweep"
    assert event["before_sha"] != event["after_sha"]
    assert "+reworded text" in event["patch"]
    assert "-text" in event["patch"]


def test_unknown_child_on_known_parent_is_discovered(env):
    scrape, api, root = env
    seed_basic(scrape, api, children=("PAR100",))
    api.set_sec(sec_doc("PAR100", children=("NEW200",)), etag="e2")
    api.set_sec(sec_doc("NEW200"))

    scrape.daily()

    assert (root / "data" / SLUG / "NEW200.json").exists()
    additions = [
        event for event in read_events(root)
        if event["section_id"] == "NEW200" and event["type"] == "added"
    ]
    assert additions
    assert additions[0]["via"] == "discovery"


def test_unknown_top_level_child_without_change_note_is_discovered(env):
    scrape, api, root = env
    seed_basic(scrape, api, children=("AAA100",))
    api.set(BASE, root_doc(children=("AAA100", "NEW100")), etag="e2")
    api.set_sec(sec_doc("NEW100"))

    scrape.daily()

    assert (root / "data" / SLUG / "NEW100.json").exists()


def test_new_contents_page_with_descendants_is_fully_ingested(env):
    scrape, api, root = env
    seed_basic(scrape, api, children=("AAA100",))
    api.set(
        BASE,
        root_doc(
            children=("AAA100", "BR100"),
            notes=(note("BR100", "2027-01-01T00:00:00Z"),),
        ),
        etag="e2",
    )
    api.set_sec(sec_doc("BR100", body="", children=("BR110", "BR120")))
    api.set_sec(sec_doc("BR110", children=("BR111",)))
    api.set_sec(sec_doc("BR111"))
    api.set_sec(sec_doc("BR120"))

    scrape.daily()

    for sid in ("BR100", "BR110", "BR111", "BR120"):
        assert (root / "data" / SLUG / f"{sid}.json").exists()


def test_404_is_provisional_then_self_contained_removal(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    api.remove(f"{BASE}/aaa200")

    scrape.daily()

    assert manifest(root)["sections"]["AAA200"]["status"] == (
        "provisionally_missing")
    assert (root / "data" / SLUG / "AAA200.json").exists()
    assert not [event for event in read_events(root)
                if event["type"] == "removed"]

    scrape.daily()

    assert manifest(root)["sections"]["AAA200"]["status"] == "tombstone"
    assert not (root / "data" / SLUG / "AAA200.json").exists()
    removals = [event for event in read_events(root)
                if event["type"] == "removed"]
    assert len(removals) == 1
    assert removals[0]["section_id"] == "AAA200"
    assert removals[0]["via"] == "sweep"
    assert removals[0]["before_sha"]
    assert "-text" in removals[0]["patch"]


def test_dangling_link_tombstones_without_removal_event(env):
    scrape, api, root = env
    api.set(BASE, root_doc(children=("GHOST1",)))

    scrape.seed(SLUG)
    scrape.daily()

    assert manifest(root)["sections"]["GHOST1"]["status"] == "tombstone"
    assert not [event for event in read_events(root)
                if event["type"] == "removed"]


def test_repeated_redirect_is_deduplicated_and_restoration_is_single_event(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    api.redirect(f"{BASE}/aaa200", f"{BASE}/elsewhere")

    scrape.daily()
    scrape.daily()

    moves = [event for event in read_events(root)
             if event["type"] == "moved"]
    assert len(moves) == 1
    assert moves[0]["via"] == "sweep"

    api.set_sec(sec_doc("AAA200"), etag="e9")
    scrape.daily()

    relevant = [
        event for event in read_events(root)
        if event["section_id"] == "AAA200" and event["run_id"].startswith("daily")
    ]
    assert len([event for event in relevant
                if event["type"] == "restored"]) == 1
    assert not [event for event in relevant if event["type"] == "added"]
    assert manifest(root)["sections"]["AAA200"]["status"] == "live"


def test_tombstone_restoration_does_not_emit_added_event(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    api.remove(f"{BASE}/aaa200")
    scrape.daily()
    scrape.daily()
    api.set_sec(sec_doc("AAA200"), etag="e9")

    scrape.daily()

    latest_id = last_run(root)["run_id"]
    latest = [event for event in read_events(root)
              if event["run_id"] == latest_id]
    assert [event for event in latest if event["type"] == "restored"]
    assert not [event for event in latest if event["type"] == "added"]


def test_mass_deletion_guard_uses_pre_run_baseline(env):
    scrape, api, root = env
    children = tuple(f"S{i}00" for i in range(1, 11))
    seed_basic(scrape, api, children=children)
    for sid in children[:5]:
        api.remove(f"{BASE}/{sid.lower()}")

    scrape.daily()
    scrape.daily()

    run = last_run(root)
    assert SLUG in run["failed"]
    assert "mass-deletion guard" in run["manuals"][SLUG]["error"]
    for sid in children[:5]:
        assert (root / "data" / SLUG / f"{sid}.json").exists()


def test_invalid_existing_payload_fails_health_and_retains_copy(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    invalid = sec_doc("AAA100")
    del invalid["details"]["section_id"]
    api.set_sec(invalid, etag="e2")

    scrape.daily()

    run = last_run(root)
    assert run["manuals"][SLUG]["errors"] == 1
    assert run["manuals"][SLUG]["status"] == "warnings"
    assert scrape.health() == 1
    assert (root / "data" / SLUG / "AAA100.json").exists()
    assert manifest(root)["sections"]["AAA100"]["status"] == "invalid"


def test_invalid_discovery_retries_until_valid(env):
    scrape, api, root = env
    seed_basic(scrape, api, children=("PAR100",))
    api.set_sec(sec_doc("PAR100", children=("BAD200",)), etag="e2")
    invalid = sec_doc("BAD200")
    del invalid["details"]["section_id"]
    api.set_sec(invalid)

    scrape.daily()

    assert manifest(root)["sections"]["BAD200"]["status"] == "invalid"
    assert not (root / "data" / SLUG / "BAD200.json").exists()
    assert scrape.health() == 1

    api.set_sec(sec_doc("BAD200"), etag="e2")
    scrape.daily()

    assert manifest(root)["sections"]["BAD200"]["status"] == "live"
    assert (root / "data" / SLUG / "BAD200.json").exists()
    assert scrape.health() == 0


def test_one_manual_failure_does_not_block_other_manual(env):
    scrape, api, root = env
    other = "other-manual"
    (root / "manuals.yml").write_text(
        f"manuals:\n  - {SLUG}\n  - {other}\n")
    seed_basic(scrape, api)
    api.remove(BASE)
    api.set(
        f"/hmrc-internal-manuals/{other}",
        root_doc(children=("OTH100",), slug=other),
    )
    other_page = sec_doc("OTH100")
    other_page["base_path"] = (
        f"/hmrc-internal-manuals/{other}/oth100")
    api.set(other_page["base_path"], other_page)

    scrape.daily()

    run = last_run(root)
    assert run["failed"] == [SLUG]
    assert run["manuals"][other]["status"] == "ok"
    assert (root / "data" / other / "OTH100.json").exists()
    assert scrape.health() == 1


def test_invalid_manual_root_fails_without_overwriting_baseline(env):
    scrape, api, root = env
    seed_basic(scrape, api)
    baseline = (root / "data" / SLUG / "_manual.json").read_text()
    invalid_root = root_doc(children=("AAA100", "AAA200"))
    del invalid_root["details"]["child_section_groups"]
    api.set(BASE, invalid_root, etag="e2")

    scrape.daily()

    run = last_run(root)
    assert run["failed"] == [SLUG]
    assert "invalid manual root payload" in run["manuals"][SLUG]["error"]
    assert (root / "data" / SLUG / "_manual.json").read_text() == baseline
    assert scrape.health() == 1


def test_corrupt_manifest_is_isolated_from_other_manual(env):
    scrape, api, root = env
    other = "other-manual"
    (root / "manuals.yml").write_text(
        f"manuals:\n  - {SLUG}\n  - {other}\n")
    seed_basic(scrape, api)
    (root / "manifest" / f"{SLUG}.json").write_text("{not-json")
    api.set(
        f"/hmrc-internal-manuals/{other}",
        root_doc(children=("OTH100",), slug=other),
    )
    other_page = sec_doc("OTH100")
    other_page["base_path"] = (
        f"/hmrc-internal-manuals/{other}/oth100")
    api.set(other_page["base_path"], other_page)

    scrape.daily()

    run = last_run(root)
    assert run["failed"] == [SLUG]
    assert run["manuals"][other]["status"] == "ok"
    assert (root / "data" / other / "OTH100.json").exists()


def test_watermark_deduplicates_notes_with_same_timestamp(env):
    scrape, api, root = env
    timestamp = "2027-03-01T00:00:00Z"
    api.set(
        BASE,
        root_doc(
            children=("AAA100", "AAA200"),
            notes=(
                note("AAA100", timestamp, "first"),
                note("AAA200", timestamp, "second"),
            ),
        ),
    )
    for child in ("AAA100", "AAA200"):
        api.set_sec(sec_doc(child))
    scrape.seed(SLUG)
    api.set(
        BASE,
        root_doc(
            children=("AAA100", "AAA200"),
            notes=(
                note("AAA100", timestamp, "first"),
                note("AAA200", timestamp, "second"),
                note("AAA200", timestamp, "third"),
            ),
        ),
        etag="e2",
    )
    api.calls.clear()

    scrape.daily()

    candidate_fetches = [
        path for path in api.calls if path == f"{BASE}/aaa200"]
    assert len(candidate_fetches) == 2  # change note plus sweep
    assert len(manifest(root)["seen_note_keys"]) == 3


def test_discovery_cap_persists_queue(env, monkeypatch):
    scrape, api, root = env
    seed_basic(scrape, api, children=("PAR100",))
    monkeypatch.setattr(scrape, "DISCOVERY_CAP", 1)
    children = ("N1000", "N2000", "N3000")
    api.set_sec(sec_doc("PAR100", children=children), etag="e2")
    for child in children:
        api.set_sec(sec_doc(child))

    scrape.daily()

    assert len(manifest(root)["pending_discovery"]) == 2
    scrape.daily()
    scrape.daily()
    assert manifest(root)["pending_discovery"] == []
    for child in children:
        assert (root / "data" / SLUG / f"{child}.json").exists()


def test_seed_events_are_identifiable_as_baseline(env):
    scrape, api, root = env
    seed_basic(scrape, api)

    seed_events = read_events(root)

    assert seed_events
    assert all(event["via"] == "seed" for event in seed_events)
    assert all(event["patch"] for event in seed_events)
