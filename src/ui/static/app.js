"use strict";

const $ = (id) => document.getElementById(id);
const PHASES = ["SCAN", "EXTRACT", "VALIDATE"];

const view = {
  runId: null,
  run: null, // snapshot from /api/runs/<id>: workflow status, history, links
  source: null,
  steps: new Map(), // tool id -> { el, started }
  phase: null,
  runStarted: null,
  timer: null,
};

// All text from the agent (file names, tool output) goes through textContent, never innerHTML.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function fmtMs(ms) {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/* ---------- statuses (labels, colours and actions come from the server) ---------- */

const catalog = { statuses: [], byId: new Map(), actions: {}, verdicts: {} };

async function loadCatalog() {
  const res = await fetch("/api/statuses").catch(() => null);
  if (!res || !res.ok) return;
  const data = await res.json();
  catalog.statuses = data.statuses;
  catalog.byId = new Map(data.statuses.map((s) => [s.id, s]));
  catalog.actions = data.actions;
  catalog.verdicts = data.verdicts;
}

// Ids such as "needs_review" never reach the screen; unknown ones still read as words.
function humanize(id) {
  const text = String(id || "").replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function statusInfo(id) {
  return catalog.byId.get(id) || { id, label: humanize(id), tone: "neutral", description: "", active: false };
}

function verdictLabel(verdict) {
  return catalog.verdicts[verdict] || humanize(verdict);
}

function statusPill(id, node = el("span")) {
  const s = statusInfo(id);
  node.className = `pill tone-${s.tone}`;
  node.textContent = s.label;
  node.title = s.description || "";
  return node;
}

function fmtWhen(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fmtAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 2 * 86400) return "yesterday";
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/* ---------- upload ---------- */

function showUploadError(message) {
  const box = $("upload-error");
  box.textContent = message || "";
  box.hidden = !message;
}

async function uploadFile(file) {
  showUploadError("");
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
    showUploadError("Please choose a PDF file.");
    return;
  }
  const body = new FormData();
  body.append("file", file);
  try {
    const res = await fetch("/api/runs", { method: "POST", body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showUploadError(err.detail || `Upload failed (${res.status}).`);
      return;
    }
    // Stay on the list: the invoice shows up at the top and its status follows the agent
    // (the list refreshes itself while anything is new or processing).
    await res.json();
    dash.filter = "all";
    // From the home page, go to the list so the new invoice is seen at the top.
    if (location.hash.startsWith("#/invoices")) loadRuns();
    else location.hash = "#/invoices";
  } catch (e) {
    showUploadError(
      location.protocol === "file:"
        ? "This page was opened as a file. Start the UI with `python -m src.ui` and open the address it prints."
        : "Could not reach the server. Is `python -m src.ui` still running in your terminal? Restart it and reload this page."
    );
  }
}

function initDropzone() {
  const zone = $("dropzone");
  const input = $("file-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => { uploadFile(input.files[0]); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) =>
    zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) =>
    zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
  zone.addEventListener("drop", (e) => uploadFile(e.dataTransfer.files[0]));
}

/* ---------- dashboard: one row per invoice, shown by its latest run ---------- */

const dash = { runs: [], filter: "all", timer: null };
const DASH_REFRESH_MS = 2000;

async function loadRuns() {
  clearTimeout(dash.timer);
  const res = await fetch("/api/invoices").catch(() => null);
  if ($("dashboard").hidden) return;
  if (res && res.ok) {
    dash.runs = await res.json();
    renderDashboard();
  } else {
    $("runs-empty").textContent = "Could not load the invoices. Is `python -m src.ui` still running?";
    $("runs-empty").hidden = false;
  }
  // Refresh only while something is still moving on its own (new or processing).
  if (dash.runs.some((r) => statusInfo(r.status).active)) dash.timer = setTimeout(loadRuns, DASH_REFRESH_MS);
}

function renderDashboard() {
  const counts = new Map();
  dash.runs.forEach((r) => counts.set(r.status, (counts.get(r.status) || 0) + 1));

  const filters = $("status-filters");
  filters.replaceChildren();
  const chip = (id, label, n) => {
    const b = el("button", `filter${dash.filter === id ? " on" : ""}${n ? "" : " zero"}`);
    b.type = "button";
    b.setAttribute("aria-pressed", String(dash.filter === id));
    b.append(label, el("span", "filter-n", String(n)));
    b.addEventListener("click", () => { dash.filter = id; renderDashboard(); });
    filters.append(b);
  };
  chip("all", "All", dash.runs.length);
  catalog.statuses.forEach((s) => chip(s.id, s.label, counts.get(s.id) || 0));
  // Statuses the page does not know (e.g. added on the server after it loaded) still get a chip.
  counts.forEach((n, id) => { if (!catalog.byId.has(id)) chip(id, humanize(id), n); });

  const shown = dash.filter === "all" ? dash.runs : dash.runs.filter((r) => r.status === dash.filter);
  $("runs-list").replaceChildren(...shown.map(runRow));
  const empty = $("runs-empty");
  empty.textContent = dash.runs.length ? `No invoices with the status ${statusInfo(dash.filter).label}.`
    : "No invoices yet. Drop one above to check it.";
  empty.hidden = shown.length > 0;
}

function runRow(r) {
  const row = el("a", "runs-row run-link");
  row.href = `#/runs/${encodeURIComponent(r.run_id)}`;
  row.setAttribute("role", "row");

  const name = el("span", "run-name");
  name.setAttribute("role", "cell");
  name.append(el("span", "run-filename", r.filename));
  if (r.invoice_type) name.append(el("span", "run-sub", r.invoice_type));

  const status = el("span", "run-status");
  status.setAttribute("role", "cell");
  status.append(statusPill(r.status));

  const s = r.summary || {};
  const issues = el("span", "run-issues");
  issues.setAttribute("role", "cell");
  if (typeof s.blocking === "number") {
    const parts = [];
    if (s.blocking) parts.push(el("span", "issue-n bad", `${s.blocking} blocking`));
    if (s.warnings) parts.push(el("span", "issue-n warn", `${s.warnings} warning${s.warnings === 1 ? "" : "s"}`));
    if (!parts.length) parts.push(el("span", "issue-n none", "None"));
    issues.append(...parts);
  } else {
    issues.append(el("span", "muted", "—"));
  }

  const when = el("span", "run-when", fmtAgo(r.created_at));
  when.setAttribute("role", "cell");
  when.title = fmtWhen(r.created_at);

  row.append(name, status, issues, when);
  return row;
}

/* ---------- run view ---------- */

function resetView() {
  if (view.source) view.source.close();
  clearInterval(view.timer);
  Object.assign(view, { runId: null, run: null, source: null, phase: null, runStarted: null, timer: null });
  view.steps.clear();
  $("timeline").replaceChildren();
  resetResult();
  $("banner").hidden = true;
  $("queue-note").hidden = true;
  $("models").replaceChildren();
  $("run-elapsed").textContent = "0.0s";
  $("run-actions").replaceChildren();
  $("run-history").replaceChildren();
  $("action-error").hidden = true;
  setPhaseStates(null);
  setStatus("new");
}

// The workflow status, on both the Run and the Result card.
function setStatus(id) {
  statusPill(id, $("run-state"));
  statusPill(id, $("result-status"));
}

// Status, reviewer actions and history of the open run, from its server snapshot.
function renderRunMeta(run) {
  view.run = run;
  setStatus(run.status);
  // Edit buttons only where the status allows corrections (the server checks this too).
  $("field-list").classList.toggle("locked", !statusInfo(run.status).editable);

  const actions = $("run-actions");
  actions.replaceChildren();
  Object.entries(catalog.actions)
    .filter(([, a]) => a.from.includes(run.status))
    .forEach(([name, a]) => {
      const style = { reevaluate: " secondary", return: " danger" }[name] || "";
      const b = el("button", `btn${style}`, a.label);
      b.type = "button";
      // Preview actions only open their dialog; the server would refuse them.
      b.addEventListener("click", () => (name === "return" ? openReturnDialog() : runAction(name, b)));
      actions.append(b);
    });

  const hist = $("run-history");
  hist.replaceChildren();
  (run.history || []).forEach((h, i) => {
    if (i) hist.append(el("span", "hist-sep", "→"));
    const item = el("span", "hist-item");
    item.append(el("b", "", statusInfo(h.status).label), ` · ${fmtWhen(h.at)}`);
    if (h.note) item.title = h.note;
    hist.append(item);
  });
  const link = (text, id) => {
    const a = el("a", "hist-link", text);
    a.href = `#/runs/${encodeURIComponent(id)}`;
    return a;
  };
  if (run.reevaluated_from) hist.append(link("Re-evaluation of an earlier run", run.reevaluated_from));
  if (run.reevaluated_to) hist.append(link("Re-evaluated: open the new run →", run.reevaluated_to));
}

/* ---------- return to the local partner (design preview: nothing is sent) ---------- */

// The draft survives closing the dialog, so a reviewer can look something up and come back.
const returnDraft = { runId: null, edited: false };

function returnIssues() {
  return (result.data && result.data.findings) || [];
}

function draftReturnMessage() {
  const picked = [...document.querySelectorAll("#return-issue-list input:checked")]
    .map((box) => returnIssues().find((f) => f.ref === box.value))
    .filter(Boolean);
  const lines = [`Hello,`, ``, `We reviewed the invoice ${view.run.filename} and cannot accept it yet.`];
  if (picked.length) {
    lines.push(`Please address the following:`, ``);
    picked.forEach((f) => lines.push(`- ${f.title}${f.reason ? `: ${f.reason}` : ""}`));
  } else {
    lines.push(`[Explain what needs to change.]`);
  }
  lines.push(``, `Please send us a corrected invoice or the missing documents.`, ``, `Thank you,`);
  $("return-message").value = lines.join("\n");
  returnDraft.edited = false;
}

function openReturnDialog() {
  $("return-invoice").textContent = view.run.filename;
  const list = $("return-issue-list");
  if (returnDraft.runId !== view.runId) {
    list.replaceChildren();
    const issues = returnIssues();
    issues.forEach((f) => {
      const row = el("label", "return-issue");
      const box = el("input");
      box.type = "checkbox";
      box.value = f.ref;
      box.checked = true;
      row.append(box, el("span", `badge ${f.status}`, STATUS_LABEL[f.status] || humanize(f.status)), el("span", "", `${f.ref} · ${f.title}`));
      list.append(row);
    });
    if (!issues.length) list.append(el("p", "muted", "The agent listed no issues for this invoice."));
    returnDraft.runId = view.runId;
    draftReturnMessage();
  }
  $("return-dialog").showModal();
  $("return-message").focus();
}

function initReturnDialog() {
  const dialog = $("return-dialog");
  dialog.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dialog.close()));
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); }); // backdrop
  $("return-message").addEventListener("input", () => { returnDraft.edited = true; });
  // Ticking issues keeps the draft in step until the reviewer has written their own text.
  $("return-issue-list").addEventListener("change", () => { if (!returnDraft.edited) draftReturnMessage(); });
  $("return-redraft").addEventListener("click", draftReturnMessage);
  // "Return invoice" submits the form, and method="dialog" just closes it. Click dummy: nothing
  // is sent and the status does not change (there is no business layer for returns yet).
}

