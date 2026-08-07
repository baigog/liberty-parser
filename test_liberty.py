#!/usr/bin/env python3.7
# -*- coding: utf-8 -*-
"""Self-checks for the Liberty parser and comparator.

Plain asserts, no framework:  python3.7 test_liberty.py
"""

from __future__ import print_function

import gzip
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from liberty_parser import (CCS_GROUPS, Group, LibertySyntaxError, locate,
                            parse_file, parse_string, tokenize)

SAMPLE = os.path.join(HERE, 'sample.lib')
SAMPLE_B = os.path.join(HERE, 'sample_b.lib')

_RESULTS = []


def check(fn):
    """Register a check. Named `check`, not `test`, so no runner picks it up."""
    _RESULTS.append(fn)
    return fn


# ==========================================================================
# Task 1 -- tokenizer
# ==========================================================================

@check
def tokenizer_drops_comments_and_continuations():
    text = '/* a { ; " comment */\nfoo : bar; \\\n baz : qux;\n'
    kinds = [t[0] for t in tokenize(text)]
    assert 'comment' not in kinds, kinds
    assert 'cont' not in kinds, kinds
    values = [t[1] for t in tokenize(text) if t[0] == 'word']
    assert values == ['foo', 'bar', 'baz', 'qux'], values


@check
def tokenizer_keeps_delimiters_inside_strings():
    text = 'note : "has , and : and ; and { } inside";'
    strings = [t[1] for t in tokenize(text) if t[0] == 'string']
    assert strings == ['has , and : and ; and { } inside'], strings


@check
def offsets_resolve_to_line_and_column():
    text = 'a : 1;\n  b : 2;\nc : 3;\n'
    lines = {t[1]: locate(text, t[2])
             for t in tokenize(text) if t[0] == 'word' and t[1] in 'abc'}
    assert lines == {'a': (1, 1), 'b': (2, 3), 'c': (3, 1)}, lines


@check
def tokenizer_rejects_unterminated_string():
    try:
        list(tokenize('a : "never closed\n'))
    except LibertySyntaxError as exc:
        assert 'unterminated' in str(exc), str(exc)
    else:
        raise AssertionError('expected LibertySyntaxError')


# ==========================================================================
# Task 2 -- parser
# ==========================================================================

@check
def parses_sample_into_library_root():
    lib = parse_file(SAMPLE)
    assert lib.type == 'library', lib.type
    assert lib.name == 'sample_lib_ss_0p75v_125c', lib.name
    cells = [c.name for c in lib.find('cell')]
    assert cells == ['INVx1_ASAP7', 'DFFx1_ASAP7'], cells


@check
def keeps_duplicate_groups_and_attributes():
    lib = parse_file(SAMPLE)
    inv = lib.by_name('cell', 'INVx1_ASAP7')
    leaks = list(inv.find('leakage_power'))
    assert len(leaks) == 2, len(leaks)
    whens = [g.get('when') for g in leaks]
    assert whens == ['A', '!A'], whens

    dff = lib.by_name('cell', 'DFFx1_ASAP7')
    d_pin = dff.by_name('pin', 'D')
    arcs = list(d_pin.find('timing'))
    assert len(arcs) == 2, len(arcs)
    types = [a.get('timing_type') for a in arcs]
    assert types == ['setup_rising', 'hold_rising'], types


@check
def rejoins_line_continuations_in_values():
    lib = parse_file(SAMPLE)
    inv = lib.by_name('cell', 'INVx1_ASAP7')
    arc = next(inv.by_name('pin', 'Y').find('timing'))
    values = arc.first('cell_rise').get('values')
    assert isinstance(values, list), type(values)
    assert len(values) == 3, values
    assert values[0] == '0.0121, 0.0245, 0.0611', values[0]
    assert values[2] == '0.0402, 0.0530, 0.0901', values[2]


@check
def attribute_without_semicolon_terminates_at_newline():
    lib = parse_file(SAMPLE)
    assert lib.get('default_cell_leakage_power') == '0.0', \
        lib.get('default_cell_leakage_power')
    # the attribute *after* the semicolon-less one must still be intact
    assert lib.get('default_max_transition') == '1.5'


