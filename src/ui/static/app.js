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
  $("run-card").hidden = false;
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
    source.addEventListener(type, (e) => fn(JSON.parse(e.data))));

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
  if (ev.details && ev.details.length) {
    const list = el("ul", "details");
    ev.details.forEach((d) => list.append(el("li", d.level, d.text)));
    step.body.append(list);
  }
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
  const lines = [
    `${ev.fields_extracted} fields extracted · ${ev.turns} turns · ${ev.rules_passed} rules passed · ` +
      `${ev.blocking_errors.length} blocking error(s) · ${ev.warnings.length} warning(s)`,
  ];
  if (ev.blocking_errors.length) lines.push(`Blocking errors: ${ev.blocking_errors.join(", ")}`);
  if (ev.warnings.length) lines.push(`Warnings: ${ev.warnings.join(", ")}`);
  if (ev.flagged_fields.length) lines.push(`Flagged for review: ${ev.flagged_fields.join(", ")}`);
  if (ev.finish_reason) lines.push(`Reason: ${ev.finish_reason}`);
  $("banner-body").textContent = lines.join("\n");
  banner.hidden = false;
  finishStream(ev);
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
  banner.hidden = false;
  finishStream(ev);
}

/* ---------- boot ---------- */

async function boot() {
  initDropzone();
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
