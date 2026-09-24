import time, tempfile, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobradar.store import Store
from jobradar.main import passes_filters

def job(i, company="Acme"):
    return {"id": i, "company": company, "title": f"Engineer {i}", "url": "u",
            "location": "Remote", "posted_at": None, "source": "greenhouse", "description": ""}

def run():
    s = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    assert len(s.upsert([job("a"), job("b")])) == 2
    assert len(s.upsert([job("a"), job("b")])) == 0, "re-seen jobs must not be new"
    time.sleep(0.02)
    s.upsert([job("a")])                                   # 'b' fell off the board
    assert [r["id"] for r in s.mark_closed(["Acme"])] == ["b"]
    assert len(s.since(3600)) == 1
    time.sleep(0.02)
    s.upsert([job("a"), job("b")])                         # 'b' reposted
    assert s.mark_closed(["Acme"]) == [], "reposted job must reopen"
    assert s.stats() == {"total": 2, "open": 2}

    # a company whose fetch failed must never have its jobs closed
    s.upsert([job("x", "Dead")])
    time.sleep(0.02)
    s.upsert([job("a"), job("b")])
    assert s.mark_closed(["Acme"]) == []                   # 'Dead' not in healthy list
    assert s.stats()["open"] == 3

    # notification bookkeeping
    s.save_score("a", 9, "great fit")
    ids = [r["id"] for r in s.unnotified(min_score=0)]
    assert set(ids) == {"a", "b", "x"}
    s.mark_notified(ids)
    assert s.unnotified(0) == [], "already-emailed jobs must not repeat"

    f = {"title_include": ["engineer"], "title_exclude": ["staff"], "locations": ["remote"]}
    assert passes_filters(job("a"), f)
    assert not passes_filters({**job("a"), "title": "Staff Engineer"}, f)
    assert not passes_filters({**job("a"), "title": "Chef"}, f)
    assert not passes_filters({**job("a"), "location": "Berlin"}, f)
    print("all store + filter tests PASS")

run()

def test_require_scored():
    s = Store(os.path.join(tempfile.mkdtemp(), "t2.db"))
    s.upsert([job("a"), job("b"), job("c")])
    s.save_score("a", 9, "great")
    s.save_score("b", 2, "weak")
    # 'c' overflowed max_to_score and is unscored
    assert [r["id"] for r in s.unnotified(0, require_scored=True)] == ["a", "b"], \
        "unscored jobs must not be emailed when a scorer is configured"
    assert [r["id"] for r in s.unnotified(6, require_scored=True)] == ["a"], \
        "min_score must still apply"
    assert len(s.unnotified(0, require_scored=False)) == 3, \
        "with scoring off, unscored jobs must still be reported"
    print("require_scored tests PASS")

test_require_scored()

def test_backlog_visible():
    s = Store(os.path.join(tempfile.mkdtemp(), "t3.db"))
    s.upsert([job(c) for c in "abcde"])
    assert s.unscored_count() == 5
    s.save_score("a", 7, "ok")
    assert s.unscored_count() == 4, "scored jobs must leave the backlog"
    assert len(s.unscored(2)) == 2, "max_to_score must cap the batch"
    # a re-scan that finds nothing new must still leave the backlog scoreable
    s.upsert([job(c) for c in "abcde"])
    assert s.unscored_count() == 4, "a no-op rescan must not hide the backlog"
    print("backlog tests PASS")

test_backlog_visible()
