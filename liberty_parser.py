#!/usr/bin/env python3.7
# -*- coding: utf-8 -*-
"""Schema-free parser for Synopsys Liberty (.lib) files.

The Liberty *grammar* is tiny and stable; only the *vocabulary* grows. Every
construct in a modern advanced-node library -- ocv_sigma_* (LVF/OCV),
output_current_* (CCS), ccsn_* (CCS noise), pg_pin, voltage_map, compact LUTs,
whatever the next release invents -- is one of exactly three forms:

    name : value ;              simple attribute
    name ( a, b, c ) ;          complex attribute   (define, values, index_1 ...)
    name ( args ) { ... }       group

So this parser knows the grammar and *nothing* about the vocabulary. There is no
keyword list, no schema, no whitelist. An unrecognised group or attribute is kept
in the tree verbatim instead of being dropped or raising. That is what makes it
safe against constructs newer than the parser.

Usage:
    from liberty_parser import parse_file
    lib = parse_file('my.lib')
    for cell in lib.find('cell'):
        print(cell.name, cell.get('area'))

CLI:
    python3.7 liberty_parser.py my.lib --stats
    python3.7 liberty_parser.py my.lib --json out.json
    python3.7 liberty_parser.py my.lib --show cell:INVx1
"""

from __future__ import print_function

import gzip
import io
import json
import os
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

__all__ = ['Group', 'LibertySyntaxError', 'parse_file', 'parse_string',
           'tokenize', 'CCS_GROUPS']

# Group types whose bodies dominate the size of an advanced-node library.
# Hand this to parse_file(skip_groups=...) when you only care about NLDM data.
CCS_GROUPS = frozenset([
    'output_current_rise', 'output_current_fall',
    'ccsn_first_stage', 'ccsn_last_stage',
    'receiver_capacitance', 'compact_ccs_rise', 'compact_ccs_fall',
])

AttrValue = Union[str, List[str]]


class LibertySyntaxError(Exception):
    """Syntax error carrying file, line, column and the enclosing group path.

    A 300 MB library with "unexpected token" and no location is unusable, so
    every parse error knows exactly where it happened and what it was inside.
    """

    def __init__(self, message, filename=None, line=0, col=0, path=''):
        # type: (str, Optional[str], int, int, str) -> None
        self.message = message
        self.filename = filename
        self.line = line
        self.col = col
        self.path = path
        where = '%s:%d:%d' % (filename or '<string>', line, col)
        if path:
            where += ' (in %s)' % path
        super(LibertySyntaxError, self).__init__('%s: %s' % (where, message))


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

# Alternation order is significant: comments and strings must win over
# everything else, or a `;` inside a comment ends an attribute and a `{` inside
# a quoted string opens a phantom group.
#
# The `word` branch accepts a backslash only when it is NOT a line continuation,
# so `values( \` splits correctly while an escaped identifier like `\A1` stays
# one token.
_TOKEN_RE = re.compile(r"""
      (?P<comment> /\*.*?\*/ | //[^\n]* )
    | (?P<cont>    \\[ \t]*\r?\n )
    | (?P<string>  "(?:[^"\\]|\\.)*" )
    | (?P<newline> \r?\n )
    | (?P<space>   [ \t\f\v]+ )
    | (?P<punct>   [:;(){},] )
    | (?P<word>    (?:[^\s:;(){},"\\]|\\(?!\r?\n))+ )
""", re.DOTALL | re.VERBOSE)

_EOF = 'eof'
_DROPPED = frozenset(['comment', 'cont', 'space'])

# Only these can contain a newline, so the line counter skips the scan on the
# words and punctuation that make up most of the stream.
_MAY_SPAN_LINES = frozenset(['comment', 'cont', 'newline', 'string'])

Token = Tuple[str, str, int, int]  # kind, value, line, col