async function refreshRunMeta() {
  const runId = view.runId;
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`).catch(() => null);
  if (res && res.ok && view.runId === runId) renderRunMeta(await res.json());
}

async function runAction(name, button) {
  const runId = view.runId;
  $("action-error").hidden = true;
  button.disabled = true;
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/actions/${name}`, { method: "POST" }).catch(() => null);
  button.disabled = false;
  if (view.runId !== runId) return;
  if (!res || !res.ok) {
    const err = res ? await res.json().catch(() => ({})) : {};
    $("action-error").textContent = err.detail || "That did not work. Is the server still running?";
    $("action-error").hidden = false;
    return;
  }
  const run = await res.json();
  if (run.run_id !== runId) location.hash = `#/runs/${encodeURIComponent(run.run_id)}`; // re-evaluated
  else renderRunMeta(run);
}

function setPhaseStates(current, failed = false) {
  const idx = current ? PHASES.indexOf(current) : -1;
  document.querySelectorAll("#stepper li").forEach((li) => {
    const i = PHASES.indexOf(li.dataset.phase);
    li.className = i < idx ? "done" : i === idx ? (failed ? "failed" : "active") : "";
  });
}

function tickClock() {
  if (view.runStarted === null) return;
  $("run-elapsed").textContent = fmtMs(Date.now() - view.runStarted);
  view.steps.forEach((s) => {
    if (s.started !== null) s.timeEl.textContent = fmtMs(Date.now() - s.started);
  });
}

