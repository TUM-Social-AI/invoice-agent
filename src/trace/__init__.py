"""
Traceability: where on the page each extracted value and compliance finding comes from.

The agent records evidence while it runs (OCR line citations, rule refs). After the run,
`finalize.write_trace_outputs` grounds that evidence to boxes, writes `trace.json` and
renders the flagged PDF from it.
"""
