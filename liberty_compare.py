#!/usr/bin/env python3.7
# -*- coding: utf-8 -*-
"""Compare two or more Liberty libraries: structure, scalars and LUTs.

The first library on the command line is the reference; every other one is
compared against it.

    python3.7 liberty_compare.py ref.lib new.lib --html report.html --json diff.json
    python3.7 liberty_compare.py ss.lib tt.lib ff.lib --html corners.html
    python3.7 liberty_compare.py a.lib b.lib --cells "INV*,BUF*" --no-timing

Extraction is done through the generic tree accessors only, so an attribute this
module has never heard of cannot break it -- at worst it is not summarised, and
it is still reachable via liberty_parser.
"""

from __future__ import print_function

import fnmatch
import html
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from liberty_parser import CCS_GROUPS, Group, parse_file

__all__ = ['extract', 'load', 'diff', 'compare_files', 'render_html']

# Header attributes where a mismatch invalidates every number underneath it.
CRITICAL_HEADER = (
    'time_unit', 'voltage_unit', 'current_unit', 'leakage_power_unit',
    'capacitive_load_unit', 'pulling_resistance_unit', 'delay_model',
    'slew_lower_threshold_pct_rise', 'slew_upper_threshold_pct_rise',
    'slew_lower_threshold_pct_fall', 'slew_upper_threshold_pct_fall',
    'input_threshold_pct_rise', 'input_threshold_pct_fall',
    'output_threshold_pct_rise', 'output_threshold_pct_fall',
    'slew_derate_from_library', 'nom_process', 'nom_voltage', 'nom_temperature',
)

# Scalar cell/pin attributes worth a numeric delta.
CELL_SCALARS = ('area', 'cell_leakage_power')
PIN_SCALARS = ('capacitance', 'rise_capacitance', 'fall_capacitance',
               'max_capacitance', 'max_transition', 'max_fanout',
               'min_pulse_width_high', 'min_pulse_width_low')


class LibertyDataError(Exception):
    """Structurally inconsistent data (e.g. a LUT whose values miss its grid)."""


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def _numbers(value):
    # type: (Any) -> List[float]
    """Flatten a Liberty index/value attribute into floats.

    `index_1 ("0.01, 0.10, 1.00")` arrives as ['0.01, 0.10, 1.00'].
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []  # type: List[float]
    for item in items:
        for piece in str(item).replace('\\', ' ').split(','):
            piece = piece.strip()
            if piece:
                out.append(float(piece))
    return out


def _rows(value):
    # type: (Any) -> List[List[float]]
    """`values(...)` as a list of rows; each element of the attribute is a row."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    rows = []
    for item in items:
        row = [float(p.strip()) for p in str(item).split(',') if p.strip()]
        if row:
            rows.append(row)
    return rows


def _table(group, where):
    # type: (Group, str) -> Dict[str, Any]
    """A LUT group -> {indexes, values}. Raises if the grid and values disagree.

    Silent misalignment here would corrupt every delta downstream, so it is an
    error rather than a best-effort reshape.
    """
    indexes = []  # type: List[List[float]]
    for n in (1, 2, 3):
        raw = group.get('index_%d' % n)
        if raw is None:
            break
        indexes.append(_numbers(raw))
    values = _rows(group.get('values'))

    if len(indexes) >= 2 and values:
        if len(values) != len(indexes[0]):
            raise LibertyDataError(
                '%s/%s: %d value rows for %d index_1 points'
                % (where, group.type, len(values), len(indexes[0])))
        bad = [len(r) for r in values if len(r) != len(indexes[1])]
        if bad:
            raise LibertyDataError(
                '%s/%s: value row width %s does not match %d index_2 points'
                % (where, group.type, bad[0], len(indexes[1])))
    elif len(indexes) == 1 and values:
        flat = [v for row in values for v in row]
        if len(flat) != len(indexes[0]):
            raise LibertyDataError(
                '%s/%s: %d values for %d index_1 points'
                % (where, group.type, len(flat), len(indexes[0])))

    return {'indexes': indexes, 'values': values}


def _tables_of(group, where):
    # type: (Group, str) -> Dict[str, Dict[str, Any]]
    """Every child group carrying `values` -- schema-free.

    cell_rise, fall_transition, ocv_sigma_* (LVF), retain_*_slew and anything a
    future release invents are all picked up without being named here.
    """
    out = {}  # type: Dict[str, Dict[str, Any]]
    for child in group.groups:
        if not child.has('values'):
            continue
        key = child.type
        n = 2
        while key in out:  # duplicate table type under one arc: keep both
            key = '%s#%d' % (child.type, n)
            n += 1
        out[key] = _table(child, where)
    return out