@check
def unknown_constructs_survive_verbatim():
    """The core promise: a construct the parser has never heard of is kept."""
    lib = parse_file(SAMPLE)
    inv = lib.by_name('cell', 'INVx1_ASAP7')
    assert inv.get('custom_vendor_attr') == \
        'unknown to any schema, must survive; keep {me}', inv.get('custom_vendor_attr')
    assert lib.get('define') == ['custom_vendor_attr', 'cell', 'string']
    assert inv.first('pg_pin').get('pg_type') == 'primary_power'
    arc = next(inv.by_name('pin', 'Y').find('timing'))
    assert arc.first('ocv_sigma_cell_rise') is not None
    # something invented after this parser was written
    made_up = parse_string('library(x){ cell(c){ quantum_flux_2031(a,b){ z : 1; } } }')
    node = next(made_up.find_all('quantum_flux_2031'))
    assert node.args == ['a', 'b'] and node.get('z') == '1'


@check
def empty_parens_and_multi_arg_groups():
    lib = parse_file(SAMPLE)
    dff = lib.by_name('cell', 'DFFx1_ASAP7')
    ff = dff.first('ff')
    assert ff.args == ['IQ', 'IQN'], ff.args
    vendor = dff.first('vendor_block')
    assert vendor.args == [], vendor.args
    assert '{ }' in vendor.get('note'), vendor.get('note')


@check
def group_path_is_reported():
    lib = parse_file(SAMPLE)
    arc = next(lib.by_name('cell', 'INVx1_ASAP7').by_name('pin', 'Y').find('timing'))
    assert arc.path == \
        'library:sample_lib_ss_0p75v_125c/cell:INVx1_ASAP7/pin:Y/timing', arc.path


# ==========================================================================
# Task 3 -- errors, skip_groups, gzip
# ==========================================================================

@check
def syntax_error_carries_location_and_path():
    bad = 'library (l) {\n  cell (c) {\n    area 0.5;\n  }\n}\n'
    try:
        parse_string(bad, 'bad.lib')
    except LibertySyntaxError as exc:
        assert exc.line == 3, exc.line
        assert 'cell:c' in exc.path, exc.path
        assert 'bad.lib' in str(exc), str(exc)
    else:
        raise AssertionError('expected LibertySyntaxError')


@check
def unclosed_group_is_an_error_not_a_silent_truncation():
    try:
        parse_string('library (l) {\n  cell (c) {\n    area : 0.5;\n')
    except LibertySyntaxError as exc:
        assert 'end of file' in str(exc), str(exc)
    else:
        raise AssertionError('expected LibertySyntaxError')


@check
def skip_groups_drops_only_the_named_subtree():
    full = parse_file(SAMPLE)
    lean = parse_file(SAMPLE, skip_groups=CCS_GROUPS)

    assert list(full.find_all('output_current_rise')), 'fixture has no CCS data'
    assert not list(lean.find_all('output_current_rise'))
    assert not list(lean.find_all('vector'))

    # everything else must be byte-identical
    def strip_ccs(node):
        return {
            'type': node['type'],
            'args': node['args'],
            'attributes': node['attributes'],
            'groups': [strip_ccs(g) for g in node['groups']
                       if g['type'] not in CCS_GROUPS],
        }

    assert strip_ccs(full.to_dict()) == lean.to_dict()


@check
def gzipped_library_parses_identically():
    plain = parse_file(SAMPLE)
    tmp = tempfile.NamedTemporaryFile(suffix='.lib.gz', delete=False)
    tmp.close()
    try:
        with io.open(SAMPLE, 'rb') as src, gzip.open(tmp.name, 'wb') as dst:
            dst.write(src.read())
        assert parse_file(tmp.name).to_dict() == plain.to_dict()
    finally:
        os.unlink(tmp.name)


# ==========================================================================
# Task 4 -- accessors and CLI
# ==========================================================================

@check
def accessors_behave():
    lib = parse_file(SAMPLE)
    inv = lib.by_name('cell', 'INVx1_ASAP7')
    assert inv.get_float('area') == 0.532
    assert inv.get_float('nope', 7.0) == 7.0
    assert inv.get('nope') is None
    assert inv.get_float('custom_vendor_attr', -1) == -1  # not a number
    assert [g.get('when') for g in inv.find('leakage_power')] == ['A', '!A']
    assert len(list(lib.find_all('timing'))) == 4
    assert lib.by_name('cell', 'nope') is None
    assert 'area' in inv.attr_names() and inv.has('area')


@check
def to_dict_is_json_round_trippable():
    lib = parse_file(SAMPLE)
    assert json.loads(json.dumps(lib.to_dict())) == lib.to_dict()