function openRun(run) {
  resetView();
  const runId = run.run_id;
  view.runId = runId;
  showView("run");
  $("run-file").textContent = run.filename || "…";
  renderRunMeta(run);

  // Runs saved before timelines were recorded have no events: show their outcome directly.
  if (run.event_count === 0 && (run.state === "done" || run.state === "error")) {
    $("queue-note").textContent = "The timeline was not recorded for this run.";
    $("queue-note").hidden = false;
    document.querySelectorAll("#stepper li").forEach((li) => (li.className = run.state === "done" ? "done" : ""));
    $("run-elapsed").textContent = typeof run.summary?.duration_s === "number" ? fmtMs(run.summary.duration_s * 1000) : "";
    if (run.verdict) {
      const banner = $("banner");
      banner.className = `banner ${run.verdict}`;
      $("banner-title").textContent = `Agent decision: ${verdictLabel(run.verdict)}`;
      const last = (run.history || []).slice(-1)[0];
      $("banner-body").textContent = last && last.note && !last.note.startsWith("Agent decision") ? last.note : "";
      banner.hidden = false;
    }
    if (run.has_result) loadResult(runId);
    return;
  }

  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  view.source = source;
  const handlers = {
    run_queued: onQueued,
    run_started: onStarted,
    phase: onPhase,
    tool_start: onToolStart,
    tool_result: onToolResult,
    run_complete: onComplete,
    run_error: onError,
  };
  Object.entries(handlers).forEach(([type, fn]) =>
    source.addEventListener(type, (e) => {
      const ev = JSON.parse(e.data);
      try {
        fn(ev);
      } catch (err) {
        console.error(`Showing the ${type} event failed`, err);
        // A run that has ended must still stop the clock and open its results, even when its
        // summary cannot be drawn (e.g. the server and this page come from different versions).
        if (type === "run_complete" || type === "run_error") {
          finishStream(ev);
          if (type === "run_complete" && ev.has_result) loadResult(view.runId);
        }
      }
    }));

  // EventSource retries on its own; just tell the user when the server has gone away.
  source.onerror = () => {
    const note = $("queue-note");
    note.textContent = "Connection to the server lost — retrying. If this persists, restart `python -m src.ui` and reload.";
    note.hidden = false;
  };
  source.onopen = () => { $("queue-note").hidden = true; };
}

