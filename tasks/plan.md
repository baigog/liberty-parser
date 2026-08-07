# Implementation Plan: Liberty (.lib) Parser + Multi-Library Comparator

## Context

`D:\gitrepos\liberty-parser` is empty — greenfield. Goal: a Python 3.7 tool that (1) parses a
full, real Liberty file and exposes everything inside it, then (2) loads several libraries and
compares them, emitting JSON + a self-contained HTML report.

The constraint that drives the design: the user runs advanced-node libraries (LVF/OCV, CCS,
CCS-noise, PG pins, compact LUTs) and does not trust third-party parsers to cover the newest
constructs.

**Key insight: the Liberty *grammar* is tiny and stable; only the *vocabulary* grows.** Every
construct — `ocv_sigma_cell_rise`, `va_compact_ccs_rise`, `output_current_rise` with nested
`vector` groups, `voltage_map`, `pg_pin`, `dc_current`, `poly_template` — is one of exactly
four syntactic forms:

```
name : value ;              simple attribute
name ( a, b, c ) ;          complex attribute   (also define(...), values(...), index_1(...))
name ( args ) { ... }       group
/* ... */                   comment
```

A parser that parses the **grammar** into a generic tree, with no schema and no keyword
whitelist, is immune to new constructs by construction.

Note: this plan file is the deliverable of plan mode. At implementation start, Task 0 copies
this content to `tasks/plan.md` and derives `tasks/todo.md`, the paths the `/build` command
expects.

---

## Architecture Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Pure stdlib, no pip dependency** | Not dependency-phobia: a schema-free tree is what actually answers the "latest constructs" question, and it is ~200 lines. Also survives locked-down EDA/server environments where pip is not available. |
| 2 | **Generic tree first, typed accessors on top** | Typed cell/pin/timing views are a convenience layer, never a gate. Anything the accessors do not know about is still reachable in the tree. |
| 3 | **No float coercion at parse time** | Values stay raw `str`. Parsing stays fast and lossless (`1.0000e-03` round-trips); coercion happens in extraction, where units are known. |
| 4 | **Regex `finditer` tokenizer over whole file** | Char-by-char is ~50x slower and that matters at 100 MB+. |
| 5 | **`skip_groups` knob** | CCS `output_current_*` vector blocks are the overwhelming bulk of an advanced-node .lib. Skipping their bodies (brace-counted) is the difference between 2 GB RAM and 200 MB. |
| 6 | **Diff reports grid mismatch, never guesses** | Comparing LUTs on different index grids by interpolation invents numbers. Flag it instead. |
| 7 | **Units compared as strings, not normalized** | A `1ns` vs `1ps` mismatch is a finding that invalidates every numeric delta below it. Silent normalization would hide it. |

### Dependency graph

```
tokenizer  ──►  Group model  ──►  parser  ──►  accessors + CLI
                                                   │
                                                   ├──►  extraction (header/cell/pin)
                                                   │            │
                                                   │            └──►  extraction (timing/power tables)
                                                   │                        │
                                                   └────────────────────────┴──►  diff  ──►  JSON  ──►  HTML
```

### Files (four, repo root)

| File | Purpose |
|---|---|
| `liberty_parser.py` | Tokenizer + recursive-descent parser -> generic `Group` tree. CLI. |
| `liberty_compare.py` | Extraction, N-way diff, JSON + HTML report. CLI. |
| `sample.lib`, `sample_b.lib` | Synthetic fixtures exercising the nasty syntax. |
| `test_liberty.py` | `assert`-based checks, run as `python3.7 test_liberty.py`. No framework. |

---

## Definition of Done (standing bar — every task clears this)

- [ ] **Python 3.7 syntax only** — no walrus `:=`, no `dict[str, X]` / `list[X]` generics (use
      `typing.Dict`/`List`), no `functools.cached_property`, no positional-only `/` params.
- [ ] **Zero third-party imports** — `stdlib` only. Grep the source for `import` and confirm.
- [ ] `python3.7 test_liberty.py` exits 0.
- [ ] **Unknown constructs never raise** — an unrecognized group or attribute name is kept in
      the tree, never dropped, never an error.
- [ ] Every deliberate shortcut carries a `# ponytail:` comment naming the ceiling and the
      upgrade path.

---

## Task List

