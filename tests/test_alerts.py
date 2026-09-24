"""Instant alerts: the right jobs, once each, without eating the weekly queue."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobradar import alerts
from jobradar.store import Store

def job(i, company):
    return {"id": i, "company": company, "title": f"Engineer {i}", "url": "u",
            "location": "Remote", "posted_at": None, "source": "greenhouse",
            "description": "", "domain": "x.com"}

s = Store(os.path.join(tempfile.mkdtemp(), "a.db"))
s.upsert([job("a", "Adobe"), job("b", "Adobe"), job("c", "Stripe"), job("d", "Oracle")])
s.save_score("a", 9, "great", {})
s.save_score("b", 2, "weak", {})
s.save_score("c", 9, "great", {})

CFG = {"companies": [{"name": n} for n in ("Adobe", "Stripe", "Oracle")],
       "alerts": {"companies": ["Adobe", "Oracle"], "min_score": 6},
       "llm": {"min_score": 6}}

got = {r["id"] for r in alerts.pick(s.unalerted(), CFG)}
assert got == {"a", "d"}, got            # b below threshold, c not watched
# an unscored job at a watched company still alerts
assert "d" in got, "unscored jobs must not be silently withheld"

# an empty watch list means every configured company
allco = {r["id"] for r in alerts.pick(s.unalerted(), {**CFG, "alerts": {"min_score": 6}})}
assert allco == {"a", "c", "d"}, allco

# alerts and the weekly digest keep separate queues
assert len(s.unnotified(0)) == 4, "alerting must not consume the digest queue"
s.mark_alerted(["a", "b", "c", "d"])
assert s.unalerted() == [], "alerted jobs must not repeat"
assert len(s.unnotified(0)) == 4, "the digest queue is still intact"

# a fresh job alerts again
s.upsert([job("e", "Adobe")])
assert [r["id"] for r in s.unalerted()] == ["e"]

# rendering survives quotes and missing fields without crashing
html, text = alerts.render(s.unalerted(), "now")
assert "Engineer e" in html and "Engineer e" in text
alerts.notify_mac('Job "Radar"', 'a \\ weird "title"')   # must not raise
print("alert tests PASS")

# --- the daily alert must not re-scan the whole board ----------------------
import jobradar.main as M
seen = {}
def fake_scan(cfg, store, quiet=False):
    seen["companies"] = [e["name"] for e in cfg.get("companies", [])]
    seen["discovery"] = cfg.get("discovery", {}).get("enabled")
    return {"new": 0, "closed": 0, "errors": []}
M.cmd_scan = fake_scan
M.alerts_mod = alerts

CFG2 = {"companies": [{"name": n} for n in ("Adobe", "Stripe", "Oracle", "Figma")],
        "alerts": {"companies": ["Adobe", "Oracle"], "min_score": 6},
        "llm": {"min_score": 6}, "email": {}}
store2 = Store(os.path.join(tempfile.mkdtemp(), "b.db"))
M.cmd_alert(CFG2, store2, dry_run=True)
assert seen["companies"] == ["Adobe", "Oracle"], seen
assert seen["discovery"] is False, "aggregator discovery is wasted work for alerts"

# with no shortlist it falls back to every company
M.cmd_alert({**CFG2, "alerts": {"min_score": 6}}, store2, dry_run=True)
assert len(seen["companies"]) == 4, seen
print("alert scope tests PASS")

# --- adding a company must not fire its whole board ------------------------
st = Store(os.path.join(tempfile.mkdtemp(), "c.db"))
st.upsert([job(f"a{i}", "Adobe") for i in range(60)])
CFG3 = {"companies": [{"name": "Adobe"}], "alerts": {"min_score": 0}, "llm": {"min_score": 0}}

# first sighting: nothing alerts, because none of it is actually new to you
assert alerts.pick(st.unalerted(), CFG3, st.baselined_companies()) == []
st.mark_alerted([r["id"] for r in st.unalerted()])          # baseline recorded
assert "Adobe" in st.baselined_companies()

# from now on, a genuinely new opening does alert
st.upsert([job("a-new", "Adobe")])
got = alerts.pick(st.unalerted(), CFG3, st.baselined_companies())
assert [r["id"] for r in got] == ["a-new"], got

# a second company added later is baselined on its own schedule
st.upsert([job(f"o{i}", "Oracle") for i in range(40)])
CFG4 = {"companies": [{"name": "Adobe"}, {"name": "Oracle"}],
        "alerts": {"min_score": 0}, "llm": {"min_score": 0}}
got = {r["company"] for r in alerts.pick(st.unalerted(), CFG4, st.baselined_companies())}
assert got == {"Adobe"}, f"Oracle must baseline silently, got {got}"
print("baseline tests PASS")

# None must mean "no baseline check", never "silence everything"
assert len(alerts.pick(st.unalerted(), CFG4, None)) == len(st.unalerted()), \
    "omitting the baseline set must not silently disable alerts"
print("baseline-default tests PASS")