def _simple_attrs(group):
    # type: (Group) -> Dict[str, str]
    """Attributes as a flat dict; complex attributes joined back with commas."""
    out = {}  # type: Dict[str, str]
    for key, value in group.attrs:
        text = ', '.join(value) if isinstance(value, list) else value
        if key in out and out[key] != text:
            out[key] = '%s | %s' % (out[key], text)  # repeated: keep both
        else:
            out[key] = text
    return out


def _arc_key(pin_name, timing):
    # type: (str, Group) -> str
    return '|'.join([
        pin_name,
        str(timing.get('related_pin', '')),
        str(timing.get('timing_type', 'combinational')),
        str(timing.get('timing_sense', '')),
        str(timing.get('when', '')),
    ])


def extract(lib, cells=None, with_timing=True):
    # type: (Group, Optional[Sequence[str]], bool) -> Dict[str, Any]
    """Flatten a parsed library into comparable plain data."""
    out = {
        'name': lib.name,
        'header': _simple_attrs(lib),
        'operating_conditions': {},
        'voltage_map': {},
        'cells': {},
    }  # type: Dict[str, Any]

    for oc in lib.find('operating_conditions'):
        out['operating_conditions'][oc.name] = _simple_attrs(oc)
    for raw in lib.get_all('voltage_map'):
        if isinstance(raw, list) and len(raw) == 2:
            out['voltage_map'][raw[0]] = raw[1]

    for cell in lib.find('cell'):
        if cells and not any(fnmatch.fnmatch(cell.name, p) for p in cells):
            continue
        entry = {
            'attributes': _simple_attrs(cell),
            'leakage_power': {},
            'pins': {},
            'arcs': {},
        }  # type: Dict[str, Any]

        for lp in cell.find('leakage_power'):
            entry['leakage_power'][str(lp.get('when', ''))] = lp.get_float('value')

        # bus groups carry pin-level attributes too, and their member pins live
        # underneath them -- find_all catches both.
        pin_groups = list(cell.find_all('pin')) + list(cell.find('bus'))
        for pin in pin_groups:
            pentry = {'attributes': _simple_attrs(pin), 'pg_pin': {}}
            for pg in pin.find('pg_pin'):
                pentry['pg_pin'][pg.name] = _simple_attrs(pg)
            entry['pins'][pin.name] = pentry

            if not with_timing:
                continue
            for timing in pin.find('timing'):
                key = _arc_key(pin.name, timing)
                where = '%s/%s' % (cell.name, key)
                entry['arcs'][key] = {
                    'attributes': _simple_attrs(timing),
                    'tables': _tables_of(timing, where),
                }

        for pg in cell.find('pg_pin'):
            entry.setdefault('pg_pin', {})[pg.name] = _simple_attrs(pg)

        out['cells'][cell.name] = entry
    return out


def load(path, cells=None, with_timing=True, skip_groups=None):
    # type: (str, Optional[Sequence[str]], bool, Optional[Sequence[str]]) -> Dict[str, Any]
    """Parse and extract one library file."""
    if skip_groups is None:
        # CCS current vectors are never compared numerically here and are the
        # bulk of an advanced-node file, so they are dropped before they are built.
        skip_groups = CCS_GROUPS
    data = extract(parse_file(path, skip_groups=skip_groups), cells, with_timing)
    data['file'] = path
    return data


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------

def _pct(ref, other):
    # type: (float, float) -> Optional[float]
    if ref == 0.0:
        return None
    return (other - ref) / abs(ref) * 100.0


def _equal(a, b, tol):
    # type: (float, float, float) -> bool
    return abs(a - b) <= tol * max(1.0, abs(a))


def _as_float(text):
    # type: (Any) -> Optional[float]
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _scalar_delta(scope, name, metric, ref_raw, other_raw, tol):
    # type: (str, str, str, Any, Any, float) -> Optional[Dict[str, Any]]
    ref, other = _as_float(ref_raw), _as_float(other_raw)
    if ref is None or other is None or _equal(ref, other, tol):
        return None
    return {
        'scope': scope, 'name': name, 'metric': metric,
        'ref': ref, 'other': other,
        'delta': other - ref, 'pct': _pct(ref, other),
    }