### Phase 1: Parser Foundation

#### Task 1: Fixture + tokenizer

**Description:** Write the adversarial fixture first (it is the spec for the tokenizer), then a
single master-regex tokenizer that turns Liberty text into a token stream.

`sample.lib` deliberately packs the constructs that break naive parsers:
- multi-line `/* */` comment containing `{` and `;`
- quoted string containing `,` `:` `;` `{`
- `values ( \` line continuations across several lines
- group with empty parens `()`, group with multiple args
- two `timing()` groups under one pin; two `when`-qualified `leakage_power` groups
- a `define(...)` statement
- an LVF `ocv_sigma_cell_rise` table and a `pg_pin` group
- a nested `output_current_rise { vector { ... } }` CCS block
- a `}` with no trailing `;`, and a simple attribute with no trailing `;`

Token kinds, alternation order significant (comments and strings must win):

```
COMMENT     /\*.*?\*/  (DOTALL)   and   //...$      <- // is non-standard but common in the wild
STRING      "([^"\\]|\\.)*"                          <- may contain { } , ; : and newlines
CONT        \\\r?\n                                  <- line continuation, deleted
PUNCT       [:;(),{}]
WORD        [^\s:;(),{}"]+                           <- covers 1.2e-3, A&B|!C, my_cell[0]
```

**Acceptance criteria:**
- [ ] `sample.lib` exists and contains every construct listed above.
- [ ] Tokenizer yields `(kind, value, line, col)`; comments and continuations are dropped.
- [ ] A quoted string containing `{ } , ; :` emerges as one single STRING token, intact.

**Verification:**
- [ ] `python3.7 test_liberty.py` — tokenizer tests pass.
- [ ] Manual: token count on `sample.lib` is stable across runs; no `UnicodeDecodeError` when
      the file is opened as `latin-1` fallback.

**Dependencies:** None
**Files:** `sample.lib`, `liberty_parser.py`, `test_liberty.py`
**Scope:** S (2-3 files)

---

#### Task 2: `Group` model + recursive-descent parser

**Description:** The `Group` node and the parser that builds the tree from the token stream.

```python
class Group:
    __slots__ = ('type', 'args', 'attrs', 'groups', 'parent')
    # type   : str                          'library' | 'cell' | 'pin' | 'timing' | 'vector' ...
    # args   : List[str]                    names inside the parens, quotes stripped
    # attrs  : List[Tuple[str, object]]     ORDER-PRESERVING, DUPLICATES KEPT
    # groups : List[Group]                  children, order preserved
```

Parse rules — after an IDENT token:
- `:` -> simple attribute; consume to `;` **or** end of line (`;` is optional in the wild);
  join tokens with spaces so `function : A & B ;` survives.
- `(` -> read comma-separated args, then peek: `{` = group, anything else = complex attribute.
- `}` may or may not be followed by `;` — accept both.
- Repeated groups of the same type (many `timing()` per pin) are appended, never overwritten.
- `define(...)` / `define_group(...)` need **no special case** — they are complex attributes.

**Acceptance criteria:**
- [ ] `parse_file('sample.lib')` returns the root `library` group with correct nesting depth.
- [ ] Both `timing()` groups under the same pin are present (no clobbering).
- [ ] `values( \` continuations rejoin into one complex-attribute value list.
- [ ] Attribute order is preserved and duplicate keys are all retained.

**Verification:**
- [ ] `python3.7 test_liberty.py` — parser tests pass.
- [ ] Manual: round-trip check — every non-comment token in `sample.lib` is accounted for in
      the tree (no silent drops).

**Dependencies:** Task 1
**Files:** `liberty_parser.py`, `test_liberty.py`
**Scope:** M

---

#### Task 3: Error reporting, `skip_groups`, gzip

**Description:** The three things that bite on real files.

```python
parse_file(path, skip_groups=frozenset(), encoding='utf-8')
```

- `LibertySyntaxError` carries **file, line, column, and group path**
  (`library/cell:INVx1/pin:Y/timing`). Advanced-node files are huge; "unexpected token" with no
  location is useless.
- `skip_groups` — group types whose *bodies* are token-skipped by brace counting instead of
  built. Default empty. Passing `{'output_current_rise','output_current_fall',
  'ccsn_first_stage','ccsn_last_stage'}` collapses a 400 MB CCS library's memory footprint.
- `.lib.gz` opened transparently via `gzip` — vendors ship compressed libs constantly.
- `# ponytail: whole file read into memory then regex-tokenized. Fine to ~1 GB on a
  workstation. If it ever isn't, switch to chunked scanning with a carry buffer for split
  tokens.`

**Acceptance criteria:**
- [ ] Malformed snippet raises `LibertySyntaxError` naming the correct line number and path.
- [ ] `skip_groups={'output_current_rise'}` removes that subtree and leaves everything else
      byte-identical to a normal parse.
- [ ] A gzipped copy of `sample.lib` parses to an identical tree.

**Verification:**
- [ ] `python3.7 test_liberty.py` — error/skip/gzip tests pass.

**Dependencies:** Task 2
**Files:** `liberty_parser.py`, `test_liberty.py`
**Scope:** S

---

### 🚦 GATE 1 — Parser core

Blocking. Do not start Phase 2 until all pass.

- [ ] `python3.7 test_liberty.py` exits 0
- [ ] `python3.7 -m compileall liberty_parser.py` on the **3.7 server**, not local 3.11
      (3.11 silently accepts 3.8+ syntax — this gate is meaningless if run locally)
- [ ] `grep -E '^\s*(import|from)' liberty_parser.py` shows stdlib only
- [ ] Malformed input produces a located error, never a bare traceback

---

### Phase 2: Parser Usability

#### Task 4: Accessors + CLI

**Description:** Make the tree usable and prove coverage on a real library.

Accessors (all cheap, no indexes built unless asked):
- `g.get(name, default=None)` -> first attribute value
- `g.get_all(name)` -> all values (needed for repeated `when`, `related_pin`)
- `g.find(type)` -> direct child groups of that type; `g.find_all(type)` -> recursive
- `g.to_dict()` -> plain nested dict/list, JSON-dumpable
- `g.path` -> `library/cell:INVx1/pin:Y/timing`

CLI:
```bash
python3.7 liberty_parser.py my.lib --json out.json        # whole tree as JSON
python3.7 liberty_parser.py my.lib --stats                # counts per group type + attr name
python3.7 liberty_parser.py my.lib --show cell:INVx1      # one subtree pretty-printed
```

`--stats` is the coverage proof: it prints every distinct group type and attribute name found,
so a real advanced-node lib immediately shows whether anything was missed.

**Acceptance criteria:**
- [ ] `get_all('when')` returns both values on the fixture's dual-`when` cell.
- [ ] `--json` output reloads via `json.load` and matches the tree.
- [ ] `--stats` lists every group type and attribute name in the fixture.

**Verification:**
- [ ] `python3.7 test_liberty.py` passes.
- [ ] `python3.7 liberty_parser.py sample.lib --stats` — eyeball the inventory.

**Dependencies:** Task 3
**Files:** `liberty_parser.py`, `test_liberty.py`
**Scope:** S

---

### 🚦 GATE 2 — Real advanced-node library ⚠️ HIGHEST-RISK GATE

Blocking, and **this is the gate that answers the original question.** Run on the user's server,
against the real library, not a fixture.

- [ ] `python3.7 liberty_parser.py <real_advanced_node>.lib --stats` completes without error
- [ ] The printed group-type / attribute inventory contains **no `UNKNOWN` and no truncation** —
      LVF, CCS, PG-pin and compact-LUT constructs all appear as ordinary tree nodes
- [ ] Peak RSS and wall time recorded; if RSS is uncomfortable, re-run with
      `--skip-groups output_current_rise,output_current_fall` and record the delta
- [ ] Public fallback if the real lib is unreachable: **ASAP7**
      (`github.com/The-OpenROAD-Project-Attic/asap7`) `asap7sc7p5t_*.lib` — real timing/power
      tables, good for a size/perf smoke test

**If this gate fails, stop and fix the parser before writing any comparison code.**

---

### Phase 3: Extraction

#### Task 5: Header, cell, and pin extraction

**Description:** Flatten the tree into comparable dicts. Read everything through the generic
accessors so an unknown attribute never breaks extraction.

- **Header**: library name, all `*_unit` attributes, `nom_voltage/temperature/process`,
  `default_*`, slew/delay threshold percentages, `operating_conditions` groups, `voltage_map`.
- **Per cell**: `area`, `cell_leakage_power`, `leakage_power` groups keyed by `when`,
  `dont_use` / `dont_touch`, pin list.
- **Per pin**: `direction`, `capacitance`, `rise/fall_capacitance`, `function`,
  `max_capacitance`, `max_transition`, `clock`, plus `pg_pin` groups.

**Acceptance criteria:**
- [ ] Every fixture cell appears with area and leakage as floats.
- [ ] A cell with two `when`-qualified `leakage_power` groups yields both, keyed by condition.
- [ ] A missing optional attribute yields `None`, never a `KeyError`.

**Verification:**
- [ ] `python3.7 test_liberty.py` — extraction tests pass.

**Dependencies:** Task 4
**Files:** `liberty_compare.py`, `test_liberty.py`
**Scope:** M

---

#### Task 6: Timing and power table extraction

**Description:** Extract LUTs, keyed so arcs from two libraries line up.

Arc key: `(cell, pin, related_pin, timing_type, timing_sense, when)`.
Value: child tables (`cell_rise`, `cell_fall`, `rise_transition`, `fall_transition`,
`rise_constraint`, ...) as `{index_1, index_2, values}` float grids. LVF siblings
(`ocv_sigma_*`) come along automatically — they are just more tables under the same `timing`
group. Same treatment for `internal_power` tables.

**Acceptance criteria:**
- [ ] `values("0.1, 0.2", "0.3, 0.4")` becomes a 2x2 float grid matching `index_1` x `index_2`.
- [ ] The fixture's `ocv_sigma_cell_rise` is extracted with no code that names it specifically.
- [ ] A table whose `values` row count disagrees with `index_1` length raises a clear error
      naming the arc — silent misalignment here would corrupt every downstream delta.

**Verification:**
- [ ] `python3.7 test_liberty.py` — table tests pass.

**Dependencies:** Task 5
**Files:** `liberty_compare.py`, `test_liberty.py`
**Scope:** M

---

### 🚦 GATE 3 — Extraction

- [ ] `python3.7 test_liberty.py` exits 0
- [ ] Extraction on the real library produces a cell count matching `--stats`
- [ ] Spot-check: one hand-verified arc's `cell_rise` grid matches the raw text in the .lib

---

### Phase 4: Comparison and Reporting

#### Task 7: Mutated fixture + structural and scalar diff

**Description:** `sample_b.lib` is `sample.lib` with four **known planted** mutations: one cell
dropped, one area +10%, one timing value changed, one pin capacitance changed.

First library on the command line is the reference; each other library is compared against it.
- **Structural**: cells / pins / timing arcs present in one and not the other. Highest-value
  output and pure set math.
- **Scalar**: area, leakage, pin capacitance -> absolute + percent delta, with `--tol`
  (default 1e-9 relative) below which values count as equal.

**Acceptance criteria:**
- [ ] Comparing the two fixtures finds **exactly the four planted deltas and nothing else** —
      the "nothing else" half is what catches float-noise and ordering bugs.
- [ ] Comparing a library against itself yields an empty diff.
- [ ] N-way (3+ libraries) produces one diff block per non-reference library.

**Verification:**
- [ ] `python3.7 test_liberty.py` — diff tests pass.

**Dependencies:** Task 6
**Files:** `liberty_compare.py`, `sample_b.lib`, `test_liberty.py`
**Scope:** M

---

#### Task 8: Table diff + unit-mismatch guard

**Description:**
- If `index_1`/`index_2` match elementwise: per-point percent delta, report min / max / mean
  plus the index coordinates of the worst point.
- If grids differ: emit `grid_mismatch` with both grids. No interpolation.
  `# ponytail: exact-grid compare only. Add bilinear interpolation onto the reference grid if
  cross-vendor libs with different templates become a real need.`
- If two libraries disagree on `time_unit` / `capacitive_load_unit` / etc., emit a loud
  top-level warning — every numeric delta below it is meaningless.

**Acceptance criteria:**
- [ ] The planted timing-value mutation is reported with the correct worst-point coordinates.
- [ ] Mismatched grids yield `grid_mismatch`, never a numeric delta.
- [ ] A unit mismatch surfaces at the top of both JSON and HTML output.

**Verification:**
- [ ] `python3.7 test_liberty.py` — table-diff tests pass.

**Dependencies:** Task 7
**Files:** `liberty_compare.py`, `test_liberty.py`
**Scope:** S

---

#### Task 9: JSON + self-contained HTML report

**Description:** One `--json` dump and one `--html` file with inline CSS, **no external assets,
no JS libraries**.

HTML sections:
1. Header banner: libraries compared, unit-mismatch warnings in red.
2. Library attribute table, one column per library, differing rows highlighted.
3. Cell set summary: counts, only-in-A / only-in-B lists.
4. Scalar delta table (area, leakage), sorted by |% delta| descending — worst offenders first.
5. Per-cell `<details>` blocks with pin and timing deltas, **collapsed by default** so a
   5000-cell library still opens.

Built with `html.escape` + string join. `# ponytail: string templating, no Jinja — one
template, one consumer.`

CLI:
```bash
python3.7 liberty_compare.py ref.lib new.lib --html report.html --json diff.json
python3.7 liberty_compare.py ss.lib tt.lib ff.lib --html corners.html      # N-way vs ref
python3.7 liberty_compare.py a.lib b.lib --cells "INV*,BUF*" --no-timing   # scope it down
```

**Acceptance criteria:**
- [ ] `--json` output reloads via `json.load`.
- [ ] HTML contains no `http://` / `https://` asset reference (self-contained guarantee) and
      opens correctly from `file://`.
- [ ] `--cells` glob and `--no-timing` measurably shrink the output.

**Verification:**
- [ ] `python3.7 test_liberty.py` — output tests pass.
- [ ] Manual: open `report.html` in a browser, confirm the four planted deltas are visible and
      per-cell sections start collapsed.

**Dependencies:** Task 8
**Files:** `liberty_compare.py`, `test_liberty.py`
**Scope:** M

---

### 🚦 GATE 4 — Complete

- [ ] Full Definition of Done met (3.7 syntax, stdlib only, tests green, unknowns preserved,
      `ponytail:` comments present)
- [ ] `python3.7 liberty_compare.py <real_ss>.lib <real_tt>.lib --html corners.html` runs on the
      server against real libraries
- [ ] The report's findings are sanity-checked against a known expectation (e.g. SS corner is
      slower than TT — if the report says otherwise, the sign convention is wrong)
- [ ] Runtime and peak memory on the real pair recorded in the README section of the plan

---

## Verification (end-to-end, local)

```bash
cd D:\gitrepos\liberty-parser
python3.7 test_liberty.py                                    # must exit 0
python3.7 liberty_parser.py sample.lib --stats                # group/attr inventory
python3.7 liberty_compare.py sample.lib sample_b.lib --html report.html --json diff.json
```

Then on the server, against the real library — Gates 2 and 4 above.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Real advanced-node lib uses a construct the grammar misses | **High** | Gate 2 runs `--stats` on the real file before any comparison code is written. Fail fast, fix the grammar. |
| Memory blowup on a multi-hundred-MB CCS library | High | `skip_groups` knob (Task 3), measured at Gate 2. |
| Local Python is 3.11 — 3.8+ syntax slips in unnoticed | Medium | Gate 1 requires `compileall` **on the 3.7 server**, plus an explicit banned-construct list in the Definition of Done. |
| Two libraries use different LUT index grids | Medium | Reported as `grid_mismatch`, never interpolated (Task 8). |
| Unit mismatch makes every delta meaningless | Medium | Top-level red warning in both outputs (Task 8). |
| 5000-cell HTML report is unopenable | Low | `<details>` collapsed by default, `--cells` / `--no-timing` scoping (Task 9). |

## Open Questions

- Real library path on the server — needed only at Gates 2 and 4; does not block Phase 1-3.
- Is the comparison usually **same library across PVT corners** (grids match, numeric deltas
  are the point) or **different library versions/vendors** (structural deltas are the point)?
  Both work today; the answer only decides which HTML section goes first.

## Deliberately Skipped

- No pip dependency — the schema-free tree is what answers the advanced-node concern.
- No float coercion at parse time, no NumPy — deltas are simple loops over small LUTs.
- No interpolation across mismatched grids — flagged, not guessed.
- No pytest / tox / CI scaffolding — one runnable `test_liberty.py`.
