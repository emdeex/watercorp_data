/* ==========================================================================
   app.js — Victorian Water Corporation Data Extractor
   ========================================================================== */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allCorps = [];
let allYears = [];
let rawData  = [];     // rows returned by /api/process-report
let calcResults = [];  // rows returned by /api/calculate
let chartInstance = null;

// ---------------------------------------------------------------------------
// Initialise
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  await loadConfig();
  bindControls();
});

async function loadConfig() {
  try {
    const resp = await fetch("/api/config");
    if (!resp.ok) {
      const body = await resp.text().catch(() => "(no body)");
      logLine(`Config API error ${resp.status}: ${body.slice(0, 300)}`, "error");
      document.getElementById("corp-list").innerHTML = '<span class="text-danger small">Failed to load — see Progress tab for details.</span>';
      document.getElementById("year-list").innerHTML = "";
      return;
    }
    const cfg  = await resp.json();
    allCorps = cfg.corporations;
    allYears = cfg.years;
    renderCorpList();
    renderYearList();
    document.getElementById("btn-run").disabled = false;
  } catch (e) {
    logLine("Failed to load configuration: " + e, "error");
    document.getElementById("corp-list").innerHTML = '<span class="text-danger small">Network error — see Progress tab.</span>';
  }
}

// ---------------------------------------------------------------------------
// Render sidebar checkboxes
// ---------------------------------------------------------------------------
function renderCorpList() {
  const el = document.getElementById("corp-list");
  el.innerHTML = "";
  allCorps.forEach(corp => {
    const div = document.createElement("div");
    div.className = "form-check";
    div.innerHTML = `
      <input class="form-check-input corp-cb" type="checkbox"
             value="${esc(corp.slug)}" id="corp-${esc(corp.slug)}"
             ${corp.is_gww_predecessor ? 'data-predecessor="1"' : ""}>
      <label class="form-check-label" for="corp-${esc(corp.slug)}">
        ${esc(corp.name)}${corp.is_gww_predecessor ? ' <span class="text-muted">(pre-merger)</span>' : ""}
      </label>`;
    el.appendChild(div);
  });
}

function renderYearList() {
  const el = document.getElementById("year-list");
  el.innerHTML = "";
  allYears.forEach(yr => {
    const div = document.createElement("div");
    div.className = "form-check";
    div.innerHTML = `
      <input class="form-check-input year-cb" type="checkbox"
             value="${esc(yr)}" id="yr-${yr}">
      <label class="form-check-label" for="yr-${yr}">${esc(yr)}</label>`;
    el.appendChild(div);
  });
}

// ---------------------------------------------------------------------------
// Control bindings
// ---------------------------------------------------------------------------
function bindControls() {
  document.getElementById("btn-all-corps").addEventListener("click", () => setAllCheckboxes(".corp-cb", true));
  document.getElementById("btn-no-corps").addEventListener("click",  () => setAllCheckboxes(".corp-cb", false));
  document.getElementById("btn-all-years").addEventListener("click", () => setAllCheckboxes(".year-cb", true));
  document.getElementById("btn-no-years").addEventListener("click",  () => setAllCheckboxes(".year-cb", false));
  document.getElementById("btn-run").addEventListener("click", runExtraction);
  document.getElementById("btn-calculate").addEventListener("click", runCalculate);
  document.getElementById("btn-dl-raw").addEventListener("click", downloadRawCsv);
  document.getElementById("btn-dl-results").addEventListener("click", downloadResultsCsv);
}

function setAllCheckboxes(selector, checked) {
  document.querySelectorAll(selector).forEach(cb => cb.checked = checked);
}