function finishStream(ev) {
  if (view.source) view.source.close();
  clearInterval(view.timer);
  view.timer = null;
  // Final duration comes from the event timestamps, so a replayed run shows its real length.
  if (view.runStarted !== null) $("run-elapsed").textContent = fmtMs(ev.ts * 1000 - view.runStarted);
  refreshRunMeta(); // the run now has its final status, and actions to match
}

function onQueued(ev) {
  $("run-file").textContent = ev.filename;
  if (ev.ahead > 0) {
    const note = $("queue-note");
    note.textContent = `Waiting for ${ev.ahead} earlier run${ev.ahead > 1 ? "s" : ""} to finish…`;
    note.hidden = false;
  }
}

function onStarted(ev) {
  $("queue-note").hidden = true;
  $("run-file").textContent = ev.filename;
  if (view.run && statusInfo(view.run.status).active) setStatus("processing");
  view.runStarted = ev.ts * 1000;
  view.timer = setInterval(tickClock, 250);
  const m = ev.models || {};
  const chips = $("models");
  chips.replaceChildren();
  [["Provider", m.provider], ["Reasoning", m.reasoning], ["Vision", m.vision], ["OCR", m.ocr]]
    .filter(([, v]) => v)
    .forEach(([k, v]) => {
      const chip = el("span", "chip");
      chip.append(`${k} `, el("b", "", v));
      chips.append(chip);
    });
}

function onPhase(ev) {
  view.phase = ev.phase;
  setPhaseStates(ev.phase);
  $("timeline").append(el("div", "phase-head", `${ev.phase} · ${ev.label}`));
}

function onToolStart(ev) {
  const row = el("div", "step running");
  row.append(el("div", "step-icon"));
  const body = el("div", "step-body");
  const title = el("div", "step-title", ev.label);
  if (ev.attempt > 1) title.append(el("span", "retry", `Retry ${ev.attempt}`));
  body.append(title);
  if (ev.intent) body.append(el("div", "step-intent", ev.intent));
  if (ev.model) {
    body.append(el("div", "step-model", ev.ocr ? `${ev.model} · OCR pre-pass` : ev.model));
  }
  const timeEl = el("div", "step-time", "0ms");
  row.append(body, timeEl);
  $("timeline").append(row);
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  view.steps.set(ev.id, { el: row, body, timeEl, started: ev.ts * 1000 });
}

function onToolResult(ev) {
  const step = view.steps.get(ev.id);
  if (!step) return;
  step.started = null;
  step.el.className = `step ${ev.success ? "ok" : "fail"}`;
  step.el.querySelector(".step-icon").textContent = ev.success ? "✓" : "✗";
  step.timeEl.textContent = fmtMs(ev.elapsed_ms);
  step.body.append(el("div", "step-result", ev.primary));
  if (ev.rules && ev.rules.length) step.body.append(ruleList(ev.rules));
  if (ev.details && ev.details.length) {
    const list = el("ul", "details");
    ev.details.forEach((d) => list.append(el("li", d.level, d.text)));
    step.body.append(list);
  }
}

// A rule is shown by its English name; the id is only a hover tooltip.
const OUTCOME_ICON = { blocking: "✗", warning: "!", not_checked: "–" };

function ruleList(rules) {
  const list = el("ul", "rules");
  rules.forEach((r) => {
    const li = el("li", `rule ${r.outcome}`);
    li.title = r.rule_id;
    li.append(el("span", "rule-icon", OUTCOME_ICON[r.outcome] || "•"));
    const body = el("span", "rule-body");
    body.append(el("span", "rule-label", r.label));
    if (r.text) body.append(el("span", "rule-text", r.text));
    li.append(body);
    list.append(li);
  });
  return list;
}

function onComplete(ev) {
  const status = ev.status;
  setPhaseStates(null);
  document.querySelectorAll("#stepper li").forEach((li) => (li.className = "done"));

  const banner = $("banner");
  banner.className = `banner ${status}`;
  $("banner-title").textContent = `Agent decision: ${verdictLabel(status)}`;
  // Same buckets as every step headline: passed / blocking errors / warnings / couldn't be checked.
  const r = ev.rules;
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const parts = [
    `${r.passed} passed`,
    count(r.blocking.length, "blocking error", "blocking errors"),
    count(r.warnings.length, "warning", "warnings"),
  ];
  if (r.not_checked.length) parts.push(`${r.not_checked.length} couldn't be checked`);
  const lines = [
    `${ev.fields_extracted} fields extracted · ${ev.turns} turns`,
    `Rules: ${parts.join(" · ")}`,
  ];
  if (ev.flagged_fields.length) lines.push(`Fields flagged for review: ${ev.flagged_fields.join(", ")}`);
  if (ev.finish_reason) lines.push(`Reason: ${ev.finish_reason}`);
  $("banner-body").textContent = lines.join("\n");

  const rulesBox = $("banner-rules");
  rulesBox.replaceChildren();
  [["Blocking errors", r.blocking], ["Warnings", r.warnings], ["Couldn't be checked", r.not_checked]]
    .filter(([, rows]) => rows.length)
    .forEach(([title, rows]) => {
      rulesBox.append(el("p", "banner-section", title));
      rulesBox.append(ruleList(rows));
    });
  banner.hidden = false;
  finishStream(ev);
  if (ev.has_result) loadResult(view.runId);
}