@check
def dumps_reparses_to_the_same_tree():
    """Strongest no-silent-drop check available: re-emit, re-parse, compare."""
    lib = parse_file(SAMPLE)
    again = parse_string(lib.dumps(), '<dumps>')
    assert again.to_dict() == lib.to_dict()


@check
def cli_stats_lists_every_construct():
    import liberty_parser
    buf = io.StringIO()
    liberty_parser._print_stats(parse_file(SAMPLE), buf)
    out = buf.getvalue()
    for expected in ('output_current_rise', 'ocv_sigma_cell_rise', 'pg_pin',
                     'voltage_map', 'custom_vendor_attr', 'vendor_block'):
        assert expected in out, 'missing %r from --stats' % expected


# ==========================================================================
# Tasks 5-9 -- extraction, diff, report
# ==========================================================================

import liberty_compare as lc

# The four planted mutations. sample_b.lib is generated from sample.lib by tree
# surgery rather than hand-copied, so "exactly these deltas and nothing else"
# is guaranteed by construction instead of by careful editing.
PLANTED_AREA = (0.532, 0.5852)
PLANTED_CAP = (0.00121, 0.00131)
PLANTED_VALUE = (0.0611, 0.0650)
PLANTED_DROPPED_CELL = 'DFFx1_ASAP7'


def _set_attr(group, name, value):
    for i, (key, _) in enumerate(group.attrs):
        if key == name:
            group.attrs[i] = (key, value)
            return
    raise KeyError('%s has no attribute %r' % (group.path, name))


def _make_sample_b(path=SAMPLE_B):
    """Regenerate sample_b.lib from sample.lib with four known mutations."""
    lib = parse_file(SAMPLE)
    lib.groups = [g for g in lib.groups
                  if not (g.type == 'cell' and g.name == PLANTED_DROPPED_CELL)]

    inv = lib.by_name('cell', 'INVx1_ASAP7')
    _set_attr(inv, 'area', str(PLANTED_AREA[1]))
    _set_attr(inv.by_name('pin', 'A'), 'capacitance', str(PLANTED_CAP[1]))

    arc = next(inv.by_name('pin', 'Y').find('timing'))
    rows = list(arc.first('cell_rise').get('values'))
    rows[0] = rows[0].replace(str(PLANTED_VALUE[0]), str(PLANTED_VALUE[1]))
    _set_attr(arc.first('cell_rise'), 'values', rows)

    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write('/* generated by test_liberty.py from sample.lib */\n')
        fh.write(lib.dumps())
        fh.write('\n')
    return path


@check
def extraction_reads_header_cells_and_pins():
    data = lc.load(SAMPLE)
    assert data['header']['time_unit'] == '1ns'
    assert data['header']['capacitive_load_unit'] == '1, pf'
    assert data['voltage_map'] == {'VDD': '0.75', 'VSS': '0.0'}
    assert data['operating_conditions']['ss_0p75v_125c']['temperature'] == '125.0'

    inv = data['cells']['INVx1_ASAP7']
    assert float(inv['attributes']['area']) == PLANTED_AREA[0]
    assert inv['leakage_power'] == {'A': 15.1, '!A': 9.7}
    assert inv['pins']['A']['attributes']['capacitance'] == str(PLANTED_CAP[0])
    assert inv['pins']['Y']['attributes']['function'] == '!A'
    assert inv['pg_pin']['VDD']['pg_type'] == 'primary_power'
    # bus and its member pin both surface
    dff = data['cells']['DFFx1_ASAP7']
    assert 'DOUT' in dff['pins'] and 'DOUT[0]' in dff['pins']
    # a missing optional attribute is absent, not a KeyError
    assert dff['pins']['CLK']['attributes'].get('function') is None


@check
def extraction_reads_luts_including_lvf():
    data = lc.load(SAMPLE)
    inv = data['cells']['INVx1_ASAP7']
    arc = inv['arcs']['Y|A|combinational|negative_unate|']
    tables = arc['tables']
    # LVF tables ride along with no code naming them
    assert 'ocv_sigma_cell_rise' in tables and 'ocv_sigma_cell_fall' in tables
    rise = tables['cell_rise']
    assert rise['indexes'][0] == [0.01, 0.10, 1.00]
    assert rise['indexes'][1] == [0.50, 1.50, 4.00]
    assert len(rise['values']) == 3 and len(rise['values'][0]) == 3
    assert rise['values'][0][2] == PLANTED_VALUE[0]
    # constraint arcs on the flop are keyed apart by timing_type
    dff = data['cells']['DFFx1_ASAP7']
    assert 'D|CLK|setup_rising||' in dff['arcs']
    assert 'D|CLK|hold_rising||' in dff['arcs']


