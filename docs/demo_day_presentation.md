---
marp: true
theme: default
paginate: true
style: |
  section {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #ffffff;
    color: #1a1a2e;
  }
  section.lead {
    background: #1a1a2e;
    color: #ffffff;
    text-align: center;
  }
  section.lead h1 {
    font-size: 2.4em;
    color: #e8f4fd;
    margin-bottom: 0.2em;
  }
  section.lead h2 {
    font-size: 1.2em;
    color: #a8d8ea;
    font-weight: 400;
  }
  section.lead p {
    color: #c0d8e8;
    font-size: 0.9em;
  }
  h1 { color: #1a1a2e; font-size: 1.7em; border-bottom: 3px solid #3498db; padding-bottom: 0.2em; }
  h2 { color: #2c3e50; font-size: 1.2em; }
  .highlight { background: #eaf4fb; border-left: 4px solid #3498db; padding: 0.6em 1em; margin: 0.5em 0; border-radius: 0 6px 6px 0; }
  table { font-size: 0.8em; }
  th { background: #1a1a2e; color: white; }
  code { background: #f0f4f8; padding: 0.1em 0.4em; border-radius: 4px; font-size: 0.85em; }
  ul li { margin-bottom: 0.3em; }
---

<!-- _class: lead -->

# Invoice Compliance Agent

## Automated invoice auditing for grant programs — end to end

SociAI · Demo Day 2026

---

# The Problem

Grant programs receive **hundreds of invoices** across multiple vendors, travel bookings, personnel costs, and equipment purchases.

Auditors must manually verify each invoice against a ruleset of **29+ compliance checks**:

- Is the vendor registered?
- Are amounts within approved thresholds?
- Does the invoice carry a valid stamp and signature?
- Is the expense category eligible under grant rules?

<div class="highlight">
Manual review is slow, error-prone, and does not scale. A missed rule can mean a rejected reimbursement claim.
</div>

---

# The Solution

An **agentic AI system** that takes a scanned PDF invoice and produces a full compliance audit — automatically.

```
PDF invoice  →  Invoice Agent  →  Compliance Report
```

- Extracts all structured fields from any invoice layout
- Evaluates deterministic rules (amounts, dates, formats) in code
- Evaluates judgment rules (stamps, signatures, adequacy) with vision AI
- Flags ambiguous cases for human review
- Learns from every run to improve future extractions

**Target program:** XUNTA DE GALICIA regional grant auditing
**Invoice types covered:** Travel, Local Personnel, HQ Personnel, Equipment, Consumables

---

# Workflow Overview

```
┌──────────────┐    ┌──────────────────┐    ┌───────────────────┐
│  SCAN phase  │ →  │  EXTRACT phase   │ →  │  VALIDATE phase   │
└──────────────┘    └──────────────────┘    └───────────────────┘

Understand the       Pull structured          Run all rules,
document structure   data from every page     resolve, report
```

Three phases, each exposing a **narrowed toolset** to the AI — the agent cannot skip steps or call tools out of sequence.

---

# Phase 1 — Structure Scan

The agent first builds a lightweight map of the document using **low-resolution thumbnails**.

1. `inspect_file` — read metadata, check file size
2. `compress_pages` — render all pages at 48 DPI (fast, cheap)
3. `inventory_pages` — vision AI classifies every page:
   `INVOICE_HEADER · LINE_ITEMS · TOTALS · SIGNATURE_STAMP · SUPPORTING_DOC`
4. `classify_document_type` — identify which of the 5 invoice types this is
5. *(Optional)* **Planning step** — reasoning model generates a step-by-step extraction plan tailored to the detected invoice type

At the end of SCAN, the agent knows exactly what is on each page before touching full-quality rendering.

---

# Phase 2 — Targeted Extraction

Full-quality rendering + field extraction using vision AI and OCR in parallel.

| Step | Tool | What happens |
|------|------|-------------|
| Render | `convert_pdf_to_images` | 200 DPI per page |
| Extract | `extract_fields_vision` | Vision LLM + configured OCR pre-pass per page |
| Retry | `crop_region` | Zoom into header / footer / totals if a field was missed |
| Note | `note` | Agent records private observations for this session |
| Compliance | `check_compliance` | Deterministic rule evaluation — no LLM needed |
| Visual check | `check_compliance_visual` | Batched vision check: stamps, seals, signatures |

The agent **never overwrites a higher-confidence value** — confidence-aware merging throughout.

---

# Phase 3 — Validate & Finish

After extraction and compliance checks:

- Retry cycles for any failed or low-confidence fields
- `flag_for_human_review` for genuinely ambiguous cases
- `write_learning` to persist new insights for future runs
- `finish` with one of: `compliance_passed · human_review_needed · max_retries · unrecoverable_error`

**Output per invoice:**

```
output/<invoice>/
  ├── results_<ts>.csv        ← extracted fields + confidence + source page
  ├── compliance_<ts>.csv     ← pass/fail per rule + messages
  ├── pages/                  ← full-quality page images
  └── logs/agent_log_<ts>.jsonl ← full turn-by-turn trace
```

---

# The Toolbox

The agent has **18 tools** across four categories:

| Category | Tools |
|----------|-------|
| **Document prep** | `inspect_file`, `compress_pages`, `convert_pdf_to_images`, `crop_region` |
| **AI extraction** | `inventory_pages`, `classify_document_type`, `extract_fields_vision`, `check_compliance_visual` |
| **Deterministic compliance** | `check_compliance` |
| **Memory & control** | `read_learnings`, `write_learning`, `edit_learning`, `delete_learning`, `note`, `flag_for_human_review`, `install_package`, `finish` |

The agent **only sees the tools relevant to its current phase** — a phase-gated policy prevents hallucinated shortcuts.

---

# The Learning System

After every run (in `--learn` mode), a **reflection loop** compares agent output against ground truth:

1. Receives the field-level diff (what was wrong, what was missed)
2. Has access only to learning CRUD tools
3. Writes targeted learnings to `learnings/learnings.md`

Categories: `extraction_patterns · vision_model_extraction · common_failures · compliance_edge_cases · approaches`

On the **next run**, learnings for the detected invoice type are injected into the system prompt — the agent builds up institutional knowledge that transfers across every invoice it processes.

---

# Technical Architecture

```
main.py
  └─ InvoiceAgent
       ├─ AgentState          ← single mutable object per run (Pydantic)
       ├─ LLMProvider         ← Gemini (cloud) or Ollama (fully local)
       ├─ ToolRegistry        ← 18 closures, phase-gated
       └─ agent_turn()
            ├─ build_system_prompt()   prompt + tool docs + learnings
            ├─ build_response_schema() constrained JSON (tool + params)
            └─ validate_action_contract()  repair-retry on bad output
```

**Dual model split:**

| Role | Gemini | Ollama (local) |
|------|--------|----------------|
| Reasoning / tool selection | `gemini-2.5-flash` | `qwen3:1.7b` |
| Vision / extraction | `gemini-2.5-pro` | `qwen2.5vl:32b` |

---

# Configuration-Driven — Zero Code Changes to Extend

All invoice types, fields, and rules live in CSV files:

```
config/csv/
  ├── invoice_types.csv        ← 5 types with context hints for the LLM
  ├── extraction_fields.csv    ← per-type fields, page regions, extraction hints
  └── compliance_rules.csv     ← 29+ rules: required, regex, range, enum,
                                  cross_field, conditional_check, visual_check
```

To add a new invoice type: add a row to each CSV. No Python required.

**Rule tiers:**
- **Code** — required fields, regex, numeric ranges, cross-field logic
- **Vision** — stamps, signatures, seals, adequacy judgments
- **Cross-page** — proof of payment, supporting documents, translations

---

# Safety & Reliability

| Challenge | How it's handled |
|-----------|-----------------|
| Agent loops / repetition | `DuplicateActionGuard` detects repeated identical actions |
| Cascading failures | `ConsecutiveFailureGuard` triggers human escalation |
| Bad JSON from LLM | One automatic repair-retry with explicit validation feedback |
| Ambiguous fields | `flag_for_human_review` — never silently drops data |
| Cost overrun | `MeteredLLMProvider` enforces per-run request + token caps |
| Model failure | Adaptive fallback routing to secondary reasoning model |
| Cloud dependency | Fully local mode via Ollama — no data leaves the machine |

---

# Demo

**Input:** Scanned PDF invoice (travel expense, VIAJES type)

**What you'll see:**
1. Phase-by-phase progress as the agent works through the document
2. Extracted fields with confidence scores and source page references
3. Compliance report — pass / fail / flagged per rule
4. Agent reasoning trace (why it chose each tool)

---

<!-- _class: lead -->

# Thank You

**Invoice Compliance Agent**

Automated, auditable, extensible grant invoice processing

Questions?