function onError(ev) {
  setStatus("error");
  setPhaseStates(view.phase, true);
  view.steps.forEach((s) => {
    if (s.started !== null) { s.el.className = "step fail"; s.started = null; }
  });
  const banner = $("banner");
  banner.className = "banner error";
  $("banner-title").textContent = "The run could not be completed";
  $("banner-body").textContent = ev.message;
  $("banner-rules").replaceChildren();
  banner.hidden = false;
  finishStream(ev);
}

/* ---------- results: flagged PDF + clickable fields and issues ---------- */

const STATUS_LABEL = { failed: "Failed", warning: "Warning" };
const QUALITY_KIND = { confirmed: "found", matched: "found", cited_unverified: "unsure", ambiguous: "unsure", anchor: "unsure" };

const result = { data: null, pages: new Map(), highlight: null, popover: null, popoverPin: null, activeRow: null };

function resetResult() {
  closePopover();
  result.data = null;
  result.pages.clear();
  result.highlight = null;
  result.activeRow = null;
  $("viewer").replaceChildren();
  $("issue-list").replaceChildren();
  $("field-list").replaceChildren();
  $("vtab-result").disabled = true;
  if (!$("run-bar").hidden) showView("run");
}

// Run (progress) and Result (flagged PDF) are separate tabs; Result unlocks when the run has results.
function showView(name) {
  if (name === "result" && $("vtab-result").disabled) return;
  document.querySelectorAll(".view-tab").forEach((t) => {
    const on = t.dataset.view === name;
    t.classList.toggle("on", on);
    t.setAttribute("aria-selected", String(on));
  });
  $("dashboard").hidden = true;
  $("run-bar").hidden = false;
  $("run-card").hidden = name !== "run";
  $("result-card").hidden = name !== "result";
}

