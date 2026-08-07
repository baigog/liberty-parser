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

# compare (first file is the reference)
python3.7 liberty_compare.py ref.lib new.lib --html report.html --json diff.json
python3.7 liberty_compare.py ss.lib tt.lib ff.lib --html corners.html
python3.7 liberty_compare.py a.lib b.lib --cells "INV*,BUF*" --no-timing
```

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