// ---------------------------------------------------------------------------
// Run Extraction
// ---------------------------------------------------------------------------
async function runExtraction() {
  const selectedCorps = [...document.querySelectorAll(".corp-cb:checked")].map(cb => cb.value);
  const selectedYears = [...document.querySelectorAll(".year-cb:checked")].map(cb => cb.value);
  const useLlm        = document.getElementById("opt-llm").checked;

  if (!selectedCorps.length || !selectedYears.length) {
    alert("Please select at least one corporation and one year.");
    return;
  }

  // Reset state
  rawData = [];
  clearRawTable();
  clearLogTable();
  clearProgress();

  const jobs = [];
  selectedCorps.forEach(slug => {
    selectedYears.forEach(year => jobs.push({ corp_slug: slug, year, use_llm: useLlm }));
  });

  const total = jobs.length;
  let done = 0;

  showProgress(true);
  updateProgressBar(0, total);
  logLine(`Starting extraction: ${total} report(s)…`, "info");

  document.getElementById("btn-run").disabled = true;
  document.getElementById("btn-calculate").disabled = true;

  // Process in batches of 5
  const BATCH = 5;
  for (let i = 0; i < jobs.length; i += BATCH) {
    const batch = jobs.slice(i, i + BATCH);
    const promises = batch.map(job =>
      fetch("/api/process-report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(job),
      })
      .then(r => r.json())
      .catch(err => ({
        corporation: job.corp_slug,
        year: job.year,
        status: "network_error",
        error: err.message,
        opex_dollars: null,
        volume_ml: null,
        extraction_method: null,
        confidence: "failed",
        notes: err.message,
      }))
    );

    const results = await Promise.allSettled(promises);
    results.forEach(outcome => {
      const row = outcome.status === "fulfilled" ? outcome.value : outcome.reason;
      done++;
      rawData.push(row);
      appendRawRow(row);
      logResult(row);
      if (row.status !== "ok") {
        appendLogRow(row);
      }
      updateProgressBar(done, total);
    });
  }

  logLine(`Done. ${done} report(s) processed.`, "info");
  document.getElementById("btn-run").disabled = false;
  document.getElementById("btn-calculate").disabled = false;
  document.getElementById("btn-dl-raw").disabled = false;

  // Switch to Raw Data tab
  const rawTab = document.querySelector('[data-bs-target="#tab-raw"]');
  if (rawTab) bootstrap.Tab.getOrCreateInstance(rawTab).show();
}

