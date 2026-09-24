const $ = (id) => document.getElementById(id);
const tiles = new Map();          // job id -> tile element
let jobs = [];
let scoringOn = true;
let t0 = 0, timer = null;

// key -> the plain-language claim the bar is measuring.
const CHECKS = [
  ["skills",    "you know the stack"],
  ["seniority", "level fits you"],
  ["location",  "you can work there"],
  ["domain",    "field you've worked in"],
  ["recency",   "experience is current"],
  ["reach",     "realistic to land"],
];

function bucket(score) {
  if (score === null || score === undefined) return "";
  return "s" + Math.min(5, Math.max(1, Math.ceil(score / 2)));
}

function logo(domain) {
  return domain ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64` : "";
}

function attachLogo(el, job, px) {
  // Google 404s with a generic globe for domains it has no icon for, so a
  // failed load falls back to the company's initials rather than a broken
  // image or a globe that says nothing.
  const src = logo(job.domain);
  if (!src) { addInitial(el, job.company); return; }
  const img = new Image();
  img.src = src;
  img.alt = "";
  if (px) { img.width = px; img.height = px; }
  img.onerror = () => { img.remove(); addInitial(el, job.company); };
  el.appendChild(img);
}

function makeTile(job) {
  const el = document.createElement("div");
  el.className = "tile " + bucket(job.score);
  el.title = `${job.title} — ${job.company}` + (job.score !== null ? ` · ${job.score}/10` : "");
  attachLogo(el, job);
  el.onclick = () => { if (job.url) window.open(job.url, "_blank", "noopener"); };
  return el;
}

function addInitial(el, company) {
  const s = document.createElement("span");
  s.className = "init";
  s.textContent = (company || "?").slice(0, 2).toUpperCase();
  el.appendChild(s);
}

// --- filtering -------------------------------------------------------------
const WEEK = 7 * 24 * 3600;

function currentFilter() {
  return {
    q: ($("q").value || "").trim().toLowerCase(),
    min: Number($("minscore").value || 0),
    company: $("co").value || "",
    newOnly: $("newonly").checked,
  };
}

// Ids matching the current text query, resolved server-side so descriptions
// are searchable without shipping megabytes of them to the browser.
// null means "no query running", not "nothing matched".
let searchIds = null;

function matches(job, f) {
  if (f.company && job.company !== f.company) return false;
  if (f.min && (job.score === null || job.score === undefined || job.score < f.min)) return false;
  if (f.newOnly && (!job.first_seen || Date.now() / 1000 - job.first_seen > WEEK)) return false;
  if (f.q && searchIds && !searchIds.has(job.id)) return false;
  return true;
}

async function runSearch() {
  const q = ($("q").value || "").trim();
  if (!q) { searchIds = null; renderGrid(); return; }
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const d = await r.json();
    searchIds = d.ids === null ? null : new Set(d.ids);
  } catch {
    searchIds = null;        // a failed lookup shows everything, not nothing
  }
  renderGrid();
}

function renderGrid() {
  const grid = $("grid");
  const f = currentFilter();
  grid.textContent = "";
  tiles.clear();
  let shown = 0;
  for (const j of jobs) {
    if (!matches(j, f)) continue;
    const el = makeTile(j);
    tiles.set(j.id, el);
    grid.appendChild(el);
    shown++;
  }
  const filtered = f.q || f.min || f.company || f.newOnly;
  if (!jobs.length) {
    $("gridnote").textContent = "no jobs yet — press Scan";
  } else if (!shown) {
    $("gridnote").textContent = "nothing matches — clear the filters";
  } else if (f.q && !searchIds) {
    $("gridnote").textContent = `${shown} jobs · searching…`;
  } else {
    $("gridnote").textContent =
      (filtered ? `${shown} of ${jobs.length} jobs` : `${jobs.length} jobs`) +
      " · click a tile to open it";
  }
}

function fillCompanies() {
  const sel = $("co");
  const chosen = sel.value;
  const names = [...new Set(jobs.map((j) => j.company))].sort();
  sel.textContent = "";
  const all = document.createElement("option");
  all.value = ""; all.textContent = "all companies";
  sel.appendChild(all);
  for (const n of names) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  sel.value = chosen;
}

let debounce = null;
function onFilterChange() {
  clearTimeout(debounce);
  debounce = setTimeout(runSearch, 220);
}

function renderStats(s) {
  $("s-checked").textContent = s.checked;
  $("s-total").textContent = s.total;
  $("s-hire").textContent = s.would_hire;
  $("s-avg").textContent = Number(s.avg).toFixed(2);
}

function renderTops() {
  const scored = jobs.filter((j) => j.score !== null && j.score !== undefined);
  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, 5);
  const box = $("tops");
  box.textContent = "";
  if (!top.length) {
    box.innerHTML = `<div class="muted">${
      scoringOn ? "nothing scored yet" : "ranking needs a model"}</div>`;
    return;
  }
  for (const j of top) {
    const a = document.createElement("a");
    a.className = "top"; a.href = j.url || "#"; a.target = "_blank"; a.rel = "noopener";
    a.title = `${j.title} — ${j.company}` + (j.reason ? `\n${j.reason}` : "");
    const mark = document.createElement("div");
    mark.className = "toplogo";
    attachLogo(mark, j, 24);
    a.appendChild(mark);
    const pct = document.createElement("div");
    pct.className = "pct"; pct.textContent = `${j.score * 10}%`;
    const co = document.createElement("div");
    co.className = "co"; co.textContent = j.company;
    a.append(pct, co);
    box.appendChild(a);
  }
}

function renderChecks(job) {
  const ul = $("checks");
  ul.textContent = "";
  if (!scoringOn) {
    ul.innerHTML = '<li class="offnote">Scoring is off. Openings, closures and the ' +
      'weekly email still work — set <code>llm.provider</code> in config.yaml to rank them.</li>';
    return;
  }
  // Values come from the model's own per-criterion judgement. If a job has no
  // breakdown the bars read zero rather than inventing a number.
  let bd = {};
  if (job && job.breakdown) {
    bd = typeof job.breakdown === "string" ? safeParse(job.breakdown) : job.breakdown;
  }
  for (const [key, claim] of CHECKS) {
    const val = Math.max(0, Math.min(100, Number(bd[key]) || 0));
    const li = document.createElement("li");
    li.innerHTML = `<span class="k">${key}</span>
      <span class="claim">${claim}</span>
      <span class="bar"><i style="width:${val}%"></i></span>
      <span class="n">${val}</span>`;
    ul.appendChild(li);
  }
}

function safeParse(s) { try { return JSON.parse(s) || {}; } catch { return {}; } }

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadState() {
  const r = await fetch("/api/state");
  const d = await r.json();
  jobs = d.jobs;
  scoringOn = d.scoring !== false;
  fillCompanies();
  renderGrid(); renderStats(d.stats); renderTops();
  renderChecks(jobs.find((j) => j.score !== null) || null);
}

async function loadProfile() {
  const d = await fetch("/api/profile").then((r) => r.json());
  if (d.name) {
    $("p-name").textContent = d.name;
    $("who").textContent = d.name.split(" ")[0];
    $("avatar").textContent = d.name.split(" ").map((w) => w[0]).slice(0, 2).join("");
  }
  $("p-headline").textContent = d.headline || "";
  $("p-location").textContent = d.location || "";
  const ul = $("exp");
  ul.textContent = "";
  if (!(d.experience || []).length) {
    ul.innerHTML = `<li class="muted">${escapeHtml(d.error || "no resume parsed")}</li>`;
    return;
  }
  for (const e of d.experience) {
    const li = document.createElement("li");
    li.innerHTML = `<div><div class="org">${escapeHtml(e.org)}</div>
                    <div class="role">${escapeHtml(e.role)}</div></div>
                    <div class="yr">${escapeHtml(e.years)}</div>`;
    ul.appendChild(li);
  }
}

let lit = [];
function clearLit() {
  for (const el of lit) el.classList.remove("active");
  lit = [];
}

function highlightCompany(company) {
  clearLit();
  let first = null;
  for (const j of jobs) {
    if (j.company !== company) continue;
    const el = tiles.get(j.id);
    if (!el) continue;
    el.classList.add("active");
    lit.push(el);
    if (!first) first = el;
    if (lit.length >= 40) break;
  }
  if (first) first.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function startScan() {
  const btn = $("scan");
  btn.disabled = true; btn.textContent = "Scanning…";
  t0 = Date.now();
  timer = setInterval(() => {
    $("s-time").textContent = ((Date.now() - t0) / 1000).toFixed(1);
  }, 100);

  const es = new EventSource("/api/scan?limit=3000");
  let active = null;

  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.type === "fetch") {
      $("lk-company").textContent = d.company;
      $("lk-title").textContent = d.error ? "fetch failed" : `${d.count} open`;
      $("lk-score").textContent = "";
      // Fetching is the long half of a scan. Light up that company's tiles so
      // the grid shows progress instead of sitting still for two minutes.
      highlightCompany(d.company);
    } else if (d.type === "fetched") {
      loadState();
      $("gridnote").textContent = `${d.new} new · ${d.closed} closed · ${d.total} tracked`;
    } else if (d.type === "scoring") {
      $("gridnote").textContent = `scoring ${d.count} of ${d.backlog} unscored`;
    } else if (d.type === "looking") {
      clearLit();
      active = tiles.get(d.id) || null;
      if (active) {
        active.classList.add("active");
        lit.push(active);
        active.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      $("lk-company").textContent = d.company;
      $("lk-title").textContent = d.title;
      $("lk-score").textContent = "…";
    } else if (d.type === "scored") {
      const el = tiles.get(d.id);
      if (el) {
        el.className = "tile " + bucket(d.score);
        el.title = `${d.title} — ${d.company} · ${d.score}/10 — ${d.reason || ""}`;
      }
      const j = jobs.find((x) => x.id === d.id);
      if (j) { j.score = d.score; j.reason = d.reason; j.breakdown = d.breakdown || {}; }
      $("lk-score").textContent = (d.score / 10).toFixed(2);
      renderChecks(j || null);
      renderTops();
      const scored = jobs.filter((x) => x.score !== null && x.score !== undefined);
      renderStats({
        checked: scored.length, total: jobs.length,
        would_hire: scored.filter((x) => x.score >= 6).length,
        avg: scored.length ? scored.reduce((a, b) => a + b.score, 0) / scored.length / 10 : 0,
      });
    } else if (d.type === "throttled") {
      $("gridnote").textContent = `rate limited after ${d.scored} — press Scan again to continue`;
    } else if (d.type === "error") {
      $("gridnote").textContent = d.message;
    } else if (d.type === "done") {
      // Score state only lands in `jobs` via the stream; reload first so the
      // finale ranks the full board, not just this run's batch.
      loadState().then(showMatchMade);
      $("s-time").textContent = d.elapsed.toFixed(1);
      $("lk-company").textContent = "idle"; $("lk-title").textContent = ""; $("lk-score").textContent = "";
      clearLit();
      es.close(); clearInterval(timer);
      btn.disabled = false; btn.textContent = "Scan";
      loadState();
    }
  };
  es.onerror = () => {
    clearLit();
    es.close(); clearInterval(timer);
    btn.disabled = false; btn.textContent = "Scan";
  };
}

function showMatchMade() {
  const scored = jobs.filter((j) => j.score !== null && j.score !== undefined);
  if (!scored.length) return;
  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, 5);

  const name = $("p-name").textContent;
  const initials = $("avatar").textContent;
  const cards = top.map((j) => `
    <a class="mm-card" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener"
       title="${escapeHtml(j.title)} — ${escapeHtml(j.reason || "")}">
      <div class="mm-tile" data-domain="${escapeHtml(j.domain || "")}"
           data-company="${escapeHtml(j.company || "?")}"></div>
      <div class="mm-pct">${j.score * 10}%</div>
      <div class="mm-co">${escapeHtml(j.company)}</div>
    </a>`).join("");

  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `
    <div class="mm">
      <div class="mm-profile">
        <div class="mm-av">${escapeHtml(initials)}</div>
        <div><div class="kicker">Profile</div>
             <div class="pname">${escapeHtml(name)}</div>
             <div class="pmeta">${escapeHtml($("p-headline").textContent)}</div></div>
      </div>
      <div class="kicker mm-kicker">Match made</div>
      <h2 class="mm-head">High probability of interviewing you</h2>
      <div class="mm-cards">${cards}</div>
      <button class="mm-close">Back to the grid</button>
    </div>`;
  document.body.appendChild(ov);
  for (const slot of ov.querySelectorAll(".mm-tile")) {
    attachLogo(slot, { domain: slot.dataset.domain, company: slot.dataset.company }, 56);
  }
  requestAnimationFrame(() => ov.classList.add("show"));
  const close = () => ov.remove();
  ov.querySelector(".mm-close").onclick = close;
  ov.onclick = (e) => { if (e.target === ov) close(); };
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
  });
}

$("scan").onclick = startScan;
$("q").addEventListener("input", onFilterChange);
$("minscore").addEventListener("change", renderGrid);
$("co").addEventListener("change", renderGrid);
$("newonly").addEventListener("change", renderGrid);
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("q").value = ""; searchIds = null; renderGrid(); }
});
loadProfile();
loadState();