def tokenize(text, filename=None):
    # type: (str, Optional[str]) -> Iterator[Token]
    """Yield (kind, value, line, col) tokens. Comments/continuations are dropped.

    Quoted strings are yielded with their quotes stripped and escapes resolved.
    """
    pos = 0
    end = len(text)
    line = 1
    line_start = 0
    match = _TOKEN_RE.match
    while pos < end:
        m = match(text, pos)
        if m is None:
            # Only reachable via an unterminated quote or a stray backslash at EOF.
            raise LibertySyntaxError(
                'unexpected character %r (unterminated string?)' % text[pos],
                filename, line, pos - line_start + 1)
        kind = m.lastgroup
        value = m.group()
        start = pos
        pos = m.end()

        newlines = value.count('\n') if kind in _MAY_SPAN_LINES else 0
        if kind not in _DROPPED:
            col = start - line_start + 1
            if kind == 'string':
                yield ('string', _unescape(value[1:-1]), line, col)
            elif kind != 'newline':
                yield (kind, value, line, col)
            else:
                yield ('newline', '\n', line, col)
        if newlines:
            line += newlines
            line_start = start + value.rfind('\n') + 1
    yield (_EOF, '', line, pos - line_start + 1)


_ESCAPE_RE = re.compile(r'\\(.)')


def _unescape(s):
    # type: (str) -> str
    return _ESCAPE_RE.sub(r'\1', s) if '\\' in s else s


class _Stream(object):
    """One-token-lookahead view over the token generator.

    A generator rather than a list: a 300 MB library is ~30M tokens, and holding
    them all as tuples costs more than the tree itself.
    """

    __slots__ = ('_it', '_buf', '_last')

    def __init__(self, it):
        # type: (Iterator[Token]) -> None
        self._it = it
        self._buf = None  # type: Optional[Token]
        self._last = (_EOF, '', 0, 0)  # type: Token

    def peek(self):
        # type: () -> Token
        if self._buf is None:
            try:
                self._buf = next(self._it)
            except StopIteration:
                self._buf = self._last
        return self._buf

    def next(self):
        # type: () -> Token
        tok = self.peek()
        if tok[0] != _EOF:
            self._buf = None
        self._last = tok
        return tok

    def skip_newlines(self):
        # type: () -> None
        while self.peek()[0] == 'newline':
            self.next()


# --------------------------------------------------------------------------
# Tree
# --------------------------------------------------------------------------

class Group(object):
    """One Liberty group: `type (args) { attributes; subgroups }`.

    Attributes are a *list* of (name, value) pairs, not a dict: Liberty allows
    the same attribute name more than once (repeated `when`, `related_pin`,
    vendor extensions) and order is meaningful. Values stay raw strings -- a
    simple attribute is a `str`, a complex attribute is a `List[str]`. Nothing
    is coerced to float here: parsing stays fast and lossless, and `1.0000e-03`
    round-trips. Coercion belongs where units are known.
    """

    __slots__ = ('type', 'args', 'attrs', 'groups', 'parent')

    def __init__(self, type_, args=None, parent=None):
        # type: (str, Optional[List[str]], Optional[Group]) -> None
        self.type = type_
        self.args = args if args is not None else []  # type: List[str]
        self.attrs = []  # type: List[Tuple[str, AttrValue]]
        self.groups = []  # type: List[Group]
        self.parent = parent

    # -- identity ---------------------------------------------------------

    @property
    def name(self):
        # type: () -> str
        """First group argument, or '' for anonymous groups like `timing ()`."""
        return self.args[0] if self.args else ''

    @property
    def path(self):
        # type: () -> str
        """`library:foo/cell:INVx1/pin:Y/timing` -- used in error messages."""
        parts = []
        node = self  # type: Optional[Group]
        while node is not None:
            parts.append('%s:%s' % (node.type, node.name) if node.name else node.type)
            node = node.parent
        return '/'.join(reversed(parts))

    def __repr__(self):
        # type: () -> str
        return '<Group %s attrs=%d groups=%d>' % (
            self.path, len(self.attrs), len(self.groups))

    # -- attribute access -------------------------------------------------

    def get(self, name, default=None):
        # type: (str, Any) -> Any
        """First value of attribute `name`, or `default` if absent."""
        for key, value in self.attrs:
            if key == name:
                return value
        return default

    def get_all(self, name):
        # type: (str) -> List[AttrValue]
        """Every value of attribute `name`, in file order."""
        return [value for key, value in self.attrs if key == name]

    def get_float(self, name, default=None):
        # type: (str, Any) -> Any
        """First value of `name` as float, or `default` if absent/not a number."""
        raw = self.get(name)
        if raw is None or isinstance(raw, list):
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def has(self, name):
        # type: (str) -> bool
        return any(key == name for key, _ in self.attrs)

    def attr_names(self):
        # type: () -> List[str]
        return [key for key, _ in self.attrs]

    # -- group access -----------------------------------------------------

    def find(self, type_=None):
        # type: (Optional[str]) -> Iterator[Group]
        """Direct child groups, optionally filtered by type."""
        for g in self.groups:
            if type_ is None or g.type == type_:
                yield g

    def find_all(self, type_=None):
        # type: (Optional[str]) -> Iterator[Group]
        """All descendant groups (depth-first), optionally filtered by type."""
        for g in self.groups:
            if type_ is None or g.type == type_:
                yield g
            for sub in g.find_all(type_):
                yield sub

    def first(self, type_):
        # type: (str) -> Optional[Group]
        for g in self.groups:
            if g.type == type_:
                return g
        return None

    def by_name(self, type_, name):
        # type: (str, str) -> Optional[Group]
        for g in self.groups:
            if g.type == type_ and g.name == name:
                return g
        return None

    # -- serialisation ----------------------------------------------------

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """Plain nested dict/list, JSON-dumpable, lossless (duplicates kept)."""
        return {
            'type': self.type,
            'args': list(self.args),
            'attributes': [[k, v] for k, v in self.attrs],
            'groups': [g.to_dict() for g in self.groups],
        }

    def dumps(self, indent=0):
        # type: (int) -> str
        """Re-emit as Liberty-ish text. For eyeballing a subtree, not round-tripping."""
        pad = '  ' * indent
        head = '%s%s (%s) {' % (pad, self.type, ', '.join(self.args))
        lines = [head]
        for key, value in self.attrs:
            if isinstance(value, list):
                lines.append('%s  %s (%s);' % (pad, key, ', '.join(
                    _requote(v) for v in value)))
            else:
                lines.append('%s  %s : %s;' % (pad, key, _requote(value)))
        for g in self.groups:
            lines.append(g.dumps(indent + 1))
        lines.append('%s}' % pad)
        return '\n'.join(lines)


