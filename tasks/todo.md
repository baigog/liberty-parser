# TODO: Liberty parser + comparator

Full detail in [plan.md](plan.md). Gates are blocking.

Status: all nine implementation tasks done, 34 checks green. The two gates that
need a **real advanced-node library on your server** are open — everything else
is verified locally.

## Phase 1: Parser Foundation
- [x] **Task 1** — `sample.lib` adversarial fixture + master-regex tokenizer
- [x] **Task 2** — `Group` model + recursive-descent parser
- [x] **Task 3** — located `LibertySyntaxError`, `skip_groups`, gzip

## Phase 2: Parser Usability
- [x] **Task 4** — accessors (`get`/`get_all`/`find`/`find_all`/`to_dict`/`path`) + CLI

### GATE 1 — Parser core (blocking)
- [x] `python3.7 test_liberty.py` exits 0 — 34 checks
- [x] 3.7 grammar enforced locally via `ast.parse(feature_version=(3,7))`
      (check `sources_are_python_37_compatible`)
- [ ] `python3.7 -m compileall liberty_parser.py liberty_compare.py` **on the 3.7 server**
      — belt-and-braces; the ast check above already covers syntax
- [x] stdlib-only imports (check `sources_import_stdlib_only`, enforced by allowlist)
- [x] malformed input gives a located error, not a bare traceback

### GATE 2 — Real advanced-node library (blocking, highest risk) — YOURS TO RUN
- [ ] `python3.7 liberty_parser.py <real>.lib --stats` completes clean
- [ ] inventory shows LVF / CCS / pg_pin / compact-LUT constructs as ordinary nodes
- [ ] peak RSS + wall time recorded; retry with `--skip-groups ccs`
- [ ] fallback if real lib unreachable: ASAP7 `asap7sc7p5t_*.lib`

Local proxy already passing: the fixture exercises `ocv_sigma_*`, `output_current_*`
with nested `vector`, `pg_pin`, `voltage_map`, `define`, empty parens, semicolon-less
attributes, and an invented `quantum_flux_2031` group (check
`unknown_constructs_survive_verbatim`).

## Phase 3: Extraction
- [x] **Task 5** — header / cell / pin extraction
- [x] **Task 6** — timing + power LUT extraction

### GATE 3 — Extraction
- [x] tests exit 0
- [x] LUT grid/values misalignment raises a named error rather than reshaping
- [x] fixture arc's `cell_rise` grid verified against the raw .lib text
      (check `extraction_reads_luts_including_lvf`)
- [ ] cell count matches `--stats` on the real library

## Phase 4: Comparison and Reporting
- [x] **Task 7** — `sample_b.lib` + structural and scalar diff
- [x] **Task 8** — table diff + unit-mismatch guard
- [x] **Task 9** — JSON + self-contained HTML report

### GATE 4 — Complete
- [x] Definition of Done met (3.7 syntax, stdlib only, tests green, unknowns preserved,
      `ponytail:` comments)
- [x] self-comparison is empty; planted-delta comparison finds exactly 4 and nothing else
- [x] throughput measured on a 14 MB synthetic library: ~3.5 MB/s, ~4 s
- [ ] real corner pair compared on the server
- [ ] sign convention sanity-checked (SS slower than TT)

## Definition of Done (every task clears this)
- [x] Python 3.7 syntax only — no `:=`, no `dict[str, X]`, no `cached_property`, no `/` params
- [x] zero third-party imports
- [x] `python3.7 test_liberty.py` exits 0
- [x] unknown constructs never raise and are never dropped
- [x] every shortcut carries a `# ponytail:` comment naming ceiling + upgrade path