def _diff_table(ref_tab, other_tab, tol):
    # type: (Dict[str, Any], Dict[str, Any], float) -> Optional[Dict[str, Any]]
    """Elementwise comparison, or a grid_mismatch flag. Never interpolates."""
    if ref_tab['indexes'] != other_tab['indexes']:
        return {'status': 'grid_mismatch',
                'ref_indexes': ref_tab['indexes'],
                'other_indexes': other_tab['indexes']}

    ref_rows, other_rows = ref_tab['values'], other_tab['values']
    if len(ref_rows) != len(other_rows) or \
            any(len(a) != len(b) for a, b in zip(ref_rows, other_rows)):
        return {'status': 'shape_mismatch',
                'ref_shape': [len(r) for r in ref_rows],
                'other_shape': [len(r) for r in other_rows]}

    pcts = []  # type: List[float]
    worst = None  # type: Optional[Dict[str, Any]]
    changed = False
    idx1 = ref_tab['indexes'][0] if ref_tab['indexes'] else []
    idx2 = ref_tab['indexes'][1] if len(ref_tab['indexes']) > 1 else []
    for i, (ref_row, other_row) in enumerate(zip(ref_rows, other_rows)):
        for j, (a, b) in enumerate(zip(ref_row, other_row)):
            if not _equal(a, b, tol):
                changed = True
            pct = _pct(a, b)
            if pct is None:
                continue
            pcts.append(pct)
            if worst is None or abs(pct) > abs(worst['pct']):
                worst = {
                    'pct': pct, 'ref': a, 'other': b,
                    'index_1': idx1[i] if i < len(idx1) else None,
                    'index_2': idx2[j] if j < len(idx2) else None,
                }
    if not changed:
        return None
    return {
        'status': 'changed',
        'min_pct': min(pcts) if pcts else None,
        'max_pct': max(pcts) if pcts else None,
        'mean_pct': (sum(pcts) / len(pcts)) if pcts else None,
        'worst': worst,
    }


def _set_diff(ref_keys, other_keys):
    # type: (Any, Any) -> Dict[str, Any]
    ref_set, other_set = set(ref_keys), set(other_keys)
    return {
        'only_in_ref': sorted(ref_set - other_set),
        'only_in_other': sorted(other_set - ref_set),
        'common': len(ref_set & other_set),
    }


def diff(ref, other, tol=1e-9):
    # type: (Dict[str, Any], Dict[str, Any], float) -> Dict[str, Any]
    """Compare two extracted libraries. `ref` is the baseline."""
    result = {
        'ref': {'name': ref['name'], 'file': ref.get('file')},
        'other': {'name': other['name'], 'file': other.get('file')},
        'critical_mismatch': [],
        'header': [],
        'cells': _set_diff(ref['cells'], other['cells']),
        'scalars': [],
        'pins': {},
        'arcs': {},
        'tables': [],
    }  # type: Dict[str, Any]

    # -- header ----------------------------------------------------------
    for key in sorted(set(ref['header']) | set(other['header'])):
        a, b = ref['header'].get(key), other['header'].get(key)
        if a == b:
            continue
        row = {'attribute': key, 'ref': a, 'other': b}
        result['header'].append(row)
        if key in CRITICAL_HEADER:
            result['critical_mismatch'].append(row)

    # -- cells -----------------------------------------------------------
    for name in sorted(set(ref['cells']) & set(other['cells'])):
        rc, oc = ref['cells'][name], other['cells'][name]

        for metric in CELL_SCALARS:
            delta = _scalar_delta('cell', name, metric,
                                  rc['attributes'].get(metric),
                                  oc['attributes'].get(metric), tol)
            if delta:
                result['scalars'].append(delta)

        for when in sorted(set(rc['leakage_power']) & set(oc['leakage_power'])):
            delta = _scalar_delta('cell', name, 'leakage_power[%s]' % when,
                                  rc['leakage_power'][when],
                                  oc['leakage_power'][when], tol)
            if delta:
                result['scalars'].append(delta)

        pin_sets = _set_diff(rc['pins'], oc['pins'])
        if pin_sets['only_in_ref'] or pin_sets['only_in_other']:
            result['pins'][name] = pin_sets

        for pin in sorted(set(rc['pins']) & set(oc['pins'])):
            ra, oa = rc['pins'][pin]['attributes'], oc['pins'][pin]['attributes']
            for metric in PIN_SCALARS:
                delta = _scalar_delta('pin', '%s/%s' % (name, pin), metric,
                                      ra.get(metric), oa.get(metric), tol)
                if delta:
                    result['scalars'].append(delta)
            if ra.get('function') != oa.get('function'):
                result['scalars'].append({
                    'scope': 'pin', 'name': '%s/%s' % (name, pin),
                    'metric': 'function', 'ref': ra.get('function'),
                    'other': oa.get('function'), 'delta': None, 'pct': None,
                })

        arc_sets = _set_diff(rc['arcs'], oc['arcs'])
        if arc_sets['only_in_ref'] or arc_sets['only_in_other']:
            result['arcs'][name] = arc_sets

        for arc in sorted(set(rc['arcs']) & set(oc['arcs'])):
            rt, ot = rc['arcs'][arc]['tables'], oc['arcs'][arc]['tables']
            for table in sorted(set(rt) & set(ot)):
                td = _diff_table(rt[table], ot[table], tol)
                if td:
                    td.update({'cell': name, 'arc': arc, 'table': table})
                    result['tables'].append(td)

    result['scalars'].sort(key=lambda d: -abs(d['pct'] or 0.0))
    result['tables'].sort(
        key=lambda d: -max(abs(d.get('max_pct') or 0.0),
                           abs(d.get('min_pct') or 0.0)))
    return result


