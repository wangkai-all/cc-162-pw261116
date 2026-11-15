/* Catalog UI: polls the JSON API and renders graph, failures, manifests,
 * change log and run history.  No frameworks, no build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, options) => {
  const res = await fetch(path, options);
  return res.json();
};

let graphCache = { nodes: [], edges: [], roots: [] };
let activeTimer = null;

// ----------------------------------------------------------- utilities

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function titleHtml(titles) {
  if (!titles || !titles.length) return "<em>(untitled)</em>";
  return titles.map((t) =>
    `<span class="lang-tag">${escapeHtml(t.language || "none")}</span>${escapeHtml(t.text)}`
  ).join("<br>");
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    return u.pathname;
  } catch {
    return url;
  }
}

function kindPill(kind) {
  return `<span class="kind-pill kind-${kind}">${kind.replace("_", " ")}</span>`;
}

function statePill(state) {
  return `<span class="state-pill state-${state}">${state}</span>`;
}

// -------------------------------------------------------------- status

async function refreshStatus() {
  const status = await api("/api/status");
  const el = $("#status");
  const counts = status.counts || {};
  const countText = Object.entries(counts)
    .map(([k, v]) => `${k}: ${v}`).join("  ·  ") || "no nodes yet";

  if (status.active && status.running_run) {
    const p = status.progress || {};
    el.innerHTML = `<span class="dot running"></span>run #${status.running_run.id} `
      + `(${status.running_run.mode}) — current: ${escapeHtml(p.current || "—")} `
      + `· queued ${p.queued ?? 0} · fetched ${p.fetched ?? 0} `
      + `· failed ${p.failed ?? 0} · changed ${p.changed ?? 0}`;
    document.querySelectorAll(".harvest-bar button").forEach(b => b.disabled = true);
    if (!activeTimer) activeTimer = setInterval(refreshAll, 1000);
  } else {
    const run = status.latest_run;
    const dot = run && run.status === "failed" ? "failed" : "done";
    el.innerHTML = `<span class="dot ${dot}"></span>${countText}`
      + (run ? ` · latest run #${run.id}: ${run.status}` : "");
    document.querySelectorAll(".harvest-bar button").forEach(b => b.disabled = false);
    if (activeTimer) { clearInterval(activeTimer); activeTimer = null; }
  }

  const failed = counts.failed || 0;
  $("#fail-badge").classList.toggle("hidden", failed === 0);
  $("#fail-badge").textContent = failed;
  return status;
}

// --------------------------------------------------------------- graph

async function refreshGraph() {
  graphCache = await api("/api/graph");
  const byUrl = new Map(graphCache.nodes.map((n) => [n.url, n]));
  const childrenOf = new Map();
  for (const e of graphCache.edges) {
    if (!childrenOf.has(e.parent)) childrenOf.set(e.parent, []);
    childrenOf.get(e.parent).push(e.child);
  }

  const roots = graphCache.roots.length
    ? graphCache.roots
    : graphCache.nodes.map((n) => n.url);

  const renderNode = (url, ancestors) => {
    const node = byUrl.get(url);
    const label = node
      ? node.titles.map((t) => t.text).join(" / ") || shortUrl(url)
      : shortUrl(url);
    const state = node ? node.state : "unknown";
    const isCycle = ancestors.includes(url);
    const nextAncestors = ancestors.concat(url);
    const kids = isCycle ? [] : (childrenOf.get(url) || []);
    const expandable = kids.length
      ? `<span class="toggle">▼</span>` : `<span class="toggle" style="visibility:hidden">·</span>`;
    const fallback = node && node.title_source.startsWith("fallback:")
      ? ` <span class="fallback-tag" title="source: ${node.title_source}">fallback title</span>` : "";

    let html = `<li><div class="node-row">${expandable}`
      + `<a data-url="${encodeURIComponent(url)}">${escapeHtml(label)}</a>`
      + statePill(state)
      + (isCycle ? ` <span class="cycle-tag">cycle — already shown above</span>` : "")
      + fallback + `</div>`;
    if (kids.length) {
      html += `<ul>${kids.map((k) => renderNode(k, nextAncestors)).join("")}</ul>`;
    }
    return html + "</li>";
  };

  $("#graph").innerHTML = `<ul class="tree">${roots
    .map((r) => renderNode(r, [])).join("")}</ul>`;

  $("#graph .toggle").forEach((t) => {
    t.style.cursor = "pointer";
    t.addEventListener("click", (ev) => {
      const ul = ev.target.closest("li").querySelector("ul");
      if (!ul) return;
      const hidden = ul.classList.toggle("hidden");
      ev.target.textContent = hidden ? "▶" : "▼";
    });
  });
  $("#graph a[data-url]").forEach((a) => {
    a.addEventListener("click", () => openNode(decodeURIComponent(a.dataset.url)));
  });
}

// ------------------------------------------------------------ failures

async function refreshFailures() {
  const rows = await api("/api/nodes?state=failed");
  $("#failures-body").innerHTML = rows.map((n) => `
    <tr>
      <td><a class="node-link" data-url="${encodeURIComponent(n.url)}">${escapeHtml(n.url)}</a></td>
      <td>${n.http_status ?? "—"}</td>
      <td>${escapeHtml(n.error || "")}</td>
      <td>${n.fetch_count}</td>
      <td>${escapeHtml(n.last_fetched_at || "")}</td>
      <td><button class="secondary btn-refetch" data-url="${encodeURIComponent(n.url)}">Re-fetch node</button></td>
    </tr>`).join("") || `<tr><td colspan="6" style="color:var(--muted)">No failed nodes.</td></tr>`;
  bindRefetchButtons("#failures-body");
}

// ------------------------------------------------------------ manifests

async function refreshManifests() {
  const [nodes, graph] = await Promise.all(
    [api("/api/nodes?state=confirmed"), api("/api/graph")]);
  const parentsOf = new Map();
  for (const e of graph.edges) {
    if (!parentsOf.has(e.child)) parentsOf.set(e.child, []);
    parentsOf.get(e.child).push(e.parent);
  }
  $("#manifests-body").innerHTML = nodes.map((n) => {
    const parents = (parentsOf.get(n.url) || []).map(shortUrl).join("<br>");
    return `<tr>
      <td><a class="node-link" data-url="${encodeURIComponent(n.url)}">${titleHtml(n.titles)}</a></td>
      <td>${n.node_type}</td>
      <td><code>${escapeHtml(shortUrl(n.url))}</code></td>
      <td>${escapeHtml(n.title_source)}</td>
      <td>${n.node_type === "collection" ? "—"
        : `<button class="secondary btn-images" data-url="${encodeURIComponent(n.url)}">images</button>`}</td>
      <td>${escapeHtml(parents) || "—"}</td>
      <td>${escapeHtml(n.last_fetched_at || "")} <small>×${n.fetch_count}</small></td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" style="color:var(--muted)">No confirmed nodes yet.</td></tr>`;

  document.querySelectorAll(".node-link").forEach((a) => {
    a.addEventListener("click", () => openNode(decodeURIComponent(a.dataset.url)));
  });
  document.querySelectorAll(".btn-images").forEach((b) => {
    b.addEventListener("click", async () => {
      const detail = await api("/api/nodes/" + b.dataset.url);
      const rows = (detail.images || []).map((i) =>
        `<li class="image-row">${escapeHtml(i.url)} — ${i.width ?? "?"}×${i.height ?? "?"}px</li>`
      ).join("");
      showModal(`<h2>Image metadata</h2><p>Addresses and dimensions only; bytes are never downloaded.</p>
        <ul class="path-list">${rows || "<li>no images</li>"}</ul>`);
    });
  });
}

// ------------------------------------------------------------- changes

async function refreshChangeRuns() {
  const runs = await api("/api/runs");
  const select = $("#change-run");
  const current = select.value;
  select.innerHTML = `<option value="">all runs</option>` + runs.map((r) =>
    `<option value="${r.id}">#${r.id} ${r.mode} (${r.status})</option>`).join("");
  select.value = current;
}

async function refreshChanges() {
  const runId = $("#change-run").value;
  const kind = $("#change-kind").value;
  const params = new URLSearchParams();
  if (runId) params.set("run_id", runId);
  if (kind) params.set("kind", kind);
  const rows = await api("/api/changes?" + params.toString());
  $("#change-badge").classList.toggle("hidden", rows.length === 0);
  $("#change-badge").textContent = rows.length;
  $("#changes-body").innerHTML = rows.map((c) => `
    <tr>
      <td>${kindPill(c.change_kind)}</td>
      <td><code>${escapeHtml(shortUrl(c.node_url))}</code></td>
      <td><code>${escapeHtml(shortUrl(c.resource_url))}</code><br>
        <small>${escapeHtml(c.resource_type)}${c.old_value ? `<br>old: ${escapeHtml(c.old_value)}` : ""}${c.new_value ? `<br>new: ${escapeHtml(c.new_value)}` : ""}</small></td>
      <td>${escapeHtml(c.detail)}</td>
      <td>#${c.run_id}</td>
    </tr>`).join("")
    || `<tr><td colspan="5" style="color:var(--muted)">No changes recorded.</td></tr>`;
}

// ---------------------------------------------------------------- runs

async function refreshRuns() {
  const rows = await api("/api/runs");
  $("#runs-body").innerHTML = rows.map((r) => `
    <tr>
      <td>${r.id}</td><td>${escapeHtml(r.mode)}</td>
      <td><code>${escapeHtml(shortUrl(r.seed_url))}</code></td>
      <td>${statePill(r.status)}</td>
      <td>${r.fetched}</td><td>${r.failed}</td><td>${r.changed}</td>
      <td>${escapeHtml(r.started_at)}</td><td>${escapeHtml(r.finished_at || "—")}</td>
    </tr>`).join("")
    || `<tr><td colspan="9" style="color:var(--muted)">No runs yet.</td></tr>`;
}

// --------------------------------------------------------------- modal

function showModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}

async function openNode(url) {
  const detail = await api("/api/nodes/" + encodeURIComponent(url));
  if (detail.error) { showModal(`<h2>Error</h2><p>${escapeHtml(detail.error)}</p>`); return; }

  const paths = (detail.source_paths || []).map((p) =>
    `<li>${p.map(shortUrl).map(escapeHtml).join(" → ")}</li>`).join("");
  const parents = (detail.parents || []).map((p) =>
    `<li>${escapeHtml(shortUrl(p.url))} — ${statePill(p.state)}</li>`).join("") || "<li>none (root)</li>";
  const kids = (detail.children || []).map((c) =>
    `<li><a class="node-link" data-url="${encodeURIComponent(c.url)}">${escapeHtml(shortUrl(c.url))}</a> ${statePill(c.state)}</li>`).join("") || "<li>none</li>";
  const images = (detail.images || []).map((i) =>
    `<li class="image-row">${escapeHtml(i.url)} — ${i.width ?? "?"}×${i.height ?? "?"}px</li>`).join("");

  showModal(`
    <h2>${titleHtml(detail.titles)}</h2>
    <p><code>${escapeHtml(detail.url)}</code> ${statePill(detail.state)}
       <small>type: ${detail.node_type} · title source: ${escapeHtml(detail.title_source)}
       · fetched ${detail.fetch_count}×</small></p>
    ${detail.error ? `<p style="color:var(--bad)">${escapeHtml(detail.error)}</p>` : ""}
    <h3>Source paths (provenance)</h3>
    <ul class="path-list">${paths || "<li>not reachable from a root yet</li>"}</ul>
    <h3>Contained in</h3><ul class="path-list">${parents}</ul>
    <h3>Contains</h3><ul class="path-list">${kids}</ul>
    ${images ? `<h3>Images (metadata only)</h3><ul class="path-list">${images}</ul>` : ""}
    <p><button id="modal-refetch" data-url="${encodeURIComponent(detail.url)}">Re-fetch this node</button></p>`);

  $("#modal-refetch").addEventListener("click", () => refetchNode(detail.url));
  $("#modal-body .node-link").forEach((a) => {
    a.addEventListener("click", () => openNode(decodeURIComponent(a.dataset.url)));
  });
}

$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") $("#modal").classList.add("hidden");
});

// -------------------------------------------------------------- actions

function bindRefetchButtons(selector) {
  document.querySelectorAll(selector + " .btn-refetch").forEach((b) => {
    b.addEventListener("click", () => refetchNode(decodeURIComponent(b.dataset.url)));
  });
}

async function refetchNode(url) {
  const result = await api("/api/harvest/refetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!result.ok) { alert(result.error); return; }
  if (!activeTimer) activeTimer = setInterval(refreshAll, 1000);
  refreshAll();
}

$("#btn-crawl").addEventListener("click", async () => {
  const seed_url = $("#seed-url").value.trim();
  if (!seed_url) return alert("Enter a seed collection URL first.");
  const result = await api("/api/harvest/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seed_url, mode: "crawl" }),
  });
  if (!result.ok) alert(result.error);
  if (!activeTimer) activeTimer = setInterval(refreshAll, 1000);
});

$("#btn-refetch-all").addEventListener("click", async () => {
  const seed_url = $("#seed-url").value.trim();
  if (!seed_url) return alert("Enter a seed collection URL first.");
  const result = await api("/api/harvest/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seed_url, mode: "refetch" }),
  });
  if (!result.ok) alert(result.error);
  if (!activeTimer) activeTimer = setInterval(refreshAll, 1000);
});

$("#btn-export").addEventListener("click", async () => {
  const saved = await api("/api/export");
  if (saved.ok) {
    const payload = await api("/api/export?download=1");
    const blob = new Blob([JSON.stringify(payload, null, 2)],
      { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "catalog-export.json";
    a.click();
    URL.revokeObjectURL(a.href);
    $("#status").textContent = `export saved on server: ${saved.path}`;
  }
});

// Tabs
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#tab-" + tab.dataset.tab).classList.add("active");
  });
});
$("#change-run").addEventListener("change", refreshChanges);
$("#change-kind").addEventListener("change", refreshChanges);

async function refreshAll() {
  const status = await refreshStatus();
  await Promise.all([
    refreshGraph(), refreshFailures(), refreshManifests(),
    refreshChanges(), refreshRuns(), refreshChangeRuns(),
  ]);
  return status;
}

// Seed convenience: prefill from the local fixture if reachable.
(async function init() {
  try {
    const manifest = await api("http://127.0.0.1:8090/__control/manifest");
    if (manifest && manifest.seed_url) $("#seed-url").value = manifest.seed_url;
  } catch { /* fixture server not running — leave placeholder */ }
  await refreshAll();
  setInterval(refreshAll, 5000);
})();