_BARE_RE = re.compile(r'^[A-Za-z0-9_.+\-]+$')


def _requote(value):
    # type: (str) -> str
    return value if _BARE_RE.match(value) else '"%s"' % value


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

class _Parser(object):
    __slots__ = ('stream', 'filename', 'skip_groups')

    def __init__(self, stream, filename, skip_groups):
        # type: (_Stream, Optional[str], frozenset) -> None
        self.stream = stream
        self.filename = filename
        self.skip_groups = skip_groups

    def error(self, message, tok, group=None):
        # type: (str, Token, Optional[Group]) -> LibertySyntaxError
        return LibertySyntaxError(message, self.filename, tok[2], tok[3],
                                  group.path if group is not None else '')

    def parse_top(self):
        # type: () -> Group
        root = Group('_file')
        self.parse_body(root, top_level=True)
        # A file normally holds exactly one `library` group; return it directly.
        # Multiple top-level groups (rare but legal) come back under the wrapper.
        if len(root.groups) == 1 and not root.attrs:
            only = root.groups[0]
            only.parent = None
            return only
        return root

    def parse_body(self, group, top_level=False):
        # type: (Group, bool) -> None
        stream = self.stream
        while True:
            kind, value, _, _ = tok = stream.peek()

            if kind == _EOF:
                if top_level:
                    return
                raise self.error('unexpected end of file, unclosed group', tok, group)

            if kind == 'newline':
                stream.next()
                continue

            if kind == 'punct':
                if value == '}':
                    if top_level:
                        raise self.error("unmatched '}'", tok, group)
                    stream.next()
                    self._eat_optional_semicolon()
                    return
                if value == ';':  # stray separator, harmless
                    stream.next()
                    continue
                raise self.error('unexpected %r' % value, tok, group)

            if kind not in ('word', 'string'):
                raise self.error('unexpected token %r' % value, tok, group)

            stream.next()
            self._parse_statement(group, value, tok)

    def _parse_statement(self, group, name, name_tok):
        # type: (Group, str, Token) -> None
        stream = self.stream
        kind, value, _, _ = tok = stream.peek()

        if kind == 'punct' and value == ':':
            stream.next()
            group.attrs.append((name, self._parse_simple_value(group)))
            return

        if kind == 'punct' and value == '(':
            stream.next()
            args = self._parse_args(group)
            stream.skip_newlines()
            nxt = stream.peek()
            if nxt[0] == 'punct' and nxt[1] == '{':
                stream.next()
                self._parse_group(group, name, args)
            else:
                # Complex attribute: values(...), index_1(...), define(...),
                # capacitive_load_unit(1, pf). `define` needs no special case.
                group.attrs.append((name, args))
                self._eat_optional_semicolon()
            return

        raise self.error("expected ':' or '(' after %r" % name, tok, group)

    def _parse_group(self, parent, type_, args):
        # type: (Group, str, List[str]) -> None
        if type_ in self.skip_groups:
            self._skip_braced_body(parent)
            return
        child = Group(type_, args, parent)
        parent.groups.append(child)
        self.parse_body(child)

    def _skip_braced_body(self, group):
        # type: (Group) -> None
        """Discard a group body by brace counting. The opening '{' is consumed.

        Strings and comments are already tokenized, so braces inside them cannot
        confuse the depth count.
        """
        stream = self.stream
        depth = 1
        while depth:
            kind, value, _, _ = tok = stream.next()
            if kind == _EOF:
                raise self.error('unexpected end of file in skipped group', tok, group)
            if kind == 'punct':
                if value == '{':
                    depth += 1
                elif value == '}':
                    depth -= 1
        self._eat_optional_semicolon()

    def _parse_simple_value(self, group):
        # type: (Group) -> str
        """Read a simple attribute value up to ';', end of line, or '}'.

        The trailing ';' is optional in real libraries, so a newline also ends
        the value. Multi-token values (`function : A & B`) are re-joined.
        """
        stream = self.stream
        parts = []  # type: List[str]
        while True:
            kind, value, _, _ = stream.peek()
            if kind == _EOF:
                break
            if kind == 'newline':
                stream.next()
                break
            if kind == 'punct':
                if value == ';':
                    stream.next()
                    break
                if value == '}':  # let the caller close the group
                    break
                stream.next()
                parts.append(value)
                continue
            stream.next()
            parts.append(value)
        if not parts:
            raise self.error('empty attribute value', stream.peek(), group)
        return parts[0] if len(parts) == 1 else ' '.join(parts)

    def _parse_args(self, group):
        # type: (Group) -> List[str]
        """Read comma-separated arguments up to the closing ')'."""
        stream = self.stream
        args = []  # type: List[str]
        parts = []  # type: List[str]
        while True:
            kind, value, _, _ = tok = stream.next()
            if kind == _EOF:
                raise self.error('unexpected end of file inside (...)', tok, group)
            if kind == 'newline':
                continue
            if kind == 'punct':
                if value == ')':
                    if parts:
                        args.append(' '.join(parts))
                    elif args:  # trailing comma
                        args.append('')
                    return args
                if value == ',':
                    args.append(' '.join(parts))
                    parts = []
                    continue
                parts.append(value)
                continue
            parts.append(value)

    def _eat_optional_semicolon(self):
        # type: () -> None
        stream = self.stream
        while True:
            kind, value, _, _ = stream.peek()
            if kind == 'newline' or (kind == 'punct' and value == ';'):
                stream.next()
                if kind == 'punct':
                    return
                continue
            return


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def parse_string(text, filename=None, skip_groups=frozenset()):
    # type: (str, Optional[str], Sequence[str]) -> Group
    """Parse Liberty source text into a Group tree."""
    parser = _Parser(_Stream(tokenize(text, filename)), filename,
                     frozenset(skip_groups))
    return parser.parse_top()