def compare_files(paths, tol=1e-9, cells=None, with_timing=True):
    # type: (Sequence[str], float, Optional[Sequence[str]], bool) -> Dict[str, Any]
    """Load every file and diff each against the first."""
    libs = [load(p, cells, with_timing) for p in paths]
    return {
        'reference': {'name': libs[0]['name'], 'file': libs[0]['file']},
        'libraries': [{'name': l['name'], 'file': l['file']} for l in libs],
        'comparisons': [diff(libs[0], other, tol) for other in libs[1:]],
        'extracted': libs,
    }


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px;
 background:#fbfbfc;color:#1a1a1a}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px;
 border-bottom:1px solid #e2e2e6;padding-bottom:4px}
h3{font-size:14px;margin:18px 0 6px;color:#444}
.sub{color:#666;margin:0 0 18px}
table{border-collapse:collapse;width:100%;margin:6px 0 14px;font-size:13px}
th,td{border:1px solid #e2e2e6;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f2f2f5;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.up{color:#a11}.down{color:#161}.flat{color:#777}
.warn{background:#fdecec;border:1px solid #e0a0a0;padding:10px 14px;margin:12px 0;
 border-radius:4px}
.warn strong{color:#a11}
.ok{background:#eef7ee;border:1px solid #b6d7b6;padding:10px 14px;border-radius:4px}
details{margin:4px 0;border:1px solid #e2e2e6;border-radius:4px;background:#fff}
summary{cursor:pointer;padding:6px 10px;font-weight:600}
details>div{padding:0 10px 8px}
code{background:#f2f2f5;padding:1px 4px;border-radius:3px;font-size:12px}
.pill{display:inline-block;background:#eef;border:1px solid #ccd;border-radius:10px;
 padding:0 8px;margin-right:6px;font-size:12px}
.scroll{overflow-x:auto}
"""

_MAX_LIST = 200  # ponytail: cap the only-in-X lists; full data is in --json


def _e(value):
    # type: (Any) -> str
    return html.escape('' if value is None else str(value))


def _fmt(value, digits=6):
    # type: (Any, int) -> str
    if value is None:
        return '-'
    if isinstance(value, float):
        return ('%.*g' % (digits, value))
    return str(value)


def _pct_cell(pct):
    # type: (Optional[float]) -> str
    if pct is None:
        return '<td class="num flat">-</td>'
    cls = 'up' if pct > 0 else ('down' if pct < 0 else 'flat')
    return '<td class="num %s">%+.3f%%</td>' % (cls, pct)


def _list_block(title, items):
    # type: (str, List[str]) -> str
    if not items:
        return ''
    shown = items[:_MAX_LIST]
    more = '' if len(items) <= _MAX_LIST else \
        ' <em>(+%d more, see --json)</em>' % (len(items) - _MAX_LIST)
    return '<p><strong>%s (%d):</strong> %s%s</p>' % (
        _e(title), len(items),
        ' '.join('<span class="pill">%s</span>' % _e(i) for i in shown), more)


def _table_html(headers, rows):
    # type: (Sequence[str], Sequence[Sequence[str]]) -> str
    out = ['<div class="scroll"><table><tr>']
    out.extend('<th>%s</th>' % _e(h) for h in headers)
    out.append('</tr>')
    for row in rows:
        out.append('<tr>%s</tr>' % ''.join(row))
    out.append('</table></div>')
    return ''.join(out)


def _render_comparison(cmp_):
    # type: (Dict[str, Any]) -> str
    out = []
    ref_name = cmp_['ref']['name'] or cmp_['ref']['file']
    other_name = cmp_['other']['name'] or cmp_['other']['file']
    out.append('<h2>%s &rarr; %s</h2>' % (_e(ref_name), _e(other_name)))
    out.append('<p class="sub">Reference: <code>%s</code> &nbsp; Compared: '
               '<code>%s</code></p>' % (_e(cmp_['ref']['file']),
                                        _e(cmp_['other']['file'])))

    if cmp_['critical_mismatch']:
        rows = ['<td>%s</td><td>%s</td><td>%s</td>' % (
            _e(r['attribute']), _e(r['ref']), _e(r['other']))
            for r in cmp_['critical_mismatch']]
        out.append('<div class="warn"><strong>Unit / threshold mismatch.</strong> '
                   'Every numeric delta below is meaningless until this is '
                   'resolved.%s</div>'
                   % _table_html(['attribute', ref_name, other_name], rows))

    # header
    if cmp_['header']:
        rows = ['<td>%s</td><td>%s</td><td>%s</td>' % (
            _e(r['attribute']), _e(r['ref']), _e(r['other']))
            for r in cmp_['header']]
        out.append('<h3>Library attributes that differ (%d)</h3>' % len(rows))
        out.append(_table_html(['attribute', ref_name, other_name], rows))
    else:
        out.append('<h3>Library attributes</h3><div class="ok">Identical.</div>')

    # cells
    cells = cmp_['cells']
    out.append('<h3>Cell inventory</h3>')
    out.append('<p>%d cells in common.</p>' % cells['common'])
    out.append(_list_block('Only in %s' % ref_name, cells['only_in_ref']))
    out.append(_list_block('Only in %s' % other_name, cells['only_in_other']))
    if not (cells['only_in_ref'] or cells['only_in_other']):
        out.append('<div class="ok">Same cell set.</div>')

    # pins / arcs
    for label, blob in (('Pin', cmp_['pins']), ('Timing arc', cmp_['arcs'])):
        if not blob:
            continue
        out.append('<h3>%s differences (%d cells affected)</h3>' % (label, len(blob)))
        for cell in sorted(blob):
            body = (_list_block('Only in %s' % ref_name, blob[cell]['only_in_ref']) +
                    _list_block('Only in %s' % other_name, blob[cell]['only_in_other']))
            out.append('<details><summary>%s</summary><div>%s</div></details>'
                       % (_e(cell), body))

    # scalars
    out.append('<h3>Scalar deltas (%d)</h3>' % len(cmp_['scalars']))
    if cmp_['scalars']:
        rows = []
        for d in cmp_['scalars']:
            rows.append(
                '<td>%s</td><td>%s</td><td>%s</td>'
                '<td class="num">%s</td><td class="num">%s</td>'
                '<td class="num">%s</td>%s' % (
                    _e(d['scope']), _e(d['name']), _e(d['metric']),
                    _fmt(d['ref']), _fmt(d['other']), _fmt(d['delta']),
                    _pct_cell(d['pct'])))
        out.append(_table_html(
            ['scope', 'name', 'metric', ref_name, other_name, 'delta', '%'], rows))
    else:
        out.append('<div class="ok">No scalar differences.</div>')

    # tables
    out.append('<h3>Timing / power table deltas (%d)</h3>' % len(cmp_['tables']))
    if cmp_['tables']:
        by_cell = {}  # type: Dict[str, List[Dict[str, Any]]]
        for t in cmp_['tables']:
            by_cell.setdefault(t['cell'], []).append(t)
        for cell in sorted(by_cell, key=lambda c: -max(
                abs(t.get('max_pct') or 0.0) for t in by_cell[c])):
            rows = []
            for t in by_cell[cell]:
                if t['status'] != 'changed':
                    rows.append(
                        '<td>%s</td><td>%s</td><td colspan="5"><strong>%s</strong> '
                        'ref=%s other=%s</td>' % (
                            _e(t['arc']), _e(t['table']), _e(t['status']),
                            _e(t.get('ref_indexes') or t.get('ref_shape')),
                            _e(t.get('other_indexes') or t.get('other_shape'))))
                    continue
                worst = t.get('worst') or {}
                rows.append(
                    '<td>%s</td><td>%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td>%s</td>' % (
                        _e(t['arc']), _e(t['table']),
                        _fmt(t['min_pct'], 4), _fmt(t['max_pct'], 4),
                        _fmt(t['mean_pct'], 4),
                        _fmt(worst.get('pct'), 4),
                        'idx1=%s idx2=%s (%s &rarr; %s)' % (
                            _fmt(worst.get('index_1')), _fmt(worst.get('index_2')),
                            _fmt(worst.get('ref')), _fmt(worst.get('other')))))
            # ponytail: collapsed by default so a 5000-cell report still opens.
            out.append('<details><summary>%s (%d tables)</summary><div>%s</div>'
                       '</details>' % (_e(cell), len(by_cell[cell]),
                                       _table_html(
                                           ['arc', 'table', 'min %', 'max %',
                                            'mean %', 'worst %', 'worst point'],
                                           rows)))
    else:
        out.append('<div class="ok">No table differences.</div>')
    return ''.join(out)


def render_html(report):
    # type: (Dict[str, Any]) -> str
    """Self-contained HTML: inline CSS, no external assets, no JavaScript."""
    files = ' '.join('<span class="pill">%s</span>' % _e(os.path.basename(l['file']))
                     for l in report['libraries'])
    body = [
        '<h1>Liberty library comparison</h1>',
        '<p class="sub">%d libraries, reference <code>%s</code></p>'
        % (len(report['libraries']), _e(report['reference']['file'])),
        '<p>%s</p>' % files,
    ]
    for cmp_ in report['comparisons']:
        body.append(_render_comparison(cmp_))
    return ('<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Liberty comparison: %s</title><style>%s</style></head>'
            '<body>%s</body></html>\n'
            % (_e(report['reference']['name']), _CSS, ''.join(body)))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _summarise(report, stream=sys.stdout):
    # type: (Dict[str, Any], Any) -> None
    for cmp_ in report['comparisons']:
        print('%s vs %s' % (cmp_['ref']['name'], cmp_['other']['name']), file=stream)
        if cmp_['critical_mismatch']:
            print('  !! UNIT/THRESHOLD MISMATCH on %s -- numeric deltas below are '
                  'meaningless' % ', '.join(r['attribute']
                                            for r in cmp_['critical_mismatch']),
                  file=stream)
        print('  header attrs differing : %d' % len(cmp_['header']), file=stream)
        print('  cells only in ref      : %d' % len(cmp_['cells']['only_in_ref']),
              file=stream)
        print('  cells only in other    : %d' % len(cmp_['cells']['only_in_other']),
              file=stream)
        print('  cells in common        : %d' % cmp_['cells']['common'], file=stream)
        print('  scalar deltas          : %d' % len(cmp_['scalars']), file=stream)
        print('  table deltas           : %d' % len(cmp_['tables']), file=stream)
        for d in cmp_['scalars'][:5]:
            print('    %-6s %-28s %-22s %s -> %s (%s)' % (
                d['scope'], d['name'][:28], d['metric'],
                _fmt(d['ref']), _fmt(d['other']),
                '-' if d['pct'] is None else '%+.2f%%' % d['pct']), file=stream)


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    import argparse
    ap = argparse.ArgumentParser(
        description='Compare Liberty libraries. First file is the reference.')
    ap.add_argument('libfiles', nargs='+')
    ap.add_argument('--html', metavar='OUT', help='write a self-contained HTML report')
    ap.add_argument('--json', metavar='OUT', help='write the full diff as JSON')
    ap.add_argument('--tol', type=float, default=1e-9,
                    help='relative tolerance below which values count as equal')
    ap.add_argument('--cells', metavar='GLOBS', default='',
                    help='comma-separated cell name globs, e.g. "INV*,BUF*"')
    ap.add_argument('--no-timing', action='store_true',
                    help='skip timing/power tables (much faster and smaller)')
    args = ap.parse_args(argv)

    if len(args.libfiles) < 2:
        ap.error('need at least two libraries to compare')

    cells = [c.strip() for c in args.cells.split(',') if c.strip()] or None
    report = compare_files(args.libfiles, tol=args.tol, cells=cells,
                           with_timing=not args.no_timing)

    _summarise(report)

    if args.json:
        with io.open(args.json, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(report, indent=2, ensure_ascii=False,
                                sort_keys=True))
        print('wrote %s' % args.json)
    if args.html:
        with io.open(args.html, 'w', encoding='utf-8') as fh:
            fh.write(render_html(report))
        print('wrote %s' % args.html)
    return 0


if __name__ == '__main__':
    sys.exit(main())
