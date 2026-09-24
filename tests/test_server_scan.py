"""A scan driven from the web UI must complete. This caught a real bug: the
SSE generator opened the database on the request thread and handed it to a
worker thread, which sqlite refuses - so every Scan died at the first write."""
import json, os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobradar.web.server as srv

# stand in for the network so the test is fast and offline
FAKE = [{"id": f"j{i}", "company": "Acme", "title": f"Engineer {i}", "url": "u",
         "location": "Remote", "posted_at": None, "source": "greenhouse",
         "description": "python react", "domain": "acme.com"} for i in range(5)]
srv.sources.fetch_company = lambda entry: (FAKE, None)
srv.sources.discover = lambda q, n: ([], [])
srv.cfg = lambda: {
    "companies": [{"name": "Acme", "board": "greenhouse", "slug": "acme"}],
    "discovery": {"enabled": False},
    "filters": {},
    "llm": {"provider": "none"},
    "resume_path": "resume.example.txt",
}

import tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp()) / "t.db"
srv.store = lambda: __import__("jobradar.store", fromlist=["Store"]).Store(tmp)

types = []
for chunk in srv._scan_events(limit=10):
    for line in chunk.splitlines():
        if line.startswith("data: "):
            d = json.loads(line[6:])
            types.append(d["type"])
            assert d["type"] != "error", f"scan errored: {d.get('message')}"

assert "fetched" in types, types
assert types[-1] == "done", f"stream must end with done, got {types[-1]}: {types}"
assert srv._state["scanning"] is False, "scanning flag must reset"

# and the write actually landed, from the worker thread
from jobradar.store import Store
assert Store(tmp).stats()["total"] == 5, "jobs were not persisted"

# a second concurrent scan is refused rather than corrupting state
srv._state["scanning"] = True
out = list(srv._scan_events(limit=10))
assert "already running" in out[0], out
srv._state["scanning"] = False
print("server scan tests PASS")

# --- an expired posting must never reach the page --------------------------
from jobradar.store import Store as _S
st = _S(tmp)
st.upsert([{"id": "gone", "company": "Acme", "title": "Closed Role", "url": "u",
            "location": "", "posted_at": None, "source": "greenhouse",
            "description": "", "domain": ""}])
import time as _t
_t.sleep(0.02)
st.upsert([{"id": f"j{i}", "company": "Acme", "title": f"Engineer {i}", "url": "u",
            "location": "", "posted_at": None, "source": "greenhouse",
            "description": "", "domain": ""} for i in range(5)])
closed = st.mark_closed(["Acme"])
assert [r["id"] for r in closed] == ["gone"], closed

srv.store = lambda: _S(tmp)
state = srv.state()
ids = {j["id"] for j in state["jobs"]}
assert "gone" not in ids, "closed jobs must not appear on the grid"
assert state["stats"]["closed"] == 1, state["stats"]
assert state["stats"]["total"] == 5, state["stats"]
print("expired-job filtering PASS")