def parse_file(path, skip_groups=frozenset(), encoding='utf-8'):
    # type: (str, Sequence[str], str) -> Group
    """Parse a .lib (or .lib.gz) file into a Group tree.

    skip_groups -- group types whose bodies are discarded by brace counting
        instead of built. Pass CCS_GROUPS to drop the current-vector data that
        makes up the bulk of an advanced-node library.
    """
    # ponytail: whole file is read into memory, then regex-tokenized at roughly
    # 3.5 MB/s (measured: 14 MB in ~4 s), so a 200 MB library takes about a
    # minute. Tokenizing is ~2/3 of that. If either the time or the memory ever
    # bites: emit fewer tokens (whitespace is ~35% of the matches) before
    # reaching for chunked scanning with a carry buffer.
    opener = gzip.open if path.endswith('.gz') else io.open
    with opener(path, 'rb') as fh:
        raw = fh.read()
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        # Vendor headers occasionally carry latin-1 bytes in comments. Never let
        # an encoding quirk in a comment block a 300 MB parse.
        text = raw.decode('latin-1')
    return parse_string(text, path, skip_groups)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _collect_stats(root):
    # type: (Group) -> Tuple[Dict[str, int], Dict[str, int], int]
    """Inventory of every group type and attribute name found, plus max depth.

    This is the coverage proof: run it on a real advanced-node library and the
    output lists every construct the file contains. Nothing is filtered, so if
    the grammar handled the file, everything in the file is in this list.
    """
    group_counts = {}  # type: Dict[str, int]
    attr_counts = {}  # type: Dict[str, int]
    max_depth = [0]

    def walk(g, depth):
        # type: (Group, int) -> None
        if depth > max_depth[0]:
            max_depth[0] = depth
        group_counts[g.type] = group_counts.get(g.type, 0) + 1
        for key, _ in g.attrs:
            attr_counts[key] = attr_counts.get(key, 0) + 1
        for child in g.groups:
            walk(child, depth + 1)

    walk(root, 1)
    return group_counts, attr_counts, max_depth[0]


