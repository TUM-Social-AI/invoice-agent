# Agent Learnings

This file is read by the agent at the start of each run and written to when new patterns are discovered.
Edit manually to add domain knowledge; the agent will append its own discoveries.

Format:
- `## GENERAL` — cross-type approaches, strategies, and tool suggestions
- `## <TYPE_ID>` — per-type patterns, failures, and edge cases

Categories per section:
- `### approaches` — what sequence of steps worked (or failed) — always write one before finish
- `### extraction_patterns` — how to reliably find specific fields
- `### common_failures` — what goes wrong and how to recover
- `### compliance_edge_cases` — rule exceptions and valid special cases
- `### tool_suggestions` — tools that would have helped but don't exist yet

---

## GENERAL

### approaches
- [initial] Standard flow: read_learnings → inspect_file → compress_pages (if large) or convert_pdf_to_images → classify_document_type → extract page 1 → check_compliance → crop and retry failing fields → write approach → finish

### extraction_patterns
- Always call inspect_file first to understand file size and page count before rendering
- For files >8 MB, use compress_pages before extract_fields_vision to avoid timeouts
- The first page usually contains the most identifying information for classification
- Spanish invoices use comma as decimal separator (1.234,56 EUR) — parse accordingly
- Dates in Spanish documents: DD/MM/YYYY or "15 de enero de 2024"
- **Scanned PDFs**: many project PDFs have **no usable text layer** (PDF text extraction returns empty). Rely on **OCR pre-pass + vision** after `convert_pdf_to_images`; do not assume copy-paste text exists.
- **Chad / JRS / XAF corpus**: documents are often **French** with amounts in **XAF (FCFA)** as whole numbers (spaces as thousands separators). Do not assume EUR, Spanish labels, or EU IVA blocks.

### compliance_edge_cases
- Rules that require **Xunta de Galicia stamps**, **PR811A**, or **2023-only** execution are tied to a **specific EU grant** profile. **NGO field documents** (Africa, XAF, local suppliers, no European seal) may **fail those visual checks** even when field extraction is correct — treat as **jurisdiction / rule-pack mismatch**, not as “bad OCR,” until rule packs are split by program.

### tool_suggestions
- (none recorded yet)

---

## VIAJES

### approaches
- [initial] Page 1 for vendor, date, beneficiary and total. For multi-page expense reports, check each page for individual receipts. Per diem tables are usually on the last page.

### extraction_patterns
- Hotel invoices: guest name = beneficiary, check-in/check-out dates define the period
- Airline tickets: passenger name = beneficiary, destination from routing (e.g. MAD-LIS)
- Per diem tables: look for rows with "días", "dieta", "tarifa" — multiply to get total
- Taxi/transport receipts may only have a total and date, no vendor name — flag for review
- "Factura simplificada" (simplified invoice) may lack NIF and buyer details
- **Reference PDF** (repo root): `A.6.- Viajes, alojamientos y dietas-A3693-25.pdf` — **image-only** scan; **French**; **XAF**; line items may be **carburant / gasoil** from a local supplier (**ETS …**). **Client** may be a **bureau / JRS office**, not a named individual traveler — map to beneficiary or flag for review per schema.
- **Payment method** may read like **“Bank-JRS / …”** or mixed French shorthand — extract **verbatim** into `payment_method`.

### common_failures
- (none recorded yet)

### compliance_edge_cases
- International travel invoices may have 0% IVA — this is valid, do not flag R_VIA_008
- Per diem calculations: if total ≠ days × rate but difference is small, may include other expenses

---

## PERS_LOCAL

### approaches
- [initial] Nómina header for employee name and period, body table for gross/deductions, footer for net (líquido a percibir). SS empresa may be on a separate document (RNT/TC2).

### extraction_patterns
- "Total devengos" = gross salary; "Total deducciones" = sum of IRPF + SS employee
- "Líquido a percibir" = net salary — always in the totals section at the bottom
- IRPF retention appears as a deduction line labeled "IRPF" or "Retención"
- SS employee appears as "Cuota obrera" or "Contingencias comunes empleado"
- Porcentaje de imputación may appear as a handwritten annotation or in a cover letter

### common_failures
- (none recorded yet)

### compliance_edge_cases
- Part-time contracts: gross may be low — do not flag as suspicious without context
- If project_allocation_pct is missing, flag for human review (needed for cost justification)

---

## PERS_SEDE

### approaches
- [initial] Same structure as PERS_LOCAL. Additionally look for allocation percentage on cover sheets or project timesheets attached as extra pages.

### extraction_patterns
- Sede invoices may be in EUR with different SS rates than local staff
- Dedication percentage (% imputación) is critical — check all pages including attachments

### common_failures
- (none recorded yet)

### compliance_edge_cases
- (none recorded yet)

---

## EQUIPOS

### approaches
- [initial] Header for vendor and invoice number, line items section for equipment description and unit prices, totals section for base imponible + IVA + total.

### extraction_patterns
- Equipment descriptions often span multiple lines — extract the full description including model/reference
- "Base imponible" = net amount before IVA
- IVA in Spain: 21% standard for most equipment, 10% for some medical/educational items
- Albarán (delivery note) may accompany the invoice — the invoice is the billable document

### common_failures
- (none recorded yet)

### compliance_edge_cases
- Equipment purchased outside Spain may show 0% IVA (reverse charge or import) — valid

---

## CONSUMIBLES

### approaches
- [initial] Often simpler documents (tickets, facturas simplificadas). Extract date and total first. Description may be a list of items — summarize if too long.

### extraction_patterns
- Supermarket/office supply tickets: look for "TOTAL" at the bottom
- Facturas simplificadas: may only have NIF of vendor, no buyer details — this is normal
- IVA may be shown as a breakdown at the bottom (Base 21% + Cuota IVA)
- **Reference PDF** (repo root): `A.4.c.- Consumibles-A4048A-25.pdf` — **image-only** scan; **French**; **XAF** with large **integer totals** (e.g. millions FCFA); vendor often **ETS …**; **Baga-Sola** / Chad basin localities; **JRS** or project bureau as client; payment may be **Chèque**, transfer, or “payé par …”.
- Kits / **fournitures** for programs: prefer the **clearest line description** for item/description fields when the table is dense.

### common_failures
- [2026-03-26] Insurance premium or non-supply documents can be misclassified as CONSUMIBLES — verify document type against line items (premiums vs physical goods) before trusting extraction.

### compliance_edge_cases
- Tickets under 400 EUR in Spain are valid as facturas simplificadas without buyer NIF