@check
def misaligned_lut_is_an_error_not_a_silent_reshape():
    bad = ('library(l){cell(c){pin(p){timing(){related_pin : "A";'
           'cell_rise(t){index_1("0.1, 0.2, 0.3");index_2("1.0, 2.0");'
           'values("0.1, 0.2");}}}}}')
    try:
        lc.extract(parse_string(bad))
    except lc.LibertyDataError as exc:
        assert '1 value rows for 3 index_1 points' in str(exc), str(exc)
    else:
        raise AssertionError('expected LibertyDataError')


@check
def self_comparison_is_empty():
    data = lc.load(SAMPLE)
    d = lc.diff(data, data)
    assert d['header'] == [] and d['scalars'] == [] and d['tables'] == []
    assert d['cells']['only_in_ref'] == [] and d['cells']['only_in_other'] == []
    assert d['pins'] == {} and d['arcs'] == {}


@check
def diff_finds_exactly_the_planted_deltas():
    _make_sample_b()
    report = lc.compare_files([SAMPLE, SAMPLE_B])
    d = report['comparisons'][0]

    assert d['critical_mismatch'] == [], d['critical_mismatch']
    assert d['cells']['only_in_ref'] == [PLANTED_DROPPED_CELL], d['cells']
    assert d['cells']['only_in_other'] == [], d['cells']
    assert d['pins'] == {} and d['arcs'] == {}

    # exactly two scalar deltas, and they are the planted ones
    assert len(d['scalars']) == 2, [(s['name'], s['metric']) for s in d['scalars']]
    by_metric = {s['metric']: s for s in d['scalars']}
    area = by_metric['area']
    assert (area['ref'], area['other']) == PLANTED_AREA
    assert abs(area['pct'] - 10.0) < 1e-9, area['pct']
    cap = by_metric['capacitance']
    assert (cap['ref'], cap['other']) == PLANTED_CAP
    assert cap['name'] == 'INVx1_ASAP7/A'

    # exactly one table delta, and it is the planted one
    assert len(d['tables']) == 1, [(t['cell'], t['table']) for t in d['tables']]
    t = d['tables'][0]
    assert (t['table'], t['status']) == ('cell_rise', 'changed')
    worst = t['worst']
    assert (worst['ref'], worst['other']) == PLANTED_VALUE
    assert (worst['index_1'], worst['index_2']) == (0.01, 4.00), worst


@check
def unit_mismatch_is_flagged_loudly():
    ref = lc.load(SAMPLE)
    other = json.loads(json.dumps(ref))
    other['header']['time_unit'] = '1ps'
    other['name'] = 'sample_lib_ps'
    d = lc.diff(ref, other)
    flagged = [r['attribute'] for r in d['critical_mismatch']]
    assert flagged == ['time_unit'], flagged
    assert 'UNIT/THRESHOLD MISMATCH' in _summary_text({'comparisons': [d]})


@check
def grid_mismatch_is_flagged_never_interpolated():
    ref = lc.load(SAMPLE)
    other = json.loads(json.dumps(ref))
    arc = other['cells']['INVx1_ASAP7']['arcs']['Y|A|combinational|negative_unate|']
    arc['tables']['cell_rise']['indexes'][1] = [0.5, 1.5, 8.0]  # different grid
    d = lc.diff(ref, other)
    hits = [t for t in d['tables'] if t['table'] == 'cell_rise']
    assert len(hits) == 1 and hits[0]['status'] == 'grid_mismatch', hits
    assert 'max_pct' not in hits[0], 'a mismatched grid must not produce a number'


@check
def tolerance_suppresses_float_noise():
    ref = lc.load(SAMPLE)
    other = json.loads(json.dumps(ref))
    inv = other['cells']['INVx1_ASAP7']['attributes']
    inv['area'] = repr(PLANTED_AREA[0] + 1e-12)
    assert lc.diff(ref, other, tol=1e-9)['scalars'] == []
    assert len(lc.diff(ref, other, tol=1e-15)['scalars']) == 1


@check
def n_way_compares_each_library_against_the_first():
    _make_sample_b()
    report = lc.compare_files([SAMPLE, SAMPLE_B, SAMPLE])
    assert len(report['comparisons']) == 2
    assert len(report['comparisons'][0]['scalars']) == 2
    assert report['comparisons'][1]['scalars'] == []