def _print_stats(root, stream=sys.stdout):
    # type: (Group, Any) -> None
    groups, attrs, depth = _collect_stats(root)
    total_groups = sum(groups.values())
    total_attrs = sum(attrs.values())
    print('library      : %s' % root.name, file=stream)
    print('max depth    : %d' % depth, file=stream)
    print('groups       : %d in %d distinct types' % (total_groups, len(groups)),
          file=stream)
    print('attributes   : %d in %d distinct names' % (total_attrs, len(attrs)),
          file=stream)
    print('', file=stream)
    print('%-40s %10s' % ('GROUP TYPE', 'COUNT'), file=stream)
    for key in sorted(groups, key=lambda k: (-groups[k], k)):
        print('%-40s %10d' % (key, groups[key]), file=stream)
    print('', file=stream)
    print('%-40s %10s' % ('ATTRIBUTE NAME', 'COUNT'), file=stream)
    for key in sorted(attrs, key=lambda k: (-attrs[k], k)):
        print('%-40s %10d' % (key, attrs[key]), file=stream)


def _select(root, selector):
    # type: (Group, str) -> List[Group]
    """Find subtrees by `type` or `type:name` (name may use * globs)."""
    import fnmatch
    if ':' in selector:
        want_type, want_name = selector.split(':', 1)
    else:
        want_type, want_name = selector, '*'
    hits = []
    if root.type == want_type and fnmatch.fnmatch(root.name, want_name):
        hits.append(root)
    for g in root.find_all(want_type):
        if fnmatch.fnmatch(g.name, want_name):
            hits.append(g)
    return hits


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    import argparse
    ap = argparse.ArgumentParser(
        description='Parse a Liberty (.lib/.lib.gz) file.')
    ap.add_argument('libfile')
    ap.add_argument('--json', metavar='OUT',
                    help='write the whole tree as JSON ("-" for stdout)')
    ap.add_argument('--stats', action='store_true',
                    help='inventory every group type and attribute name found')
    ap.add_argument('--show', metavar='SELECTOR',
                    help='pretty-print a subtree, e.g. cell:INVx1 or pin:Y')
    ap.add_argument('--skip-groups', metavar='TYPES', default='',
                    help='comma-separated group types to discard, or "ccs" for '
                         'the standard current-vector set')
    args = ap.parse_args(argv)

    if args.skip_groups == 'ccs':
        skip = CCS_GROUPS
    else:
        skip = frozenset(t.strip() for t in args.skip_groups.split(',') if t.strip())

    try:
        root = parse_file(args.libfile, skip_groups=skip)
    except LibertySyntaxError as exc:
        print('parse error: %s' % exc, file=sys.stderr)
        return 2

    if args.stats:
        _print_stats(root)
    if args.show:
        hits = _select(root, args.show)
        if not hits:
            print('no group matched %r' % args.show, file=sys.stderr)
            return 1
        for g in hits:
            print(g.dumps())
    if args.json:
        payload = root.to_dict()
        if args.json == '-':
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write('\n')
        else:
            with io.open(args.json, 'w', encoding='utf-8') as fh:
                fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
            print('wrote %s (%d bytes)' % (args.json, os.path.getsize(args.json)))
    if not (args.stats or args.show or args.json):
        _print_stats(root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
