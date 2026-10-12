"use strict";

const $ = (id) => document.getElementById(id);
const PHASES = ["SCAN", "EXTRACT", "VALIDATE"];

const view = {
  runId: null,
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
    const run = await res.json();
    location.hash = run.run_id;
    openRun(run.run_id, run.filename);
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
  $("new-run").addEventListener("click", () => {
    location.hash = "";
    resetView();
    $("run-card").hidden = true;
    $("view-tabs").hidden = true;
    $("upload-card").hidden = false;
  });
}

/* ---------- run view ---------- */

function resetView() {
  if (view.source) view.source.close();
  clearInterval(view.timer);
  Object.assign(view, { runId: null, source: null, phase: null, runStarted: null, timer: null });
  view.steps.clear();
  $("timeline").replaceChildren();
  resetResult();
  $("banner").hidden = true;
  $("queue-note").hidden = true;
  $("new-run").hidden = true;
  $("models").replaceChildren();
  $("run-elapsed").textContent = "0.0s";
  setPhaseStates(null);
  setState("queued", "Queued");
}

function setState(kind, label) {
  const pill = $("run-state");
  pill.className = `pill ${kind}`;
  pill.textContent = label;
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

function openRun(runId, filename) {
  resetView();
  view.runId = runId;
  $("upload-card").hidden = true;
  $("view-tabs").hidden = false;
  showView("run");
  $("run-file").textContent = filename || "…";

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
  $("new-run").hidden = false;
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
  setState("running", "Running");
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
  const pillKind = { passed: "done", failed: "error", error: "error" }[status] || "warn";
  setState(pillKind, status.replace("_", " "));
  setPhaseStates(null);
  document.querySelectorAll("#stepper li").forEach((li) => (li.className = "done"));

  const banner = $("banner");
  banner.className = `banner ${status}`;
  $("banner-title").textContent = `Decision: ${status.replace("_", " ").toUpperCase()}`;
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
  setState("error", "Error");
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

const result = { pages: new Map(), highlight: null, popover: null, popoverPin: null, activeRow: null };

function resetResult() {
  closePopover();
  result.pages.clear();
  result.highlight = null;
  result.activeRow = null;
  $("viewer").replaceChildren();
  $("issue-list").replaceChildren();
  $("field-list").replaceChildren();
  $("vtab-result").disabled = true;
  if (!$("view-tabs").hidden) showView("run");
}

// Run (progress) and Result (flagged PDF) are separate tabs; Result unlocks when the run has results.
function showView(name) {
  if (name === "result" && $("vtab-result").disabled) return;
  document.querySelectorAll(".view-tab").forEach((t) => {
    const on = t.dataset.view === name;
    t.classList.toggle("on", on);
    t.setAttribute("aria-selected", String(on));
  });
  $("run-card").hidden = name !== "run";
  $("result-card").hidden = name !== "result";
  document.querySelector("main").classList.toggle("wide", name === "result");
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
  $("open-pdf").href = data.pdf_url;
  $("result-file").textContent = data.file;
  const status = data.status || "";
  $("result-status").className = `pill ${{ passed: "done", failed: "error", error: "error" }[status] || "warn"}`;
  $("result-status").textContent = status.replace("_", " ");

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
      pin.title = `${c.ref} ${c.rule_id}`;
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
  const head = el("div", "item-head");
  head.append(el("span", `badge ${f.status}`, STATUS_LABEL[f.status] || f.status), el("span", "item-ref", `${f.ref} ${f.rule_id}`));
  if (f.target) head.append(el("span", "page-chip", `p${f.target.page}`));
  li.append(head, el("div", "item-title", f.title), el("div", "item-comment", f.comment));
  makeClickable(li, f.target, f.status);
  return li;
}

function fieldRow(f) {
  const found = f.value !== null && f.value !== "";
  const kind = !found ? "missing" : f.needs_review ? "review" : QUALITY_KIND[f.quality] || "unlocated";
  const li = el("li", `item field ${kind}`);
  const head = el("div", "item-head");
  head.append(el("span", "dot"), el("span", "item-label", f.label));
  if (f.target) head.append(el("span", "page-chip", `p${f.target.page}`));
  li.append(head, el("div", "item-value", f.display));
  const meta = [];
  if (found && typeof f.confidence === "number") meta.push(`confidence ${f.confidence.toFixed(2)}`);
  if (found && !f.target) meta.push("location not found on the page");
  else if (found && !f.target.box) meta.push("page only");
  if (f.review_reason) meta.push(f.review_reason);
  if (meta.length) li.append(el("div", "item-meta", meta.join(" · ")));
  makeClickable(li, f.target, kind);
  return li;
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
  pop.append(el("div", "popover-title", `${comment.ref} ${comment.rule_id}`), el("div", "popover-body", comment.text));
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

async function boot() {
  initDropzone();
  initResult();
  const runId = location.hash.slice(1);
  if (!runId) return;
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`).catch(() => null);
  if (res && res.ok) {
    const run = await res.json();
    openRun(run.run_id, run.filename);
  } else {
    location.hash = "";
  }
}

boot();