// ---------------------------------------------------------------------------
// Calculate
// ---------------------------------------------------------------------------
async function runCalculate() {
  if (!rawData.length) {
    alert("No extracted data to calculate.");
    return;
  }

  document.getElementById("btn-calculate").disabled = true;
  logLine("Calculating CPI-adjusted $/ML…", "info");

  try {
    const resp = await fetch("/api/calculate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows: rawData }),
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    calcResults = data.results;
    renderPivot(data.pivot);
    renderChart(data.pivot);
    document.getElementById("btn-dl-results").disabled = false;

    // Switch to Results tab
    const resultsTab = document.querySelector('[data-bs-target="#tab-results"]');
    if (resultsTab) bootstrap.Tab.getOrCreateInstance(resultsTab).show();

    logLine("Calculation complete.", "info");
  } catch (e) {
    logLine("Calculation failed: " + e.message, "error");
  }

  document.getElementById("btn-calculate").disabled = false;
}

// ---------------------------------------------------------------------------
// Render pivot table
// ---------------------------------------------------------------------------
function renderPivot(pivot) {
  const wrap = document.getElementById("results-wrap");
  if (!pivot || !pivot.corps.length) {
    wrap.innerHTML = "<p class='text-muted small'>No results to display.</p>";
    return;
  }

  const { corps, years, values, outliers } = pivot;

  let html = `<table class="table table-bordered table-sm" id="pivot-table">
    <thead class="table-light"><tr>
      <th>Corporation</th>`;
  years.forEach(y => { html += `<th>${esc(y)}</th>`; });
  html += `</tr></thead><tbody>`;

  corps.forEach((corp, ci) => {
    html += `<tr><td>${esc(corp)}</td>`;
    years.forEach((_, yi) => {
      const val = values[ci][yi];
      const outlier = outliers[ci][yi];
      if (val === null || val === undefined) {
        html += `<td class="pivot-missing">—</td>`;
      } else {
        const cls = outlier ? " pivot-outlier" : "";
        html += `<td class="${cls}">${fmt(val)}</td>`;
      }
    });
    html += `</tr>`;
  });

  html += `</tbody></table>
  <p class="text-muted small mt-1">Values are operating cost per ML delivered in ${esc(pivot.years ? pivot.years[pivot.years.length - 1] || "real" : "real")} dollars (CPI-adjusted). <span class="pivot-outlier px-1">Highlighted</span> cells are possible outliers.</p>`;
  wrap.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Render Chart.js line chart
// ---------------------------------------------------------------------------
function renderChart(pivot) {
  const el = document.getElementById("cost-chart");
  document.getElementById("chart-empty").style.display = "none";

  if (chartInstance) {
    chartInstance.destroy();
    chartInstance = null;
  }

  if (!pivot || !pivot.corps.length) return;

  const { corps, years, values } = pivot;
  const COLORS = [
    "#0d6efd","#198754","#dc3545","#fd7e14","#6f42c1",
    "#20c997","#0dcaf0","#ffc107","#d63384","#6610f2",
    "#adb5bd","#343a40","#0d6efd","#198754","#dc3545","#fd7e14",
  ];

  const datasets = corps.map((corp, ci) => ({
    label: corp,
    data: values[ci].map((v, yi) => ({ x: years[yi], y: v })),
    borderColor: COLORS[ci % COLORS.length],
    backgroundColor: COLORS[ci % COLORS.length] + "22",
    tension: 0.3,
    fill: false,
    pointRadius: 4,
    spanGaps: true,
  }));

  chartInstance = new Chart(el, {
    type: "line",
    data: { labels: years, datasets },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "right", labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: $${fmt(ctx.parsed.y)}/ML`,
          },
        },
      },
      scales: {
        x: { title: { display: true, text: "Financial Year" } },
        y: {
          title: { display: true, text: "Operating Cost ($/ML, real)" },
          ticks: { callback: v => "$" + fmt(v) },
        },
      },
    },
  });
}

// ---------------------------------------------------------------------------
// CSV downloads
// ---------------------------------------------------------------------------
async function downloadRawCsv() {
  const resp = await fetch("/api/download/raw", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows: rawData }),
  });
  triggerDownload(await resp.blob(), "watercorp_raw_data.csv");
}

async function downloadResultsCsv() {
  const resp = await fetch("/api/download/results", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows: rawData }),
  });
  triggerDownload(await resp.blob(), "watercorp_results.csv");
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a   = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Progress & logging helpers
// ---------------------------------------------------------------------------
function showProgress(visible) {
  const el = document.getElementById("progress-wrap");
  el.style.setProperty("display", visible ? "block" : "none", "important");
}

function updateProgressBar(done, total) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  document.getElementById("progress-bar").style.width = pct + "%";
  document.getElementById("progress-label").textContent = `${done} / ${total}`;
}

function clearProgress() {
  document.getElementById("progress-log").innerHTML = "";
  updateProgressBar(0, 0);
}

function logLine(msg, level = "info") {
  const el = document.getElementById("progress-log");
  const span = document.createElement("div");
  span.className = `log-${level}`;
  span.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}

function logResult(row) {
  const corp = row.corporation || row.corp_slug || "?";
  const year = row.year || "?";
  if (row.status === "ok" && row.opex_dollars && row.volume_ml) {
    logLine(
      `✓ ${corp} ${year} — opex $${fmtM(row.opex_dollars)}, vol ${fmtK(row.volume_ml)} ML [${row.confidence}]`,
      "ok"
    );
  } else if (row.status === "ok" && (row.opex_dollars || row.volume_ml)) {
    logLine(`⚠ ${corp} ${year} — partial result (confidence: ${row.confidence})`, "warn");
  } else {
    logLine(`✗ ${corp} ${year} — ${row.status}: ${row.error || row.notes || "no data"}`, "error");
  }
}

// ---------------------------------------------------------------------------
// Raw table helpers
// ---------------------------------------------------------------------------
function clearRawTable() {
  document.getElementById("raw-tbody").innerHTML = "";
  document.getElementById("raw-empty").style.display = "block";
}

function appendRawRow(row) {
  const tbody = document.getElementById("raw-tbody");
  document.getElementById("raw-empty").style.display = "none";
  const conf = row.confidence || "failed";
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${esc(row.corporation || row.corp_slug || "")}</td>
    <td>${esc(row.year || "")}</td>
    <td>${row.opex_dollars != null ? "$" + fmtM(row.opex_dollars) : "—"}</td>
    <td>${row.volume_ml != null ? fmtK(row.volume_ml) : "—"}</td>
    <td>${row.opex_page != null ? row.opex_page : "—"}</td>
    <td>${row.volume_page != null ? row.volume_page : "—"}</td>
    <td>${esc(row.extraction_method || "—")}</td>
    <td><span class="badge badge-${conf}">${conf}</span></td>
    <td class="text-muted">${esc(row.notes || "")}</td>`;
  tbody.appendChild(tr);
}

// ---------------------------------------------------------------------------
// Log table helpers
// ---------------------------------------------------------------------------
function clearLogTable() {
  document.getElementById("log-tbody").innerHTML = "";
  document.getElementById("log-empty").style.display = "block";
}

function appendLogRow(row) {
  const tbody = document.getElementById("log-tbody");
  document.getElementById("log-empty").style.display = "none";
  const tr = document.createElement("tr");
  tr.className = "table-danger";
  tr.innerHTML = `
    <td>${esc(row.corporation || row.corp_slug || "")}</td>
    <td>${esc(row.year || "")}</td>
    <td>${esc(row.status || "")}</td>
    <td>${esc(row.error || row.notes || "")}</td>`;
  tbody.appendChild(tr);
}

// ---------------------------------------------------------------------------
// Formatting utilities
// ---------------------------------------------------------------------------
function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmt(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-AU", { maximumFractionDigits: 0 });
}

function fmtM(n) {
  // Format large dollar amounts: 145,320,000 → $145.3M
  if (n == null) return "—";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
  return Number(n).toLocaleString("en-AU");
}

function fmtK(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-AU");
}
