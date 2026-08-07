# liberty-parser

Parse Synopsys Liberty (`.lib`) files and compare libraries against each other.
Python 3.7, standard library only, no pip install.

## Why there is no dependency

The Liberty *grammar* is tiny and stable; only the *vocabulary* grows. Every construct in a
modern advanced-node library — `ocv_sigma_*` (LVF/OCV), `output_current_*` (CCS), `ccsn_*`,
`pg_pin`, `voltage_map`, compact LUTs, whatever the next release invents — is one of exactly
three forms:

```
name : value ;              simple attribute
name ( a, b, c ) ;          complex attribute
name ( args ) { ... }       group
```

So the parser knows the grammar and nothing about the vocabulary: no schema, no keyword list,
no whitelist. An unrecognised group or attribute is kept in the tree verbatim rather than
dropped or rejected. That is what makes it safe against constructs newer than the parser.

## Files

| File | Purpose |
|---|---|
| `liberty_parser.py` | Tokenizer + parser → generic `Group` tree, plus CLI |
| `liberty_compare.py` | Extraction, N-way diff, JSON + HTML report, plus CLI |
| `sample.lib` | Adversarial fixture (comments with braces, quoted delimiters, line continuations, empty parens, duplicate groups, LVF, CCS, `pg_pin`, missing semicolons) |
| `sample_b.lib` | Generated from `sample.lib` by `test_liberty.py` with four known mutations |
| `test_liberty.py` | 34 `assert`-based checks, no framework |

## Use

```bash
python3.7 test_liberty.py                                  # must exit 0

# parse
python3.7 liberty_parser.py my.lib --stats                 # inventory every construct found
python3.7 liberty_parser.py my.lib --show cell:INVx1       # pretty-print one subtree
python3.7 liberty_parser.py my.lib --json tree.json        # whole tree as JSON
python3.7 liberty_parser.py huge.lib --stats --skip-groups ccs   # drop CCS current vectors

# compare libraries (first file is the reference)
python3.7 liberty_compare.py ref.lib new.lib --html report.html --json diff.json
python3.7 liberty_compare.py ss.lib tt.lib ff.lib --html corners.html
python3.7 liberty_compare.py a.lib b.lib --cells "INV*,BUF*" --no-timing

# compare cells against each other INSIDE one library
python3.7 liberty_compare.py my.lib --within "BUF*" --html buffers.html
python3.7 liberty_compare.py my.lib --within "BUF*,CLKBUF*" --slew 0.02 --load 0.01
```

## Comparing cell variants within one library

`--within GLOBS` answers "what actually differs between these cells, and should
I avoid any of them" when the vendor documents nothing.

```
cell             function      area  leakage       Cin        FO4    driveR  flags
BUFx4_LVT        A             0.42       81     0.002     0.0185         1
BUFx1            A             0.24        9     0.001      0.035         4
BUFx2            A             0.42       19     0.002      0.037         2
BUFx2_ECO        A             0.42       19     0.002      0.037         2  dont_use
BUFx1_OLD        A             0.26       11     0.001      0.043         5
BUFx1_HVT        A             0.24        3     0.001      0.053         6
** BUFx2_ECO: marked dont_use by the vendor
** BUFx1_OLD: dominated by BUFx1: same function, and no worse on area,
   leakage, input capacitance, drive strength or maximum load
```

Two variants of the same buffer have **different load grids**, scaled to their
drive strength, so there is no shared LUT point to compare. Every cell is
therefore evaluated at one common operating point, interpolated onto its own
grid — the opposite of the cross-library rule, where interpolating would paper
over a real mismatch. Here interpolation is the measurement.

The delay LUT is fitted to `delay = intrinsic + drive_resistance × load` along
the load axis. Those two numbers are what actually separate a cell family:

- **intrinsic** — unloaded delay
- **drive R** — falls as drive strength rises; the real meaning of the `x1`/`x2`/`x4` suffix
- **Cin** — the load this cell presents to whatever drives it
- **FO4** — delay driving 4 copies of itself, which normalises drive strength away

Reference slew and load default to the median of the cells' own grids; override
with `--slew` / `--load`, and change the fanout with `--fanout`. Values that fall
outside a cell's characterised grid are clamped to the edge and flagged
`clamped` — still fine for ranking, not to be quoted as characterised data.

**On the `dominated by` flag:** a cell is only called redundant if another cell
with the same function is no worse on area, leakage, input capacitance, drive
resistance *and* maximum load. Deliberately not judged on FO-N delay — FO-N
normalises drive away, so a small cell always looks faster there, and ranking on
it would declare every high-drive buffer redundant. Above, `BUFx2`, `BUFx4_LVT`
and `BUFx1_HVT` are all genuine trade-offs and are correctly left alone; only
`BUFx1_OLD`, which is worse on every axis, is flagged.

```python
from liberty_parser import parse_file, CCS_GROUPS

lib = parse_file('my.lib', skip_groups=CCS_GROUPS)   # .lib.gz works too
for cell in lib.find('cell'):
    print(cell.name, cell.get_float('area'))
    for arc in cell.find_all('timing'):
        print(' ', arc.get('related_pin'), arc.get('timing_type'))
```

`get` / `get_all` / `get_float` / `find` / `find_all` / `first` / `by_name` / `to_dict` /
`dumps` / `path`. Attributes keep order and duplicates (repeated `when`, `related_pin`).
Values stay raw strings — nothing is coerced to float until units are known.

## What it will and will not do

- Compares header attributes, cell/pin sets, timing-arc sets, scalars (area, leakage,
  capacitance), and LUTs elementwise.
- A unit or threshold mismatch between libraries is reported **loudly at the top** — every
  numeric delta below it is meaningless until resolved.
- LUTs on different index grids are reported as `grid_mismatch`. It does **not** interpolate;
  inventing numbers to make a comparison work is worse than not comparing.
- CCS current vectors are skipped by default in `liberty_compare` (they are the bulk of an
  advanced-node file and are never compared numerically here). `liberty_parser` keeps them
  unless you pass `--skip-groups`.

## Performance

Measured on CPython 3.11; 3.7 is noticeably slower.

| | throughput |
|---|---|
| parse into tree | ~5 MB/s |
| parse with `--skip-groups ccs` on a CCS-heavy library | ~15 MB/s |
| raw body skip (scanning only) | ~48 MB/s |

Lexing is ~79% of parse time and already runs as a C-driven `finditer`, so
there is little left to win in Python. The tree costs about 5x the input in RAM.

**`--skip-groups ccs` is worth more than any other tuning.** CCS current vectors
are typically most of an advanced-node library, and skipped bodies are jumped in
the raw source rather than tokenized — measured at 4x end-to-end on a file that
is 72% CCS. Use it whenever you do not need the current vectors.

## Verifying on a real library

The parser has been checked against the fixture; the real proof is your own library.

```bash
# 1. coverage: does the grammar handle the file at all?
python3.7 -m compileall liberty_parser.py liberty_compare.py   # confirm 3.7 accepts it
python3.7 liberty_parser.py <real>.lib --stats

# 2. if memory is tight
python3.7 liberty_parser.py <real>.lib --stats --skip-groups ccs

# 3. real comparison, then sanity-check the sign convention
python3.7 liberty_compare.py <ss>.lib <tt>.lib --html corners.html --json corners.json
```

`--stats` prints every distinct group type and attribute name in the file. Nothing is
filtered, so if it completes without error, every construct in the file is in that list.
If a construct is missing or the run errors, that is a grammar bug worth reporting.

No real library at hand? [ASAP7](https://github.com/The-OpenROAD-Project-Attic/asap7) ships
`asap7sc7p5t_*.lib` with real timing and power tables.
