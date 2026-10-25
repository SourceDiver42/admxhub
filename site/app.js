"use strict";
/* ADMX Hub website: renders the catalog JSON. No build step, no external resources. */

const BASE = "catalog/v1/";
const $app = document.getElementById("app");
let INDEX = null;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
const clear = (el) => { el.replaceChildren(); return el; };
// replace an element's content; null/false entries are skipped (native append would print "null")
const fill = (el, ...kids) => { el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false)); return el; };
const fmtSize = (n) => (n == null ? "" : n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
const fmtDate = (s) => (s ? String(s).slice(0, 10) : "");
const safe = (v) => String(v).replace(/[^\w.+-]/g, "_").slice(0, 80);

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function route() {
  try {
    if (!INDEX) {
      INDEX = await getJSON(BASE + "index.json");
      document.getElementById("generated").textContent = `· updated ${INDEX.generated.replace("T", " ").slice(0, 16)} UTC`;
    }
  } catch (e) {
    fill($app, h("h1", {}, "ADMX Hub"), h("p", { class: "muted" }, "The catalog has not been built yet. It is published by the daily GitHub Action."));
    return;
  }
  const [, page, id, sub] = location.hash.split("/");
  window.scrollTo(0, 0);
  if (page === "p" && id) return sub === "policies" ? viewPolicies(id) : viewProduct(id);
  if (page === "cli") return viewCli();
  return viewList();
}
window.addEventListener("hashchange", route);

function health(p) {
  const st = p.status || {};
  return st.error ? h("span", { class: "badge bad", title: st.error }, "check failing") : null;
}

function viewList() {
  const search = h("input", { type: "search", placeholder: "Filter by product or vendor…", "aria-label": "Filter" });
  const grid = h("div", { class: "grid" });
  const draw = () => {
    const q = search.value.toLowerCase();
    clear(grid);
    for (const p of INDEX.products.filter((x) => !q || `${x.name} ${x.vendor} ${x.id}`.toLowerCase().includes(q))) {
      const l = p.latest || {};
      const recent = l.discovered && Date.now() - Date.parse(l.discovered) < 14 * 86400000 && (l.diff || {}).previous;
      grid.append(h("a", { class: "card", href: `#/p/${p.id}` },
        h("div", { class: "row" }, h("h3", {}, p.name), recent ? h("span", { class: "badge new" }, "updated") : null, health(p)),
        h("div", { class: "muted small" }, p.vendor),
        h("p", { class: "small" }, p.description),
        h("div", { class: "row small" }, h("strong", {}, l.version || "—"), h("span", { class: "muted" }, fmtDate(l.date || l.discovered)),
          l.policyCount ? h("span", { class: "muted" }, `· ${l.policyCount} policies`) : null)));
    }
  };
  search.oninput = draw;
  fill($app, h("h1", {}, "Group Policy templates"),
    h("p", { class: "muted" }, `${INDEX.products.length} products. Latest versions, checksums, policy lists and what changed between releases. Subscribe to the `, h("a", { href: BASE + "feed.xml" }, "feed"), " to hear about new templates."),
    search, grid,
    INDEX.warnings && INDEX.warnings.length ? h("details", { style: "margin-top:20px" }, h("summary", { class: "muted small" }, "Namespace conflicts between products"), h("ul", { class: "small" }, INDEX.warnings.map((w) => h("li", {}, w)))) : null);
  draw();
}

async function viewProduct(id) {
  fill($app, h("p", { class: "muted" }, "Loading…"));
  let p;
  try { p = await getJSON(`${BASE}products/${id}.json`); } catch (e) { fill($app, h("p", {}, "Unknown product.")); return; }
  const l = p.versions[0];
  fill($app, 
    h("div", { class: "muted small" }, h("a", { href: "#/" }, "Templates"), " / ", p.vendor),
    h("div", { class: "row" }, h("h1", {}, p.name), health(p)),
    h("p", {}, p.description, " ", h("a", { href: p.homepage, rel: "noopener" }, "Vendor page")));
  if (!l) { $app.append(h("p", { class: "muted" }, "No version recorded yet.")); return; }
  $app.append(
    h("div", { class: "card" }, h("div", { class: "kv" },
      h("span", {}, "Latest version"), h("strong", {}, l.version),
      h("span", {}, "Released / discovered"), h("span", {}, `${fmtDate(l.date) || "—"} / ${fmtDate(l.discovered)}`),
      h("span", {}, "Download (vendor)"), h("a", { href: l.url, rel: "noopener", class: "mono" }, decodeURIComponent(l.url.split("/").pop())),
      h("span", {}, "Size"), h("span", {}, fmtSize(l.size)),
      h("span", {}, "SHA-256"), h("span", { class: "mono" }, l.sha256),
      h("span", {}, "Policies"), h("span", {}, `${l.policyCount} in ${l.admxCount} ADMX file(s) · `, h("a", { href: `#/p/${p.id}/policies` }, "browse")),
      h("span", {}, "Languages"), h("span", {}, l.languages.join(", ")),
      h("span", {}, "Namespaces"), h("span", { class: "mono" }, l.namespaces.join(", ")))),
    h("h2", {}, "Install with the CLI"),
    h("pre", {}, `admxhub install ${p.id} --store \\\\your.domain\\SYSVOL\\your.domain\\Policies\\PolicyDefinitions`),
    h("h2", {}, "Files"),
    h("div", { class: "table-wrap" }, h("table", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Language"), h("th", {}, "Size"), h("th", {}, "SHA-256")),
      l.files.filter((f) => f.kind === "admx" || f.lang === "en-US").map((f) => h("tr", {}, h("td", { class: "mono" }, f.name), h("td", {}, f.lang || "—"), h("td", { class: "small" }, fmtSize(f.size)), h("td", { class: "mono small" }, f.sha256))))),
    h("p", { class: "muted small" }, `Showing ADMX and en-US files of ${l.files.length}.`),
    h("h2", {}, "Version history"),
    h("div", { class: "table-wrap" }, h("table", {}, h("tr", {}, h("th", {}, "Version"), h("th", {}, "Released"), h("th", {}, "Policies"), h("th", {}, "Changes")),
      p.versions.map((v) => {
        const d = v.diff || {};
        return h("tr", {}, h("td", {}, h("strong", {}, v.version), v.backfilled ? h("span", { class: "badge", style: "margin-left:6px", title: "Added from the vendor's release history" }, "historic") : null),
          h("td", { class: "small" }, fmtDate(v.date || v.discovered)), h("td", {}, v.policyCount),
          h("td", { class: "small" }, d.previous ? diffDetails(d) : h("span", { class: "muted" }, "first recorded")));
      }))));
}

function diffDetails(d) {
  const lists = ["added", "removed", "changed"].filter((k) => d.details && d.details[k].length);
  return h("details", {}, h("summary", {}, `+${d.added} / −${d.removed} / ~${d.changed} since ${d.previous}`),
    lists.map((k) => h("div", {}, h("strong", {}, k + ": "),
      h("span", { class: "mono" }, d.details[k].map((x) => x.split(":").pop()).join(", ")))));
}

async function viewPolicies(id) {
  fill($app, h("p", { class: "muted" }, "Loading…"));
  const p = await getJSON(`${BASE}products/${id}.json`);
  const v = p.versions[0];
  const pols = await getJSON(`${BASE}policies/${id}/${safe(v.version)}.json`);
  const search = h("input", { type: "search", placeholder: "Search policy names and registry values…" });
  const body = h("tbody");
  const count = h("span", { class: "muted small" });
  const draw = () => {
    const q = search.value.toLowerCase();
    const hits = pols.filter((x) => !q || `${x.display} ${x.name} ${x.key} ${x.valueName || ""} ${x.category.join(" ")}`.toLowerCase().includes(q));
    count.textContent = `${hits.length} of ${pols.length}`;
    clear(body).append(...hits.slice(0, 500).map((x) => h("tr", {},
      h("td", {}, h("div", {}, x.display), h("div", { class: "muted small" }, x.category.join(" / "))),
      h("td", { class: "small" }, x.class), h("td", { class: "mono" }, `${x.key}${x.valueName ? "\\" + x.valueName : ""}`))));
  };
  search.oninput = draw;
  fill($app, h("div", { class: "muted small" }, h("a", { href: "#/" }, "Templates"), " / ", h("a", { href: `#/p/${id}` }, p.name)),
    h("h1", {}, `${p.name} ${v.version}: policies`), h("div", { class: "row" }, search, count),
    h("div", { class: "table-wrap", style: "margin-top:12px" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Policy"), h("th", {}, "Scope"), h("th", {}, "Registry"))), body)));
  draw();
}

function viewCli() {
  fill($app, h("h1", {}, "admxhub CLI"),
    h("p", {}, "A single admxhub.exe for Windows (from the GitHub releases), or ", h("code", {}, "pip install admxhub"), " on any OS."),
    h("pre", {}, [
      "admxhub sources                     # known products",
      "admxhub check                       # latest versions",
      "admxhub status                      # what's installed in your central store, what's outdated",
      "admxhub install edge chrome         # install into the central store (or --store PATH)",
      "admxhub update                      # update everything that has a newer version",
      "admxhub policies edge --search tls  # search the policies of a product",
      "admxhub download windows11 --out .  # just download and extract",
    ].join("\n")),
    h("p", { class: "muted small" }, "On a domain-joined Windows machine the default store is \\\\<domain>\\SYSVOL\\<domain>\\Policies\\PolicyDefinitions; otherwise C:\\Windows\\PolicyDefinitions. Add --json to any command for scripting."));
}

route();