@check
def cell_filter_and_no_timing_shrink_the_work():
    full = lc.load(SAMPLE)
    filtered = lc.load(SAMPLE, cells=['INV*'])
    assert set(filtered['cells']) == {'INVx1_ASAP7'}
    assert set(full['cells']) == {'INVx1_ASAP7', 'DFFx1_ASAP7'}
    lean = lc.load(SAMPLE, with_timing=False)
    assert lean['cells']['INVx1_ASAP7']['arcs'] == {}
    assert lean['cells']['INVx1_ASAP7']['pins']  # pins survive


@check
def html_report_is_self_contained():
    _make_sample_b()
    report = lc.compare_files([SAMPLE, SAMPLE_B])
    page = lc.render_html(report)
    assert '<!DOCTYPE html>' in page
    for forbidden in ('http://', 'https://', '<script', 'src='):
        assert forbidden not in page, 'report is not self-contained: %r' % forbidden
    assert '<details>' in page and 'open>' not in page  # collapsed by default
    assert 'INVx1_ASAP7' in page and PLANTED_DROPPED_CELL in page


@check
def html_report_escapes_library_content():
    """Library text is data, not markup -- a vendor attribute cannot inject tags."""
    ref = lc.load(SAMPLE)
    other = json.loads(json.dumps(ref))
    other['header']['vendor_note'] = '<script>alert("x")</script> & <b>'
    page = lc.render_html({
        'reference': {'name': ref['name'], 'file': ref['file']},
        'libraries': [{'name': ref['name'], 'file': ref['file']},
                      {'name': other['name'], 'file': other['file']}],
        'comparisons': [lc.diff(ref, other)],
    })
    assert '&lt;script&gt;' in page, 'library content was not escaped'
    assert '<script' not in page
    assert '&amp;' in page


@check
def json_report_round_trips():
    _make_sample_b()
    report = lc.compare_files([SAMPLE, SAMPLE_B])
    assert json.loads(json.dumps(report, sort_keys=True))['comparisons']


def _summary_text(report):
    buf = io.StringIO()
    for cmp_ in report['comparisons']:
        cmp_.setdefault('ref', {'name': 'a', 'file': 'a'})
        cmp_.setdefault('other', {'name': 'b', 'file': 'b'})
    lc._summarise(report, buf)
    return buf.getvalue()


# ==========================================================================
# Definition of Done -- enforced, not just documented
# ==========================================================================

_SOURCES = ['liberty_parser.py', 'liberty_compare.py', 'test_liberty.py']

# Everything these modules are allowed to import. The point of the project is
# that it runs on a locked-down 3.7 box with no pip.
_STDLIB_OK = frozenset([
    'argparse', 'ast', 'fnmatch', 'gzip', 'html', 'io', 'json', 'math', 'os',
    're', 'sys', 'tempfile', 'typing', 'collections', 'datetime', '__future__',
    'liberty_parser', 'liberty_compare',
])


def _sources():
    for name in _SOURCES:
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            with io.open(path, encoding='utf-8') as fh:
                yield name, fh.read()


@check
def sources_are_python_37_compatible():
    """Compile under 3.7 grammar rules, so walrus etc. cannot slip in locally.

    The dev box runs 3.11, which accepts 3.8+ syntax silently; feature_version
    makes the 3.7 target checkable here instead of only on the server.
    """
    import ast
    for name, src in _sources():
        try:
            ast.parse(src, filename=name, feature_version=(3, 7))
        except SyntaxError as exc:
            raise AssertionError('%s is not 3.7-compatible: %s' % (name, exc))


@check
def sources_import_stdlib_only():
    import ast
    for name, src in _sources():
        for node in ast.walk(ast.parse(src, filename=name)):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or '']
            else:
                continue
            for mod in mods:
                top = mod.split('.')[0]
                assert top in _STDLIB_OK, '%s imports non-stdlib %r' % (name, mod)


# ==========================================================================
# runner
# ==========================================================================

def main():
    failures = 0
    for fn in _RESULTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - this is the reporter
            failures += 1
            print('FAIL  %s\n        %s: %s' % (fn.__name__, type(exc).__name__, exc))
        else:
            print('ok    %s' % fn.__name__)
    print('\n%d checks, %d failures' % (len(_RESULTS), failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