async function loadResult(runId) {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/result`).catch(() => null);
  if (!res || !res.ok || view.runId !== runId) return;
  renderResult(await res.json());
}

// Boxes arrive as fractions of the page image (top-left origin), so CSS percentages place them.
function placeAt(node, box) {
  node.style.left = `${box[0] * 100}%`;
  node.style.top = `${box[1] * 100}%`;
  node.style.width = `${(box[2] - box[0]) * 100}%`;
  node.style.height = `${(box[3] - box[1]) * 100}%`;
}

function renderResult(data) {
  resetResult();
  result.data = data;
  returnDraft.runId = null; // the next Return dialog drafts from these issues
  $("open-pdf").href = data.pdf_url;
  $("result-file").textContent = data.file;
  if (view.run) setStatus(view.run.status);

  const viewer = $("viewer");
  data.pages.forEach((p) => {
    const wrap = el("div", "pdf-page");
    wrap.style.aspectRatio = `1 / ${p.aspect}`;
    const img = el("img");
    img.src = p.image_url;
    img.alt = `Page ${p.page}`;
    wrap.append(img, el("span", "page-no", `Page ${p.page}`));
    p.comments.forEach((c) => {
      const pin = el("button", `pin ${c.status}`, c.ref);
      pin.type = "button";
      pin.title = c.title || `${c.ref} ${c.rule_id}`;
      placeAt(pin, c.box);
      pin.addEventListener("click", (e) => {
        e.stopPropagation();
        if (result.popoverPin === pin) clearSelection();
        else openPopover(wrap, c, pin);
      });
      wrap.append(pin);
    });
    viewer.append(wrap);
    result.pages.set(p.page, wrap);
  });

  const issues = $("issue-list");
  data.findings.forEach((f) => issues.append(issueRow(f)));
  if (!data.findings.length) issues.append(el("li", "empty", "No failed rules or warnings."));
  const fields = $("field-list");
  data.fields.forEach((f) => fields.append(fieldRow(f)));

  $("tab-issues").textContent = `Issues (${data.findings.length})`;
  $("tab-fields").textContent = `Fields (${data.fields.filter((f) => f.target).length}/${data.fields.length})`;
  showTab(data.findings.length ? "issues" : "fields");
  $("vtab-result").disabled = false;
  showView("result");
  window.scrollTo(0, 0);
}

function issueRow(f) {
  const li = el("li", `item issue ${f.status}`);
  li.dataset.ref = f.ref;
  li.title = f.rule_id;
  const head = el("div", "item-head");
  head.append(el("span", `badge ${f.status}`, STATUS_LABEL[f.status] || humanize(f.status)), el("span", "item-ref", `${f.ref} · ${f.title}`));
  if (f.target) head.append(el("span", "page-chip", `p${f.target.page}`));
  li.append(head);
  if (f.reason) li.append(el("div", "item-title", f.reason));
  li.append(el("div", "item-comment", f.comment));
  makeClickable(li, f.target, f.status);
  return li;
}

function fieldRow(f) {
  const found = f.value !== null && f.value !== "";
  const kind = !found ? "missing" : f.needs_review ? "review" : QUALITY_KIND[f.quality] || "unlocated";
  const li = el("li", `item field ${f.edit ? "edited" : kind}`);
  li.dataset.field = f.field_name;
  const head = el("div", "item-head");
  head.append(el("span", "dot"), el("span", "item-label", f.label));
  if (f.edit) head.append(el("span", "badge edited", "Edited"));
  if (f.target) head.append(el("span", "page-chip", `p${f.target.page}`));
  const edit = el("button", "edit-btn", "Edit");
  edit.type = "button";
  edit.setAttribute("aria-label", `Edit ${f.label}`);
  edit.addEventListener("click", (e) => { e.stopPropagation(); openFieldEditor(li, f); });
  head.append(edit);
  li.append(head, el("div", "item-value", f.display));

  const meta = [];
  if (f.edit) {
    meta.push(`AI read: ${f.edit.original}`, `edited ${fmtWhen(f.edit.at)}`);
  } else {
    if (found && typeof f.confidence === "number") meta.push(`confidence ${f.confidence.toFixed(2)}`);
    if (found && !f.target) meta.push("location not found on the page");
    else if (found && !f.target.box) meta.push("page only");
    if (f.review_reason) meta.push(f.review_reason);
  }
  if (meta.length) li.append(el("div", "item-meta", meta.join(" · ")));
  makeClickable(li, f.target, kind);
  return li;
}

// Inline editor for one field. Saved values are kept with the run; the checks are not re-run.
function openFieldEditor(li, f) {
  if (li.querySelector(".field-editor")) return;
  const found = f.value !== null && f.value !== "";
  const form = el("form", "field-editor");
  const input = el("input");
  input.type = "text";
  input.value = found ? f.display : "";
  input.setAttribute("aria-label", `New value for ${f.label}`);
  input.maxLength = 2000;
  const save = el("button", "btn", "Save");
  save.type = "submit";
  const cancel = el("button", "btn secondary", "Cancel");
  cancel.type = "button";
  const buttons = el("div", "editor-buttons");
  buttons.append(save, cancel);
  if (f.edit) {
    const undo = el("button", "btn secondary", "Undo edit");
    undo.type = "button";
    undo.title = `Go back to what the AI read: ${f.edit.original}`;
    undo.addEventListener("click", () => saveField(li, f, null, form));
    buttons.append(undo);
  }
  const error = el("p", "error-text editor-error");
  error.hidden = true;
  form.append(input, buttons, error);

  const value = li.querySelector(".item-value");
  value.hidden = true;
  const close = () => { form.remove(); value.hidden = false; };
  cancel.addEventListener("click", close);
  form.addEventListener("submit", (e) => { e.preventDefault(); saveField(li, f, input.value, form); });
  // Keys and clicks stay in the editor: the row itself jumps to the field on the page.
  form.addEventListener("click", (e) => e.stopPropagation());
  form.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") close();
  });
  value.after(form);
  input.focus();
  input.select();
}

async function saveField(li, f, value, form) {
  const url = `/api/runs/${encodeURIComponent(view.runId)}/fields/${encodeURIComponent(f.field_name)}`;
  const opts = value === null
    ? { method: "DELETE" }
    : { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value }) };
  form.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const res = await fetch(url, opts).catch(() => null);
  if (!res || !res.ok) {
    const err = res ? await res.json().catch(() => ({})) : {};
    const box = form.querySelector(".editor-error");
    box.textContent = typeof err.detail === "string" ? err.detail : "Could not save. Is the server still running?";
    box.hidden = false;
    form.querySelectorAll("button").forEach((b) => (b.disabled = false));
    return;
  }
  const row = fieldRow(await res.json());
  if (result.activeRow === li) clearSelection();
  li.replaceWith(row);
}

function makeClickable(li, target, kind) {
  if (!target) { li.classList.add("disabled"); return; }
  li.tabIndex = 0;
  li.setAttribute("role", "button");
  // A second click on the active row closes it again (comment collapsed, highlight removed).
  const go = () => {
    if (result.activeRow === li) { clearSelection(); return; }
    setActiveRow(li);
    jumpTo(target, kind);
  };
  li.addEventListener("click", go);
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
}

function clearSelection() {
  closePopover();
  if (result.activeRow) result.activeRow.classList.remove("active");
  result.activeRow = null;
  if (result.highlight) result.highlight.remove();
}

function setActiveRow(li) {
  if (result.activeRow) result.activeRow.classList.remove("active");
  result.activeRow = li;
  li.classList.add("active");
}

function jumpTo(target, kind) {
  const wrap = result.pages.get(target.page);
  if (!wrap) return;
  closePopover();
  const viewer = $("viewer");
  if (!result.highlight) result.highlight = el("div", "hl");
  const hl = result.highlight;
  hl.className = `hl ${kind}`;
  let top = wrap.offsetTop;
  let left = 0;
  if (target.box) {
    const pad = 0.006;
    placeAt(hl, [target.box[0] - pad, target.box[1] - pad, target.box[2] + pad, target.box[3] + pad]);
    wrap.append(hl);
    top += target.box[1] * wrap.clientHeight - viewer.clientHeight * 0.25;
    const boxRight = target.box[2] * wrap.clientWidth;
    if (boxRight > viewer.clientWidth) left = boxRight - viewer.clientWidth + 24;
    void hl.offsetWidth; // restart the pulse animation
    hl.classList.add("pulse");
  } else {
    hl.remove();
    top -= 8;
  }
  viewer.scrollTo({ top: Math.max(0, top), left, behavior: "smooth" });
}

function openPopover(wrap, comment, pin) {
  closePopover();
  const pop = el("div", `popover ${comment.status}`);
  pop.setAttribute("role", "dialog");
  pop.append(el("div", "popover-title", comment.title || `${comment.ref} ${comment.rule_id}`), el("div", "popover-body", comment.text));
  // Pins sit in the right margin, so the popover opens to their left, below the pin.
  pop.style.right = `${(1 - comment.box[0]) * 100}%`;
  pop.style.top = `${comment.box[3] * 100}%`;
  pop.addEventListener("click", (e) => e.stopPropagation());
  wrap.append(pop);
  result.popover = pop;
  pin.classList.add("open");
  result.popoverPin = pin;
  const row = [...document.querySelectorAll("#issue-list .issue")]
    .find((li) => li.dataset.ref === comment.ref);
  if (row) {
    showTab("issues");
    setActiveRow(row);
    // Scroll the list only; scrollIntoView would also move the page and the viewer.
    const list = $("issue-list");
    list.scrollTo({ top: row.offsetTop - 8, behavior: "smooth" });
  }
}

function closePopover() {
  if (result.popover) result.popover.remove();
  if (result.popoverPin) result.popoverPin.classList.remove("open");
  result.popover = null;
  result.popoverPin = null;
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("on", on);
    t.setAttribute("aria-selected", String(on));
  });
  $("issue-list").hidden = name !== "issues";
  $("field-list").hidden = name !== "fields";
}

function initResult() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  document.querySelectorAll(".view-tab").forEach((t) => t.addEventListener("click", () => {
    showView(t.dataset.view);
    window.scrollTo(0, 0);
  }));
  document.addEventListener("click", closePopover);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });
}

/* ---------- boot ---------- */

/* ---------- pages: #/ is the dashboard, #/runs/<id> one run ---------- */

/* ---------- home: today's desk ---------- */

const home = { timer: null };
const LAST_RUN_KEY = "impendo.lastRun";

// Per-browser convenience only: losing it just hides the "pick up where you left off" card.
function rememberLastRun(runId) {
  try { localStorage.setItem(LAST_RUN_KEY, JSON.stringify({ id: runId, at: Date.now() })); } catch (e) { /* ignore */ }
}

function lastRun() {
  try { return JSON.parse(localStorage.getItem(LAST_RUN_KEY) || "null"); } catch (e) { return null; }
}

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const runLink = (id) => `#/runs/${encodeURIComponent(id)}`;

// The home tiles: each opens the invoice list filtered to its statuses.
const HOME_TILES = [
  { statuses: ["needs_review"], label: "Need your review", tone: "warn" },
  { statuses: ["returned"], label: "Returned", tone: "bad" },
  { statuses: ["new", "processing"], link: "processing", label: "Being checked", tone: "running" },
  { statuses: ["finalized"], label: "Finalized", tone: "ok" },
];

function moveUploadCard(slot) {
  const card = $("upload-card");
  if (card.parentElement !== slot) slot.prepend(card);
}

function showHome() {
  resetView();
  clearTimeout(dash.timer);
  ["run-bar", "run-card", "result-card", "dashboard"].forEach((id) => ($(id).hidden = true));
  $("home").hidden = false;
  moveUploadCard($("home-upload-slot"));
  document.title = "Impendo";
  $("hero-greeting").textContent = greeting();
  $("hero-date").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  loadHome();
}

async function loadHome() {
  clearTimeout(home.timer);
  const res = await fetch("/api/invoices").catch(() => null);
  if ($("home").hidden) return;
  if (!res || !res.ok) {
    $("hero-summary").textContent = "Could not load the invoices. Is `python -m src.ui` still running?";
    return;
  }
  const invoices = await res.json();
  renderHome(invoices);
  if (invoices.some((r) => statusInfo(r.status).active)) home.timer = setTimeout(loadHome, DASH_REFRESH_MS);
}

function renderHome(invoices) {
  const count = (...ids) => invoices.filter((r) => ids.includes(r.status)).length;
  const review = count("needs_review");
  const returned = count("returned");
  const checking = count("new", "processing");
  const errors = count("error");

  // One sentence that says what needs attention today.
  const summary = $("hero-summary");
  summary.replaceChildren();
  const parts = [];
  if (review) parts.push(el("b", "", `${plural(review, "invoice needs", "invoices need")} your review`));
  if (checking) parts.push(document.createTextNode(`${plural(checking, "invoice is", "invoices are")} being checked right now`));
  if (returned) parts.push(document.createTextNode(`${plural(returned, "invoice was", "invoices were")} returned for revision`));
  if (!invoices.length) parts.push(document.createTextNode("No invoices yet. Upload the first one below to get started."));
  else if (!review && !checking && !errors) parts.unshift(el("b", "", "You're all caught up."));
  parts.forEach((p, i) => { if (i) summary.append(" · "); summary.append(p); });
  $("hero-cta").hidden = !review;

  const tiles = $("home-tiles");
  tiles.replaceChildren(...HOME_TILES.map((t) => {
    const a = el("a", `tile tone-${t.tone}`);
    a.href = `#/invoices?status=${t.link || t.statuses[0]}`;
    a.setAttribute("role", "listitem");
    a.append(el("span", "tile-n", String(count(...t.statuses))), el("span", "tile-label", t.label));
    return a;
  }));

  const alert = $("home-alert");
  alert.replaceChildren();
  if (errors) {
    const link = el("a", "", "Show them");
    link.href = "#/invoices?status=error";
    alert.append(`${plural(errors, "invoice", "invoices")} stopped with a technical error and should be re-evaluated. `, link);
  }
  alert.hidden = !errors;

  renderResume(invoices);
  renderActivity(invoices);
}

async function renderResume(invoices) {
  const last = lastRun();
  const card = $("resume-card");
  let run = last && invoices.find((r) => r.run_id === last.id);
  if (last && !run) {
    // An older run of an invoice that was re-checked since: still worth offering.
    const res = await fetch(`/api/runs/${encodeURIComponent(last.id)}`).catch(() => null);
    run = res && res.ok ? await res.json() : null;
  }
  card.hidden = !run;
  if (!run) return;
  const link = $("resume-link");
  link.href = runLink(run.run_id);
  link.replaceChildren(
    el("span", "resume-name", run.filename),
    statusPill(run.status),
    el("span", "resume-when", `opened ${fmtAgo(last.at / 1000)}`),
  );
}

function renderActivity(invoices) {
  // Decisions only (agent, reviewer, errors), not the queue's own New / Processing steps.
  const events = invoices.flatMap((r) => (r.history || [])
    .filter((h) => h.by !== "system" || h.status === "error")
    .map((h) => ({ ...h, run: r })));
  events.sort((a, b) => b.at - a.at);
  const list = $("activity-list");
  list.replaceChildren(...events.slice(0, 8).map((e) => {
    const li = el("li", "activity");
    const a = el("a", "activity-link");
    a.href = runLink(e.run.run_id);
    const body = el("span", "activity-body");
    body.append(el("span", "activity-name", e.run.filename));
    const who = e.by === "reviewer" ? "by a reviewer" : "";
    body.append(el("span", "activity-note", [statusInfo(e.status).label, who].filter(Boolean).join(" ")));
    const when = el("span", "activity-when", fmtAgo(e.at));
    when.title = fmtWhen(e.at);
    a.append(el("span", `activity-dot tone-${statusInfo(e.status).tone}`), body, when);
    li.append(a);
    return li;
  }));
  $("activity-empty").hidden = events.length > 0;
}

/* ---------- pages: #/ home, #/invoices the list, #/runs/<id> one run ---------- */

function setNav(page) {
  document.querySelectorAll(".main-nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === page));
}

function showDashboard(filter) {
  resetView();
  clearTimeout(home.timer);
  $("run-bar").hidden = true;
  $("run-card").hidden = true;
  $("result-card").hidden = true;
  $("home").hidden = true;
  $("dashboard").hidden = false;
  moveUploadCard($("dashboard"));
  if (filter) dash.filter = filter;
  document.title = "Invoices · Impendo";
  loadRuns();
}

async function showRunPage(runId) {
  clearTimeout(dash.timer);
  clearTimeout(home.timer);
  $("home").hidden = true;
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`).catch(() => null);
  if (currentRunId() !== runId) return; // the user navigated on meanwhile
  if (!res || !res.ok) {
    location.hash = "#/";
    return;
  }
  const run = await res.json();
  document.title = `${run.filename} · Impendo`;
  rememberLastRun(run.run_id);
  openRun(run);
  window.scrollTo(0, 0);
}

function currentRunId() {
  // "#<id>" is the address older versions of this page used.
  const m = location.hash.match(/^#\/runs\/([^/?#]+)$/) || location.hash.match(/^#([0-9a-f]{12})$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function route() {
  const runId = currentRunId();
  const list = location.hash.match(/^#\/invoices(?:\?status=([a-z_]+))?$/);
  if (runId) {
    setNav("invoices");
    showRunPage(runId);
  } else if (list) {
    setNav("invoices");
    showDashboard(list[1] || "all");
  } else {
    setNav("home");
    showHome();
  }
}

async function boot() {
  initDropzone();
  initResult();
  initReturnDialog();
  await loadCatalog();
  window.addEventListener("hashchange", route);
  route();
}

boot();
