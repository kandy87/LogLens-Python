"""
Log Lens (desktop) — Windows log viewer for very large (multi-GB) log files.

Design notes
------------
Files are never fully loaded into memory. Opening a file scans it once in a
background thread and records the *byte offset* of every line start (an
array of 64-bit ints — for a 5 GB file with ~50M lines that's ~400 MB of
index, not 5 GB of text). After that, the file is memory-mapped and lines
are read on demand.

Multiple files can be loaded at once. With exactly one file loaded, the
table shows its lines in on-disk order with no extra work — random-access
scrolling is instant and nothing is scanned up front. With two or more
files, their lines are merged into one timeline sorted by timestamp (a
per-file timestamp carried forward for lines that don't have their own,
e.g. stack traces), which does require a one-time full scan of every
loaded file to extract timestamps — there's no way to merge-order lines
without knowing where they fall in time. That scan runs in a background
thread with a progress readout.

Filtering (keyword / trace id / date range, combined with AND or OR) runs
once over the mmapped files in a background thread and produces a second,
much smaller index: the row numbers that matched. Only that index is held
in memory, and a new search cancels whichever one is still in flight.
"""

import re
import sys
import mmap
import os
import base64
import json
from array import array
from datetime import datetime

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QThread, Signal, QTimer, QRect, QRectF, QSize, QPointF, QPoint
)
from PySide6.QtGui import (
    QColor, QPainter, QFont, QFontMetricsF, QIcon, QPixmap, QKeySequence, QPen, QShortcut,
    QTextLayout, QTextLine, QTextCharFormat, QTextOption
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMenu, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QCheckBox, QComboBox, QFileDialog, QTableView,
    QHeaderView, QStyledItemDelegate, QStyle, QStackedWidget, QFrame, QScrollArea,
    QDialog, QTextEdit
)

# ---------------------------------------------------------------------------
# Theme (mirrors the original web UI's CSS variables)
# ---------------------------------------------------------------------------
VERSION = "2.5.0"

BG = "#10151c"
PANEL = "#161d27"
PANEL_2 = "#1c2430"
LINE = "#263140"
TEXT = "#c9d4e0"
TEXT_DIM = "#6c7b8f"
ACCENT = "#ffb454"
ACCENT_2 = "#4a90e2"     # keyword matches / method names — blue
DANGER = "#e0616b"
FONT_FAMILY = "Consolas, 'IBM Plex Mono', 'Courier New', monospace"

LVL_ERROR = "#ff6b6b"
LVL_WARN = "#f2d24b"
LVL_INFO = "#5fd68a"
LVL_DEBUG = "#8a97ab"
JSON_COLOR = "#c9b458"   # dull/muted yellow for embedded JSON payloads

KW_MARK_BG = QColor(74, 144, 226, 82)     # rgba(74,144,226,0.32)
KW_MARK_TEXT = QColor("#eaf3ff")
ROW_DIVIDER = QColor(38, 49, 64, 102)     # rgba(38,49,64,0.4) — matches td's border-bottom
TEXT_SEL_BG = QColor(74, 144, 226, 90)    # translucent blue overlay for cursor-drag text selection

SOURCE_COLORS = ['#4a90e2', '#5fd68a', '#f2b155', '#c792ea', '#ff8fa3', '#4fc1c9']

# The web version pads each row 9px top/bottom and uses line-height:1.6 for
# wrapped text; ROW_HEIGHT (a single unwrapped line) and LINE_HEIGHT_FACTOR
# (spacing between wrapped lines within one row) mirror that so rows read
# with the same breathing room instead of feeling cramped.
ROW_HEIGHT = 38
LINE_HEIGHT_FACTOR = 1.6
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB scan chunks

STYLESHEET = f"""
QMainWindow, QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}
#header {{
    background: {PANEL};
    border-bottom: 1px solid {LINE};
}}
#titleLabel {{
    font-size: 15px;
    font-weight: 600;
    color: #eef2f7;
}}
#dot {{
    background: {ACCENT};
    border-radius: 4px;
}}
#filenameLabel {{
    color: {TEXT_DIM};
    font-size: 12px;
}}
QLineEdit, QComboBox {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    color: {TEXT};
    padding: 6px 8px;
    border-radius: 5px;
    font-family: {FONT_FAMILY};
    font-size: 12.5px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL_2};
    color: {TEXT};
    border: 1px solid {LINE};
    selection-background-color: {ACCENT_2};
}}
QLineEdit:focus, QComboBox:focus {{
    border: 1px solid {ACCENT_2};
}}
QFrame#kwGroup {{
    border: 1px solid {LINE};
    border-radius: 6px;
    background: {PANEL};
}}
QFrame#kwSubgroup {{
    border: 1px solid {ACCENT_2};
    border-radius: 6px;
    background: {PANEL_2};
}}
QPushButton {{
    font-family: {FONT_FAMILY};
    font-size: 12px;
    border-radius: 5px;
    border: 1px solid {LINE};
    background: {PANEL_2};
    color: {TEXT};
    padding: 7px 12px;
}}
QPushButton:hover {{
    border: 1px solid {ACCENT_2};
    color: #ffffff;
}}
QPushButton:disabled {{
    color: #4a5566;
    border: 1px solid {LINE};
}}
QPushButton#primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #1a1206;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    border: 1px solid {ACCENT};
}}
QCheckBox {{
    color: {TEXT_DIM};
    font-size: 11px;
}}
#statusbar {{
    background: {PANEL_2};
    border-bottom: 1px solid {LINE};
    color: {TEXT_DIM};
    font-size: 11.5px;
}}
#statusbar b {{ color: {TEXT}; }}
QTableView {{
    background: {BG};
    color: {TEXT};
    border: none;
    gridline-color: {LINE};
    selection-background-color: rgba(79,193,201,60);
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background: {PANEL_2};
    color: {TEXT_DIM};
    border: none;
    border-right: 1px solid {LINE};
    border-bottom: 1px solid {LINE};
    padding: 4px 8px;
}}
#emptyBig {{ color: {TEXT}; font-size: 13px; }}
#emptyDot {{ color: {TEXT_DIM}; font-size: 11.5px; }}
"""

# ---------------------------------------------------------------------------
# Timestamp extraction (mirrors the regexes in the original web version)
# ---------------------------------------------------------------------------
ISO_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)'
)
SLASH_RE = re.compile(r'(\d{2}/\d{2}/\d{4}\s\d{2}:\d{2}:\d{2})')
DASH_RE = re.compile(r'(\d{2}-\d{2}-\d{4}\s\d{2}:\d{2}:\d{2})')

# Log-level word and method-name coloring (mirrors colorLevelWord / colorMethodName).
LEVEL_PATTERNS = [
    ('error', LVL_ERROR, re.compile(r'\b(ERROR|FATAL|CRITICAL)\b', re.IGNORECASE)),
    ('warn', LVL_WARN, re.compile(r'\b(WARN|WARNING)\b', re.IGNORECASE)),
    ('info', LVL_INFO, re.compile(r'\bINFO\b', re.IGNORECASE)),
    ('debug', LVL_DEBUG, re.compile(r'\b(DEBUG|TRACE)\b', re.IGNORECASE)),
]
LEVEL_SCAN_WINDOW = 60  # only look for the level tag near the start of the line
METHOD_PATTERN = re.compile(r'(method\s*:\s*)(\[[^\]]+\])', re.IGNORECASE)


# A cheap "does this actually look like JSON" signal for the fallback path
# below: real JSON objects/arrays-of-objects are full of quoted-key colon
# patterns; ordinary bracketed log text (thread names, trace IDs, class
# references) never has this shape.
JSON_KV_RE = re.compile(r'"[^"\\]{1,80}?"\s*:')


def _quote_aware_balanced_end(text, i):
    """From an opening '{' or '[' at `text[i]`, scans forward tracking
    quoted strings (so a brace/bracket *inside* a properly-escaped string
    doesn't affect the count) and returns the index just past the matching
    close, or None if it never balances or the bracket types mismatch."""
    stack = [text[i]]
    j = i + 1
    n = len(text)
    in_string = False
    escape = False
    while j < n and stack:
        cj = text[j]
        if in_string:
            if escape:
                escape = False
            elif cj == '\\':
                escape = True
            elif cj == '"':
                in_string = False
        else:
            if cj == '"':
                in_string = True
            elif cj in '{[':
                stack.append(cj)
            elif cj in '}]':
                top = stack.pop()
                if (top == '{' and cj != '}') or (top == '[' and cj != ']'):
                    return None
        j += 1
    return None if stack else j


def _dumb_balanced_end(text, i):
    """Same as above but ignores quotes entirely — pure bracket counting.
    Used as a fallback for logs that embed a JSON structure as a *string
    value* without escaping its inner quotes (a common real-world logging
    bug: '"field":"[{"a":"b"}]"'), which desyncs quote-aware tracking and
    makes it stop short of the real closing bracket. The brace/bracket
    *symbols* themselves are still consistently balanced in that case even
    though the quoting around them is broken, so counting them blindly
    recovers the true outer span."""
    stack = [text[i]]
    j = i + 1
    n = len(text)
    while j < n and stack:
        cj = text[j]
        if cj in '{[':
            stack.append(cj)
        elif cj in '}]':
            if not stack:
                return None
            top = stack.pop()
            if (top == '{' and cj != '}') or (top == '[' and cj != ']'):
                return None
        j += 1
    return None if stack else j


def _is_meaningful_json(value):
    """Rejects trivial single-scalar arrays like [null] or [210] — very
    common in enterprise log formats as a generic "label:[value]" field
    wrapper (e.g. "timetaken:[210]", "login id:[null]") that has nothing
    to do with JSON, even though it happens to also be valid JSON grammar.
    A JSON object is always meaningful; a JSON array is too, unless it's
    exactly one bare scalar with nothing else going on."""
    if isinstance(value, list) and len(value) == 1 and not isinstance(value[0], (dict, list)):
        return False
    return True


def prettify_json_text(raw):
    """Best-effort pretty-printing for a matched JSON span, used when the
    user clicks on one. Strict parsing covers well-formed JSON directly;
    for a span that find_json_spans only accepted via its lenient fallback
    (a "field:[value]"-wrapper-mangled object — see find_json_spans'
    docstring), a small repair pass undoes the specific double-encoding
    pattern responsible (a nested array/object serialized as a string
    without escaping its own quotes) so the click still produces properly
    indented, readable output instead of just failing. Returns
    (pretty_text, was_valid) — was_valid is False only if neither strict
    parsing nor the repair pass could make sense of it, in which case
    pretty_text is just the original matched text unchanged.
    """
    try:
        parsed = json.loads(raw)
        return json.dumps(parsed, indent=2, ensure_ascii=False), True
    except (json.JSONDecodeError, ValueError):
        pass

    repaired = raw
    for _ in range(4):
        new_repaired = re.sub(r'":"(\[|\{)', r'":\1', repaired)
        new_repaired = re.sub(r'(\]|\})"(?=[,}])', r'\1', new_repaired)
        if new_repaired == repaired:
            break
        repaired = new_repaired
        try:
            parsed = json.loads(repaired)
            return json.dumps(parsed, indent=2, ensure_ascii=False), True
        except (json.JSONDecodeError, ValueError):
            continue

    return raw, False


def find_json_spans(text):
    """Finds embedded JSON object/array literals in a log line.

    Two-stage detection:
      1. Quote-aware bracket matching + strict json.loads() validation —
         handles well-formed JSON, including brace/bracket characters that
         appear inside properly-escaped string values.
      2. If that fails, a quote-agnostic bracket count + a lightweight
         "contains quoted-key: patterns" heuristic — catches real-world
         malformed JSON (double-encoded/unescaped nested JSON-as-a-string,
         as seen in some service logs) that will never pass strict
         validation but is still clearly JSON, not ordinary bracketed text.

    Stage 2's heuristic is what keeps ordinary bracketed log text — a
    thread name '[http-nio-0.0.0-8881-exec-15]', a trace ID
    '[dd0f65d3377a9f326f9446829baecfbd]', a class reference
    '[com.foo.Bar@76f8621]' — from being mistaken for JSON: none of those
    contain a quoted-key: pattern, so they're rejected by both stages.
    """
    spans = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c in '{[':
            accepted_end = None

            qa_end = _quote_aware_balanced_end(text, i)
            if qa_end is not None:
                try:
                    parsed = json.loads(text[i:qa_end])
                    if _is_meaningful_json(parsed):
                        accepted_end = qa_end
                except (json.JSONDecodeError, ValueError):
                    pass

            # The lenient fallback only applies to '{' (object) starts, not
            # '['. Enterprise log formats very often use '[' as a generic
            # "label:[value]" field wrapper that has nothing to do with
            # JSON — e.g. "values:[{...}], timetaken:[210] ms" — and that
            # wrapper is itself balanced-bracket-shaped and would contain
            # a real object's "key": patterns inside it, so the same
            # heuristic that correctly rescues a malformed {...} object
            # would also wrongly swallow the surrounding non-JSON [...]
            # wrapper if applied here too. Restricting to '{' means the
            # scan naturally continues past a wrapper like that and picks
            # out just the actual object inside it.
            #
            # It's also skipped when the very next character is itself an
            # opening bracket ('{{...}}' or '{[...]}' at the very start).
            # Genuine nested JSON always has a quoted key in between, e.g.
            # {"key":{...}} — back-to-back opening brackets with nothing
            # between them is a sign of a spurious extra wrapping layer
            # (e.g. a dropped map key in a toString()-style dump), and the
            # inner {...} is typically already valid JSON on its own; not
            # attempting the fallback here lets the scan reach that inner
            # object and pick it up cleanly via strict parsing instead of
            # swallowing the invalid outer layer along with it.
            if accepted_end is None and c == '{' and (i + 1 >= n or text[i + 1] not in '{['):
                dumb_end = _dumb_balanced_end(text, i)
                if dumb_end is not None and JSON_KV_RE.search(text[i:dumb_end]):
                    accepted_end = dumb_end

            if accepted_end is not None and accepted_end > i + 1:
                spans.append((i, accepted_end))
                i = accepted_end
                continue
        i += 1
    return spans


def extract_timestamp(text: str):
    m = ISO_RE.search(text)
    if m:
        s = m.group(1).replace(',', '.')
        s = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', s)
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    m = SLASH_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), '%m/%d/%Y %H:%M:%S')
        except ValueError:
            pass
    m = DASH_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), '%d-%m-%Y %H:%M:%S')
        except ValueError:
            pass
    return None


def extract_timestamp_fast(raw: bytes):
    """Same as extract_timestamp, but avoids decoding a whole long line when
    the timestamp (as is almost always the case) is near the start."""
    ts = extract_timestamp(raw[:96].decode('utf-8', 'replace'))
    if ts is None and len(raw) > 96:
        ts = extract_timestamp(raw.decode('utf-8', 'replace'))
    return ts


def parse_datetime_field(text: str):
    """Parse the From/To text fields. Empty string => no bound (returns None)."""
    text = text.strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None  # unparsable -> treated as no bound


def resource_path(name: str) -> str:
    """Resolve a bundled asset (e.g. favicon.ico) both when run from source
    and when frozen by PyInstaller/pynsist, whose bundles unpack alongside
    the executable rather than next to this .py file."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# Tiny (627-byte) multi-resolution .ico embedded directly in source, so the
# window/taskbar icon is never the OS's generic "python.exe" icon even if
# favicon.ico wasn't kept alongside this file — it's the same amber
# magnifying-glass mark used everywhere else in the app, just baked in as a
# guaranteed fallback rather than a bundled asset that could go missing.
_EMBEDDED_ICON_B64 = (
    "AAABAAEAEBAAAAAAIABdAgAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAQAAAAEAgGAAAAH/P/YQAA"
    "AiRJREFUeJylkztrVFEUhb99zr03d2bymARNkxAIJA6CJiFgJ9iYQkFbC4mVbUII2OQHGMTCwtZC"
    "SEBLQZgiipVVCMRHUEOEoCAWksnDzIM795xtMZm8U7ngNKdY7L2+tQWgZ3DkZprUHqtzF2lIOF0K"
    "INZ+DaL4wa+1D0XpuTByIynvvq4nNauqCogIGGl4eFVUDxwVVEQkjGIX5VpvSXdfYS2plgcAB1hj"
    "BO+VnaoDoD1jaf4dkgNslMl9D/B+AFVFxBojlGuO0Aq3r3QB8PbjFvXEk4vtYROLquL9gHT3Dvqk"
    "VhFjDZWa41JflmcTBbzzABhruP90lZWfFbKHTVSJ4qyaZmDeK4EV5icKFJdKjE4vMzq9THGpxPxE"
    "gcCeWANADIAxwk41ZWwoT5IqM3PrdLWFdLWFzMytU0+VsaE8O9UUa44CMmfgOiFjTidrmuO3ZwLe"
    "fNoiCoSH4/2U/iaU/taZHe8HgVeLG3TmQtyxNaS7d1CTWoWTIeo++xYDU8/XWVje5Fw+JHXaDPHA"
    "ABEOY7w+nEdEKC5ucO1ynpdTBe48+ca7lW06cgHe+X0Dn9Qqwl7zjhRJobM1YGO7zthIJwuzw9x7"
    "9IUX7//QkbXYKKPNCbRR4KOhCeD28JZ2U+5ePc/q7yqff5SJQ9GwJSsnqnwWBWuEzXJKS2CII3Gq"
    "jSqbMM5MhlHsgMYxaSOg4y91nnzWEoeoKjaMYhfGmUmB/zvnfx1EFLCRtPt4AAAAAElFTkSuQmCC"
)
_embedded_icon_cache = None


def app_icon() -> QIcon:
    """The window/taskbar icon: prefer a bundled favicon.ico next to the
    script/executable (lets a packager or the user swap in their own art),
    falling back to the icon embedded above so branding is never silently
    dropped just because that file wasn't carried along."""
    icon_path = resource_path("favicon.ico")
    if os.path.exists(icon_path):
        return QIcon(icon_path)

    global _embedded_icon_cache
    if _embedded_icon_cache is None:
        pixmap = QPixmap()
        pixmap.loadFromData(base64.b64decode(_EMBEDDED_ICON_B64), "ICO")
        _embedded_icon_cache = QIcon(pixmap)
    return _embedded_icon_cache


def human_size(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# A single loaded, memory-mapped file with a byte-offset line index
# ---------------------------------------------------------------------------
class LoadedFile:
    def __init__(self, path, color):
        self.path = path
        self.name = os.path.basename(path)
        self.color = color
        self.offsets = array('q')
        self.file_size = 0
        self._fh = None
        self._mm = None

    def set_index(self, offsets, file_size):
        self.offsets = offsets
        self.file_size = file_size

    def open_mmap(self):
        self._fh = open(self.path, 'rb')
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def total_lines(self):
        return len(self.offsets)

    def line_bytes(self, line_no):
        total = len(self.offsets)
        start = self.offsets[line_no]
        end = self.offsets[line_no + 1] if line_no + 1 < total else self.file_size
        return self._mm[start:end].rstrip(b'\r\n')

    def line_text(self, line_no):
        return self.line_bytes(line_no).decode('utf-8', 'replace')


# ---------------------------------------------------------------------------
# Index one file: byte offset of every line start
# ---------------------------------------------------------------------------
class IndexWorker(QThread):
    progress = Signal(int)          # percent 0-100
    finished_ok = Signal(object, int)   # offsets array, file_size
    failed = Signal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            file_size = os.path.getsize(self.path)
            offsets = array('q', [0])
            pos = 0
            last_percent = -1
            with open(self.path, 'rb') as f:
                while True:
                    if self._cancelled:
                        return
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    for m in re.finditer(rb'\n', chunk):
                        offsets.append(pos + m.start() + 1)
                    pos += len(chunk)
                    if file_size:
                        percent = int(pos * 100 / file_size)
                        if percent != last_percent:
                            last_percent = percent
                            self.progress.emit(percent)
            if offsets and offsets[-1] == file_size:
                offsets.pop()  # trailing newline -> no phantom empty last line
            self.finished_ok.emit(offsets, file_size)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Merge two or more already-indexed files into one timestamp-sorted timeline
# ---------------------------------------------------------------------------
class MergeSortWorker(QThread):
    progress = Signal(int)
    finished_ok = Signal(object, object, int)   # order_file array('i'), order_line array('i'), total_rows
    failed = Signal(str)

    def __init__(self, files):
        super().__init__()
        self.files = files
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            total_all = sum(f.total_lines() for f in self.files)
            merged = []
            done = 0
            last_percent = -1
            extract_fast = extract_timestamp_fast

            for file_idx, lf in enumerate(self.files):
                last_ts = None
                mm, offsets, fsize = lf._mm, lf.offsets, lf.file_size
                n = len(offsets)
                for line_no in range(n):
                    if (line_no & 0xFFF) == 0 and self._cancelled:
                        return
                    start = offsets[line_no]
                    end = offsets[line_no + 1] if line_no + 1 < n else fsize
                    raw = mm[start:end].rstrip(b'\r\n')
                    ts = extract_fast(raw)
                    if ts is not None:
                        last_ts = ts
                    sort_key = last_ts.timestamp() if last_ts is not None else float('-inf')
                    merged.append((sort_key, file_idx, line_no))
                    done += 1
                    if total_all and done % 100000 == 0:
                        percent = int(done * 100 / total_all)
                        if percent != last_percent:
                            last_percent = percent
                            self.progress.emit(percent)

            merged.sort(key=lambda t: t[0])  # stable: ties keep file-then-line order
            order_file = array('i', (m[1] for m in merged))
            order_line = array('i', (m[2] for m in merged))
            self.finished_ok.emit(order_file, order_line, len(merged))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Filtering: scans every row once, byte-level, returns matching row numbers
# ---------------------------------------------------------------------------
class FilterWorker(QThread):
    progress = Signal(int)
    finished_ok = Signal(object, int)   # matches array('i') of row indices, total_rows
    failed = Signal(str)

    def __init__(self, files, order_file, order_line, total_rows,
                 keyword_groups, case_sensitive, regex_mode, from_dt, to_dt, combine_mode):
        super().__init__()
        self.files = files
        self.order_file = order_file  # None => single-file direct mode (row == line_no in files[0])
        self.order_line = order_line
        self.total_rows = total_rows
        # list[{'mode': 'and'|'or', 'terms': list[str], 'subgroups': [{'mode','terms'}, ...]}]
        self.keyword_groups = keyword_groups
        self.case_sensitive = case_sensitive
        self.regex_mode = regex_mode
        self.from_dt = from_dt
        self.to_dt = to_dt
        self.combine_or = (combine_mode == 'or')
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        # This loop runs once per line of a potentially multi-gigabyte file,
        # so it avoids per-row method calls and repeated `self.x` attribute
        # lookups in favor of local variables — at tens of millions of
        # iterations that difference is the gap between ~45s and ~120s.
        try:
            # Three levels of boolean logic:
            #   outer (combine_or, between groups)
            #     -> group (its own AND/OR, between its direct terms + subgroup results)
            #          -> subgroup (its own AND/OR, between its own terms)
            # e.g. ((A OR B) AND C) OR D:
            #   group1 = mode AND, terms [C], subgroups [{mode: or, terms: [A, B]}]
            #   group2 = terms [D]
            #   combine_mode = or
            def compile_terms(terms, flags):
                fns = []
                for kw in terms:
                    pattern = kw if self.regex_mode else re.escape(kw)
                    fns.append(re.compile(pattern.encode('utf-8', 'surrogateescape'), flags).search)
                return fns

            compiled_groups = []
            if self.keyword_groups:
                flags = 0 if self.case_sensitive else re.IGNORECASE
                try:
                    for g in self.keyword_groups:
                        term_fns = compile_terms(g["terms"], flags)
                        sub_compiled = [
                            (compile_terms(sg["terms"], flags), sg["mode"] == 'or')
                            for sg in g["subgroups"]
                        ]
                        compiled_groups.append((term_fns, sub_compiled, g["mode"] == 'or'))
                except re.error as e:
                    self.failed.emit(f"Invalid regular expression: {e}")
                    return

            case_sensitive = self.case_sensitive
            combine_or = self.combine_or
            from_dt = self.from_dt
            to_dt = self.to_dt
            date_active = bool(from_dt or to_dt)
            extract_fast = extract_timestamp_fast

            order_file = self.order_file
            order_line = self.order_line
            total = self.total_rows
            single_file = order_file is None

            file_mms = [(lf._mm, lf.offsets, lf.file_size) for lf in self.files]

            def line_matches(raw):
                active = 0
                passed = 0
                for term_fns, sub_compiled, group_is_or in compiled_groups:
                    active += 1
                    child_results = [fn(raw) is not None for fn in term_fns]
                    for sub_fns, sub_is_or in sub_compiled:
                        sub_results = [fn(raw) is not None for fn in sub_fns]
                        child_results.append(any(sub_results) if sub_is_or else all(sub_results))
                    group_pass = any(child_results) if group_is_or else all(child_results)
                    if group_pass:
                        passed += 1
                if date_active:
                    active += 1
                    ts = extract_fast(raw)
                    ok = ts is not None
                    if ok and from_dt and ts < from_dt:
                        ok = False
                    if ok and to_dt and ts > to_dt:
                        ok = False
                    if ok:
                        passed += 1
                if active == 0:
                    return True
                return passed > 0 if combine_or else passed == active

            matches = array('i')
            last_percent = -1

            # Branching once outside the loop (rather than every row) matters
            # at tens of millions of iterations.
            if single_file:
                mm, offsets, fsize = file_mms[0]
                total_f = len(offsets)
                for row in range(total):
                    if (row & 0xFFF) == 0 and self._cancelled:
                        return
                    start = offsets[row]
                    end = offsets[row + 1] if row + 1 < total_f else fsize
                    raw = mm[start:end].rstrip(b'\r\n')
                    if line_matches(raw):
                        matches.append(row)
                    if total and row % 100000 == 0:
                        percent = int(row * 100 / total)
                        if percent != last_percent:
                            last_percent = percent
                            self.progress.emit(percent)
            else:
                for row in range(total):
                    if (row & 0xFFF) == 0 and self._cancelled:
                        return
                    file_idx = order_file[row]
                    line_no = order_line[row]
                    mm, offsets, fsize = file_mms[file_idx]
                    total_f = len(offsets)
                    start = offsets[line_no]
                    end = offsets[line_no + 1] if line_no + 1 < total_f else fsize
                    raw = mm[start:end].rstrip(b'\r\n')
                    if line_matches(raw):
                        matches.append(row)
                    if total and row % 100000 == 0:
                        percent = int(row * 100 / total)
                        if percent != last_percent:
                            last_percent = percent
                            self.progress.emit(percent)

            self.finished_ok.emit(matches, total)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ExportWorker(QThread):
    progress = Signal(int)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self, files, order_file, order_line, total_rows, row_numbers, multi, out_path):
        super().__init__()
        self.files = files
        self.order_file = order_file
        self.order_line = order_line
        self.total_rows = total_rows
        self.row_numbers = row_numbers  # None -> export every row
        self.multi = multi
        self.out_path = out_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            iterable = self.row_numbers if self.row_numbers is not None else range(self.total_rows)
            n = len(iterable)
            count = 0
            last_percent = -1
            with open(self.out_path, 'wb') as out:
                for idx, row in enumerate(iterable):
                    if self._cancelled:
                        return
                    if self.order_file is not None:
                        file_idx = self.order_file[row]
                        line_no = self.order_line[row]
                    else:
                        file_idx = 0
                        line_no = row
                    lf = self.files[file_idx]
                    raw = lf.line_bytes(line_no)
                    if self.multi:
                        out.write(b'[')
                        out.write(lf.name.encode('utf-8', 'replace'))
                        out.write(b'] ')
                    out.write(raw)
                    out.write(b'\r\n')
                    count += 1
                    if n and idx % 50000 == 0:
                        percent = int(idx * 100 / n)
                        if percent != last_percent:
                            last_percent = percent
                            self.progress.emit(percent)
            self.finished_ok.emit(count)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Table model — virtualized: only reads the lines Qt actually asks to paint
# ---------------------------------------------------------------------------
class LogTableModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.files = []          # list[LoadedFile], in load order
        self.order_file = None   # array('i') or None (single-file / no-files direct mode)
        self.order_line = None
        self.total_rows = 0
        self.filtered = None     # array('i') of row indices, or None (show everything)

        self.kw_re_str = None

    @property
    def multi(self):
        return len(self.files) > 1

    def set_files(self, files, order_file, order_line, total_rows):
        self.beginResetModel()
        self.files = files
        self.order_file = order_file
        self.order_line = order_line
        self.total_rows = total_rows
        self.filtered = None
        self.endResetModel()

    def set_filtered(self, filtered):
        self.beginResetModel()
        self.filtered = filtered
        self.endResetModel()

    def set_highlight(self, kw_re_str, case_sensitive):
        self.kw_re_str = kw_re_str

    def close_all(self):
        for f in self.files:
            f.close()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self.filtered) if self.filtered is not None else self.total_rows

    def columnCount(self, parent=QModelIndex()):
        return 3  # source badge, line number, text

    def entry_for_row(self, row):
        actual = self.filtered[row] if self.filtered is not None else row
        if self.order_file is not None:
            return self.order_file[actual], self.order_line[actual]
        return 0, actual

    def line_text(self, file_idx, line_no):
        if not self.files or file_idx < 0 or file_idx >= len(self.files):
            return ""
        return self.files[file_idx].line_text(line_no)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not self.files:
            return None
        col = index.column()
        file_idx, line_no = self.entry_for_row(index.row())
        if role == Qt.DisplayRole:
            if col == 0:
                return self.files[file_idx].name
            if col == 1:
                return str(line_no + 1)
            return self.line_text(file_idx, line_no)
        if col == 1:
            if role == Qt.ForegroundRole:
                return QColor(TEXT_DIM)
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return ("Source", "Line", "Text")[section]

    # -- Rendering (level-word / method-name coloring + keyword/trace marks) --
    def _bg_spans(self, text):
        """Background highlight spans: keyword matches (trace/request IDs
        are just another keyword search term now, not a separate concept)."""
        if not self.kw_re_str:
            return []
        spans = []
        try:
            for m in self.kw_re_str.finditer(text):
                if m.end() > m.start():
                    spans.append((m.start(), m.end(), 'kw'))
        except re.error:
            pass
        return spans

    def _fg_spans(self, text):
        """Foreground color spans: the log-level word, any method:[name],
        and any embedded JSON object/array."""
        spans = []
        window = text[:LEVEL_SCAN_WINDOW]
        for _level, color, pattern in LEVEL_PATTERNS:
            m = pattern.search(window)
            if m:
                spans.append((m.start(), m.end(), color))
                break
        for m in METHOD_PATTERN.finditer(text):
            spans.append((m.start(2), m.end(2), ACCENT_2))
        for s, e in find_json_spans(text):
            spans.append((s, e, JSON_COLOR))
        spans.sort(key=lambda s: s[0])
        return spans

    def compute_render_runs(self, text):
        """Merge background marks and foreground colors into paint-ready runs
        of (start, end, bg_cls, fg_color)."""
        n = len(text)
        bg_spans = self._bg_spans(text)
        fg_spans = self._fg_spans(text)
        if not bg_spans and not fg_spans:
            return [(0, n, None, None)]

        points = {0, n}
        for s, e, _ in bg_spans:
            points.add(s)
            points.add(e)
        for s, e, _ in fg_spans:
            points.add(s)
            points.add(e)
        points = sorted(p for p in points if 0 <= p <= n)

        runs = []
        for i in range(len(points) - 1):
            s, e = points[i], points[i + 1]
            if s >= e:
                continue
            bg_cls = None
            for bs, be, cls in bg_spans:
                if bs <= s and e <= be:
                    bg_cls = cls
                    break
            fg_color = None
            for fs, fe, color in fg_spans:
                if fs <= s and e <= fe:
                    fg_color = color
                    break
            runs.append((s, e, bg_cls, fg_color))
        return runs


def draw_row_divider(painter: QPainter, rect: QRect):
    """A subtle line under each row, matching the web version's
    `border-bottom:1px solid rgba(38,49,64,0.4)` on every <td> — without it,
    rows have no visual separator and reading down a column of wrapped text
    blurs together."""
    painter.save()
    pen = QPen(ROW_DIVIDER)
    pen.setWidth(1)
    painter.setPen(pen)
    y = rect.bottom()
    painter.drawLine(rect.left(), y, rect.right(), y)
    painter.restore()


class LogLineDelegate(QStyledItemDelegate):
    """Paints column 2 (the log text) with level/method coloring and
    keyword/trace highlight marks. Two modes, toggled by `wrap_enabled`:
    single-line with horizontal scroll (fixed row height — cheap at any
    row count), or wrapped like the original web version (variable row
    height — matches the source exactly but costs more at huge row counts,
    which is why it's a toggle rather than the only option)."""

    PAD_X = 10
    PAD_Y = 9

    def __init__(self, model: LogTableModel, parent=None):
        super().__init__(parent)
        self.model_ref = model
        self.wrap_enabled = True
        self.table_ref = None  # set by the window after the table view exists;
        # lets paint() ask "is any character range in this row selected?"
        # Qt gives sizeHint() an option.rect with width 0 (there's a genuine
        # circular dependency: our row height depends on column width, and
        # the stretched column's width depends on whether a vertical
        # scrollbar is needed, which depends on total row height). So the
        # wrap width is tracked here independently and kept in sync by the
        # window on layout/resize, rather than trusted from option.rect.
        self.wrap_width = 600

    def _build_layout(self, text, width, font):
        """Lay out `text` wrapped to `width` px, with per-run coloring
        applied via QTextLayout's native rich-formatting support (handles
        wrapping and highlight spans together correctly, including spans
        that straddle a wrap point)."""
        layout = QTextLayout(text, font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)

        formats = []
        for s, e, bg_cls, fg_color in self.model_ref.compute_render_runs(text):
            if e <= s:
                continue
            fmt = QTextCharFormat()
            if bg_cls == 'kw':
                fmt.setBackground(KW_MARK_BG)
                fmt.setForeground(KW_MARK_TEXT)
            else:
                fmt.setForeground(QColor(TEXT))
            if fg_color:
                fmt.setForeground(QColor(fg_color))
                bold = QFont(font)
                bold.setBold(True)
                fmt.setFont(bold)
            fr = QTextLayout.FormatRange()
            fr.start = s
            fr.length = e - s
            fr.format = fmt
            formats.append(fr)
        layout.setFormats(formats)

        # Uniform per-line spacing derived from the font, scaled by
        # LINE_HEIGHT_FACTOR — matches the web version's `line-height:1.6`
        # (QTextLine.height() alone gives the font's natural ~1.15x
        # leading, which reads as noticeably tighter).
        line_spacing = QFontMetricsF(font).height() * LINE_HEIGHT_FACTOR

        layout.beginLayout()
        n_lines = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0, n_lines * line_spacing))
            n_lines += 1
        layout.endLayout()
        return layout, n_lines * line_spacing, line_spacing

    def _selection_range_for(self, row, text_len):
        """(start, end) char offsets selected in this row by the table's
        cursor-drag text selection, or None. `end` is already clamped to
        text_len (a row fully inside a multi-row selection gets (0, text_len))."""
        if self.table_ref is None:
            return None
        rng = self.table_ref.get_text_selection_range_for_row(row)
        if rng is None:
            return None
        s, e = rng
        return (max(0, s), text_len if e is None else min(text_len, e))

    def paint(self, painter: QPainter, option, index):
        if index.column() != 2:
            super().paint(painter, option, index)
            return

        text = index.data(Qt.DisplayRole) or ""
        painter.save()
        painter.setClipRect(option.rect)

        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        sel_range = self._selection_range_for(index.row(), len(text))

        if self.wrap_enabled:
            # Use the tracked width, not option.rect.width(), so wrapping is
            # identical to what sizeHint() reserved room for (see __init__).
            width = self.wrap_width - 2 * self.PAD_X
            layout, _total_h, line_spacing = self._build_layout(text, width, option.font)
            origin = QPointF(option.rect.x() + self.PAD_X, option.rect.y() + self.PAD_Y)
            if sel_range and sel_range[1] > sel_range[0]:
                s, e = sel_range
                painter.setPen(Qt.NoPen)
                for li in range(layout.lineCount()):
                    line = layout.lineAt(li)
                    ls = line.textStart()
                    le = ls + line.textLength()
                    ss, ee = max(s, ls), min(e, le)
                    if ee > ss:
                        x1, _ = line.cursorToX(ss)
                        x2, _ = line.cursorToX(ee)
                        rect = QRectF(
                            origin.x() + min(x1, x2), origin.y() + line.position().y(),
                            abs(x2 - x1), line_spacing
                        )
                        painter.fillRect(rect, TEXT_SEL_BG)
            painter.setPen(QColor(TEXT))
            layout.draw(painter, origin)
            draw_row_divider(painter, option.rect)
            painter.restore()
            return

        fm = option.fontMetrics
        pad = self.PAD_X
        x = option.rect.x() + pad
        y = option.rect.y()
        h = option.rect.height()
        max_x = option.rect.right()

        if sel_range and sel_range[1] > sel_range[0]:
            s, e = sel_range
            x1 = x + fm.horizontalAdvance(text[:s])
            x2 = x + fm.horizontalAdvance(text[:e])
            painter.fillRect(QRect(x1, y, max(0, min(x2, max_x) - x1), h), TEXT_SEL_BG)

        base_font = option.font
        bold_font = QFont(base_font)
        bold_font.setBold(True)

        for s, e, bg_cls, fg_color in self.model_ref.compute_render_runs(text):
            if x >= max_x:
                break
            seg = text[s:e]
            if not seg:
                continue
            w = fm.horizontalAdvance(seg)

            if bg_cls == 'kw':
                painter.fillRect(QRect(x, y, min(w, max_x - x), h), KW_MARK_BG)
                default_color = KW_MARK_TEXT
            else:
                default_color = QColor(TEXT)

            if fg_color:
                painter.setFont(bold_font)
                painter.setPen(QColor(fg_color))
            else:
                painter.setFont(base_font)
                painter.setPen(default_color)

            painter.drawText(QRect(x, y, max(0, max_x - x), h), Qt.AlignVCenter | Qt.TextSingleLine, seg)
            x += w

        draw_row_divider(painter, option.rect)
        painter.restore()

    def sizeHint(self, option, index):
        if not self.wrap_enabled or index.column() != 2:
            return QSize(option.rect.width(), ROW_HEIGHT)
        # Qt calls sizeHint() with option.rect.width() == 0 for this column
        # (see the note in __init__), so use the independently tracked width.
        text = index.data(Qt.DisplayRole) or ""
        width = self.wrap_width - 2 * self.PAD_X
        _layout, total_h, _line_spacing = self._build_layout(text, width, option.font)
        return QSize(self.wrap_width, max(ROW_HEIGHT, int(total_h) + 2 * self.PAD_Y))


class SourceBadgeDelegate(QStyledItemDelegate):
    """Paints column 0 as a small rounded, colored chip with the source file name."""

    def __init__(self, model: LogTableModel, parent=None):
        super().__init__(parent)
        self.model_ref = model

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setClipRect(option.rect)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        file_idx, _ = self.model_ref.entry_for_row(index.row())
        lf = self.model_ref.files[file_idx] if self.model_ref.files else None
        if lf is None:
            draw_row_divider(painter, option.rect)
            painter.restore()
            return

        text = lf.name
        color = QColor(lf.color)
        bg = QColor(color)
        bg.setAlpha(34)

        fm = option.fontMetrics
        pad_h = 6
        text_w = fm.horizontalAdvance(text)
        badge_w = max(0, min(text_w + pad_h * 2, option.rect.width() - 4))
        badge_h = min(fm.height() + 4, option.rect.height() - 4)
        bx = option.rect.x() + 2
        by = option.rect.y() + (option.rect.height() - badge_h) // 2
        rect = QRect(bx, by, badge_w, badge_h)

        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(color)
        painter.drawText(rect.adjusted(pad_h, 0, -pad_h, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        draw_row_divider(painter, option.rect)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)


class LineNoDelegate(QStyledItemDelegate):
    """Column 1 (the line number) otherwise just uses default rendering —
    this only exists to add the same row divider the other two columns
    draw, so the line is continuous across the full row width."""

    def paint(self, painter: QPainter, option, index):
        super().paint(painter, option, index)
        draw_row_divider(painter, option.rect)


class LogTableView(QTableView):
    """QTableView with an added cursor-drag text-selection mode for column 2
    (the log text), on top of its normal row selection for columns 0/1.

    The text in column 2 is custom-painted by LogLineDelegate (needed for
    wrapping + highlight marks at file-scale), so Qt's native selection
    machinery has no idea individual characters exist there — clicking and
    dragging only ever selected whole rows. This adds real character-level
    hit-testing (via the same QTextLayout the delegate paints with, so
    coordinates always agree) plus an anchor/end selection range that spans
    rows, mirroring how text selection works in a normal text editor or in
    the browser version of this tool.
    """

    json_clicked = Signal(str)  # emitted with the exact matched JSON span text

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_delegate = None
        self.text_sel_anchor = None  # (row, char_offset) or None
        self.text_sel_end = None
        self._dragging_text = False
        self.setMouseTracking(False)

    def set_line_delegate(self, delegate):
        self.line_delegate = delegate

    def _json_span_at(self, row, offset):
        """The matched JSON text at (row, char_offset), or None — used to
        tell a plain click on a highlighted JSON span apart from a click
        anywhere else in the text column."""
        model = self.model()
        if model is None or row < 0 or row >= model.rowCount():
            return None
        text = model.index(row, 2).data(Qt.DisplayRole) or ""
        for s, e in find_json_spans(text):
            if s <= offset < e:
                return text[s:e]
        return None

    # -- Hit testing --------------------------------------------------
    def _hit_test(self, pos: QPoint):
        """Map a viewport pixel position to (row, char_offset) in column 2's
        text, or None if pos isn't over that column."""
        index = self.indexAt(pos)
        if not index.isValid() or index.column() != 2 or self.line_delegate is None:
            return None

        rect = self.visualRect(index)
        text = index.data(Qt.DisplayRole) or ""
        delegate = self.line_delegate
        font = self.font()
        local_x = pos.x() - rect.x() - delegate.PAD_X
        local_y = pos.y() - rect.y() - delegate.PAD_Y

        width = (delegate.wrap_width - 2 * delegate.PAD_X) if delegate.wrap_enabled else 10_000_000
        layout, _total_h, line_spacing = delegate._build_layout(text, max(1, width), font)

        if layout.lineCount() == 0:
            return (index.row(), 0)

        line_idx = 0
        if line_spacing > 0:
            line_idx = max(0, min(layout.lineCount() - 1, int(local_y // line_spacing)))
        line = layout.lineAt(line_idx)
        offset = line.xToCursor(local_x, QTextLine.CursorBetweenCharacters)
        return (index.row(), offset)

    def _row_edge_hit(self, pos: QPoint):
        """Fallback for drag-selecting past the top/bottom of the loaded
        text column (e.g. dragging into the header, or below the last row):
        clamps to the nearest row's start or end offset."""
        row = self.rowAt(pos.y())
        if row == -1:
            row = self.model().rowCount() - 1 if pos.y() > 0 else 0
        if row < 0:
            return None
        idx = self.model().index(row, 2)
        text = idx.data(Qt.DisplayRole) or ""
        rect = self.visualRect(idx)
        offset = 0 if pos.y() < rect.y() else len(text)
        return (row, offset)

    # -- Mouse events for cursor-drag text selection -------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            hit = self._hit_test(pos)
            if hit is not None:
                self.clearSelection()
                self.text_sel_anchor = hit
                self.text_sel_end = hit
                self._dragging_text = True
                self.viewport().update()
                event.accept()
                return
            elif self.text_sel_anchor is not None:
                self.text_sel_anchor = None
                self.text_sel_end = None
                self.viewport().update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging_text:
            pos = event.position().toPoint()
            hit = self._hit_test(pos) or self._row_edge_hit(pos)
            if hit is not None:
                self.text_sel_end = hit
                self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging_text:
            self._dragging_text = False
            # anchor == end means the mouse never moved to a different
            # character during the press — a plain click, not a
            # click-drag selection. Only then does landing on a JSON span
            # open the pretty-print dialog, so dragging to select JSON
            # text (e.g. to copy it) still works exactly as before.
            if self.text_sel_anchor is not None and self.text_sel_anchor == self.text_sel_end:
                row, offset = self.text_sel_anchor
                span_text = self._json_span_at(row, offset)
                if span_text:
                    self.json_clicked.emit(span_text)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- Selection state, queried by the delegate and by copy actions --
    def clear_text_selection(self):
        if self.text_sel_anchor is not None or self.text_sel_end is not None:
            self.text_sel_anchor = None
            self.text_sel_end = None
            self.viewport().update()

    def has_text_selection(self):
        return (
            self.text_sel_anchor is not None
            and self.text_sel_end is not None
            and self.text_sel_anchor != self.text_sel_end
        )

    def _ordered_selection(self):
        (ar, ao), (er, eo) = self.text_sel_anchor, self.text_sel_end
        if (ar, ao) <= (er, eo):
            return ar, ao, er, eo
        return er, eo, ar, ao

    def get_text_selection_range_for_row(self, row):
        """(start_offset, end_offset_or_None) selected within `row`'s text,
        or None if that row isn't part of the current selection. A `None`
        end means "to the end of the line" (the caller knows the length)."""
        if not self.has_text_selection():
            return None
        start_row, start_off, end_row, end_off = self._ordered_selection()
        if row < start_row or row > end_row:
            return None
        if start_row == end_row:
            return (min(start_off, end_off), max(start_off, end_off))
        if row == start_row:
            return (start_off, None)
        if row == end_row:
            return (0, end_off)
        return (0, None)

    def get_selected_text(self):
        if not self.has_text_selection():
            return ""
        start_row, start_off, end_row, end_off = self._ordered_selection()
        model = self.model()
        parts = []
        for row in range(start_row, end_row + 1):
            text = model.index(row, 2).data(Qt.DisplayRole) or ""
            if row == start_row and row == end_row:
                parts.append(text[start_off:end_off])
            elif row == start_row:
                parts.append(text[start_off:])
            elif row == end_row:
                parts.append(text[:end_off])
            else:
                parts.append(text)
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class LogLensWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Log Lens v{VERSION}")
        self.resize(1320, 820)
        self.setAcceptDrops(True)
        self.setWindowIcon(app_icon())

        self.model = LogTableModel()
        self.files = []  # list[LoadedFile], authoritative load order

        self._pending_paths = []
        self.index_worker = None
        self.merge_worker = None
        self.filter_worker = None
        self.export_worker = None

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(350)
        self.debounce.timeout.connect(self.run_filter)

        # Wrapped rows need re-flowing when the (stretched) text column's
        # width changes, e.g. on window resize — debounced so dragging the
        # window edge doesn't re-lay-out every intermediate frame.
        self.wrap_resize_debounce = QTimer(self)
        self.wrap_resize_debounce.setSingleShot(True)
        self.wrap_resize_debounce.setInterval(150)
        self.wrap_resize_debounce.timeout.connect(self._relayout_rows)

        self._build_ui()

    # -- UI construction ---------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_statusbar())

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.stack.addWidget(self._build_empty_state())
        self.stack.addWidget(self._build_table())
        self.stack.setCurrentIndex(0)

    def _build_header(self):
        header = QFrame()
        header.setObjectName("header")
        v = QVBoxLayout(header)
        v.setContentsMargins(18, 14, 18, 12)
        v.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        dot = QLabel()
        dot.setObjectName("dot")
        dot.setFixedSize(8, 8)
        title = QLabel("Log Lens")
        title.setObjectName("titleLabel")
        version_label = QLabel(f"v{VERSION}")
        version_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:10px;")
        self.filename_label = QLabel("")
        self.filename_label.setObjectName("filenameLabel")
        self.filename_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        title_row.addWidget(dot)
        title_row.addWidget(title)
        title_row.addWidget(version_label)
        title_row.addStretch(1)
        title_row.addWidget(self.filename_label)
        v.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.open_btn = QPushButton("Open Logs")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self.open_file_dialog)
        toolbar.addLayout(self._field("", self.open_btn))

        self.remove_all_btn = QPushButton("Remove Files")
        self.remove_all_btn.setEnabled(False)
        self.remove_all_btn.clicked.connect(self.remove_all_files)
        toolbar.addLayout(self._field("", self.remove_all_btn))

        self.keyword_groups = []  # list of dicts: {frame, body_layout, terms, subgroups, combo, remove_btn}
        self.keyword_groups_container = QWidget()
        self.keyword_groups_layout = QHBoxLayout(self.keyword_groups_container)
        self.keyword_groups_layout.setContentsMargins(0, 0, 0, 0)
        self.keyword_groups_layout.setSpacing(6)
        # Groups pack left-to-right; this trailing stretch keeps them from
        # being stretched to fill extra width, and new groups are inserted
        # before it (see _add_keyword_group) so it always stays last.
        self.keyword_groups_layout.addStretch(1)

        # Side-by-side groups mean the only dimension that can grow with a
        # complex expression is width, not height — so this scrolls
        # horizontally within a fixed height instead of vertically. A single
        # very tall group (many terms/subgroups stacked inside it) still
        # gets a vertical scrollbar as a fallback.
        kw_scroll = QScrollArea()
        kw_scroll.setWidget(self.keyword_groups_container)
        kw_scroll.setWidgetResizable(True)
        kw_scroll.setFixedHeight(150)
        kw_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        kw_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        kw_scroll.setFrameShape(QFrame.NoFrame)
        kw_scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }}")

        self.keyword_field_widget = QWidget()
        kw_field_layout = QVBoxLayout(self.keyword_field_widget)
        kw_field_layout.setContentsMargins(0, 0, 0, 0)
        kw_field_layout.setSpacing(4)
        kw_field_layout.addWidget(kw_scroll)

        add_group_btn = QPushButton("+ Group")
        add_group_btn.setToolTip(
            "Add another group, e.g. (text1 OR text2) AND text3 — set the first "
            "group to OR, add a second group with text3, then set \"Combine "
            "filters\" below to Match ALL (AND)."
        )
        add_group_btn.clicked.connect(lambda: self._add_keyword_group())
        kw_field_layout.addWidget(add_group_btn, alignment=Qt.AlignLeft)

        self._add_keyword_group()

        self.from_input = QLineEdit()
        self.from_input.setPlaceholderText("YYYY-MM-DD HH:MM:SS")
        self.from_input.setMinimumWidth(150)
        toolbar.addLayout(self._field("From", self.from_input))

        self.to_input = QLineEdit()
        self.to_input.setPlaceholderText("YYYY-MM-DD HH:MM:SS")
        self.to_input.setMinimumWidth(150)
        toolbar.addLayout(self._field("To", self.to_input))

        self.case_checkbox = QCheckBox("case sensitive")
        self.regex_checkbox = QCheckBox("regex")
        self.wrap_checkbox = QCheckBox("wrap text")
        self.wrap_checkbox.setChecked(True)
        self.wrap_checkbox.setToolTip(
            "Wrap long lines like the web version. Turn off for very large files "
            "if scrolling gets sluggish — fixed-height rows scroll faster."
        )
        checkbox_row = QWidget()
        checkbox_row_layout = QHBoxLayout(checkbox_row)
        checkbox_row_layout.setContentsMargins(0, 0, 0, 0)
        checkbox_row_layout.setSpacing(12)
        checkbox_row_layout.addWidget(self.case_checkbox)
        checkbox_row_layout.addWidget(self.regex_checkbox)
        checkbox_row_layout.addWidget(self.wrap_checkbox)
        toolbar.addLayout(self._field("", checkbox_row))

        self.clear_btn = QPushButton("Clear filters")
        self.clear_btn.clicked.connect(self.clear_filters)
        toolbar.addLayout(self._field("", self.clear_btn))

        self.export_btn = QPushButton("Export matches")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_matches)
        toolbar.addLayout(self._field("", self.export_btn))

        toolbar.addStretch(1)
        for i in range(toolbar.count()):
            item = toolbar.itemAt(i)
            if item.layout() is not None:
                toolbar.setAlignment(item.layout(), Qt.AlignTop)
        v.addLayout(toolbar)

        # Row 2: the keyword/text/trace search groups get their own row so
        # they never fight the rest of the toolbar for space — and a
        # minimize toggle lets it be collapsed down to just this header
        # when you're not actively building a search expression.
        kw_row = QVBoxLayout()
        kw_row.setSpacing(4)

        kw_header = QHBoxLayout()
        kw_header.setContentsMargins(0, 0, 0, 0)
        kw_header.setSpacing(6)
        kw_label = QLabel("Keyword / Text / Trace / Request Id")
        kw_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:10px;")
        kw_header.addWidget(kw_label)
        self.kw_minimize_btn = QPushButton("▾ Minimize")
        self.kw_minimize_btn.setStyleSheet("QPushButton { padding: 3px 8px; font-size: 11px; }")
        self.kw_minimize_btn.setToolTip("Collapse/expand the keyword search row")
        self.kw_minimize_btn.clicked.connect(self._toggle_keyword_row)
        kw_header.addWidget(self.kw_minimize_btn)
        kw_header.addSpacing(12)
        combine_label = QLabel("Combine filters")
        combine_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:10px;")
        kw_header.addWidget(combine_label)
        self.combine_combo = QComboBox()
        self.combine_combo.addItem("Match ALL (AND)", "and")
        self.combine_combo.addItem("Match ANY (OR)", "or")
        kw_header.addWidget(self.combine_combo)
        kw_header.addStretch(1)
        kw_row.addLayout(kw_header)
        kw_row.addWidget(self.keyword_field_widget)
        v.addLayout(kw_row)

        v.addWidget(self._build_level_legend())
        self.source_legend_row = QWidget()
        self.source_legend_layout = QHBoxLayout(self.source_legend_row)
        self.source_legend_layout.setContentsMargins(0, 0, 0, 0)
        self.source_legend_layout.setSpacing(16)
        v.addWidget(self.source_legend_row)

        for w in (self.from_input, self.to_input):
            w.textChanged.connect(lambda _=None: self.debounce.start())
        self.case_checkbox.toggled.connect(lambda _=None: self.debounce.start())
        self.regex_checkbox.toggled.connect(lambda _=None: self.debounce.start())
        self.combine_combo.currentIndexChanged.connect(lambda _=None: self.debounce.start())
        self.wrap_checkbox.toggled.connect(self.set_wrap_enabled)

        return header

    def _field(self, label_text, widget):
        """Wrap `widget` in a labeled column for the toolbar. Every toolbar
        item goes through this — including buttons and checkboxes, with an
        empty label — so every item shares the same two-row (label + control)
        shape and lines up on one baseline instead of the shorter widgets
        drifting to Qt's default vertical-centering, which visually collides
        with its taller neighbors."""
        col = QVBoxLayout()
        col.setSpacing(3)
        label = QLabel(label_text if label_text else " ")
        color = TEXT_DIM if label_text else "transparent"
        label.setStyleSheet(f"color:{color}; font-size:10px; padding-left:2px;")
        col.addWidget(label)
        col.addWidget(widget)
        return col

    def _toggle_keyword_row(self):
        now_visible = not self.keyword_field_widget.isVisible()
        self.keyword_field_widget.setVisible(now_visible)
        self.kw_minimize_btn.setText("▾ Minimize" if now_visible else "▸ Expand")

    def _add_keyword_group(self):
        """Adds a new top-level keyword group — a bordered box whose own
        AND/OR mode combines everything directly inside it: plain terms,
        AND/OR one nested subgroup for one extra level of parenthesization.
        Top-level groups themselves are combined by the "Combine filters"
        dropdown. Together this gives three levels of boolean logic — e.g.
        to build ((A OR B) AND C) OR D:
          - Group 1: mode AND, containing a term C plus a subgroup [A, B]
            set to OR  →  (A OR B) AND C
          - Group 2: containing a term D
          - "Combine filters" set to Match ANY (OR)  →  (…) OR D
        """
        frame = QFrame()
        frame.setObjectName("kwGroup")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        combo = QComboBox()
        combo.addItem("Match ANY here (OR)", "or")
        combo.addItem("Match ALL here (AND)", "and")
        combo.currentIndexChanged.connect(lambda _=None: self.debounce.start())
        header.addWidget(combo)
        header.addStretch(1)
        remove_group_btn = QPushButton("×")
        remove_group_btn.setFixedSize(20, 20)
        remove_group_btn.setStyleSheet("QPushButton { padding: 0px; font-size: 13px; }")
        remove_group_btn.setToolTip("Remove this whole group")
        header.addWidget(remove_group_btn)
        outer.addLayout(header)

        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addStretch(1)
        outer.addLayout(body_layout)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(4)
        add_term_btn = QPushButton("+ Term")
        add_term_btn.setToolTip("Add another term directly in this group")
        footer.addWidget(add_term_btn)
        add_sub_btn = QPushButton("+ Subgroup")
        add_sub_btn.setToolTip("Add a nested (…) subgroup inside this group")
        footer.addWidget(add_sub_btn)
        footer.addStretch(1)
        outer.addLayout(footer)

        group = {"frame": frame, "body_layout": body_layout, "combo": combo,
                 "terms": [], "subgroups": [], "remove_btn": remove_group_btn}
        remove_group_btn.clicked.connect(lambda: self._remove_keyword_group(group))
        add_term_btn.clicked.connect(lambda: self._add_term(group))
        add_sub_btn.clicked.connect(lambda: self._add_subgroup(group))

        self.keyword_groups.append(group)
        self._add_term(group)  # every group starts with one usable term

        self.keyword_groups_layout.insertWidget(self.keyword_groups_layout.count() - 1, frame)
        self._update_group_remove_buttons()
        return group

    def _remove_keyword_group(self, group):
        if len(self.keyword_groups) <= 1:
            return  # always keep at least one top-level group
        had_text = self._group_has_text(group)
        self.keyword_groups.remove(group)
        group["frame"].setParent(None)
        group["frame"].deleteLater()
        self._update_group_remove_buttons()
        if had_text:
            self.debounce.start()

    def _update_group_remove_buttons(self):
        # Only allow removing a top-level group down to a minimum of one.
        only_one = len(self.keyword_groups) == 1
        for g in self.keyword_groups:
            g["remove_btn"].setVisible(not only_one)

    def _group_has_text(self, group):
        if any(e.text().strip() for e in group["terms"]):
            return True
        return any(e.text().strip() for sg in group["subgroups"] for e in sg["terms"])

    # -- Plain terms, directly inside a group or inside a subgroup --------
    def _add_term(self, container, text=""):
        """Adds one term row inside `container`, which is either a
        top-level group dict or a subgroup dict — both have "terms" (list)
        and "body_layout" (where the row is inserted)."""
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        edit = QLineEdit()
        edit.setPlaceholderText("e.g. exception, failed" if not container["terms"] else "another term…")
        edit.setMinimumWidth(160)
        edit.setText(text)
        edit.textChanged.connect(lambda _=None: self.debounce.start())
        row_layout.addWidget(edit)

        remove_btn = QPushButton("-")
        remove_btn.setFixedSize(26, 26)
        remove_btn.setStyleSheet("QPushButton { padding: 0px; font-size: 15px; font-weight: 700; }")
        remove_btn.setToolTip("Remove this term")
        remove_btn.clicked.connect(lambda: self._remove_term(container, row_widget, edit))
        row_layout.addWidget(remove_btn)

        container["body_layout"].insertWidget(container["body_layout"].count() - 1, row_widget)
        container["terms"].append(edit)
        return edit

    def _remove_term(self, container, row_widget, edit):
        had_text = bool(edit.text().strip())
        container["terms"].remove(edit)
        row_widget.setParent(None)
        row_widget.deleteLater()
        if had_text:
            self.debounce.start()

    # -- Subgroups: one extra level of (…) nesting inside a group ---------
    def _add_subgroup(self, group):
        frame = QFrame()
        frame.setObjectName("kwSubgroup")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        combo = QComboBox()
        combo.addItem("Match ANY here (OR)", "or")
        combo.addItem("Match ALL here (AND)", "and")
        combo.currentIndexChanged.connect(lambda _=None: self.debounce.start())
        header.addWidget(combo)
        header.addStretch(1)
        remove_btn = QPushButton("×")
        remove_btn.setFixedSize(20, 20)
        remove_btn.setStyleSheet("QPushButton { padding: 0px; font-size: 13px; }")
        remove_btn.setToolTip("Remove this subgroup")
        header.addWidget(remove_btn)
        outer.addLayout(header)

        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addStretch(1)
        outer.addLayout(body_layout)

        add_term_btn = QPushButton("+ Term")
        add_term_btn.setToolTip("Add another term in this subgroup")
        term_footer = QHBoxLayout()
        term_footer.setContentsMargins(0, 0, 0, 0)
        term_footer.addWidget(add_term_btn)
        term_footer.addStretch(1)
        outer.addLayout(term_footer)

        subgroup = {"frame": frame, "body_layout": body_layout, "combo": combo, "terms": []}
        remove_btn.clicked.connect(lambda: self._remove_subgroup(group, subgroup))
        add_term_btn.clicked.connect(lambda: self._add_term(subgroup))

        group["subgroups"].append(subgroup)
        self._add_term(subgroup)  # subgroup starts with one usable term
        group["body_layout"].insertWidget(group["body_layout"].count() - 1, frame)
        return subgroup

    def _remove_subgroup(self, group, subgroup):
        had_text = any(e.text().strip() for e in subgroup["terms"])
        group["subgroups"].remove(subgroup)
        subgroup["frame"].setParent(None)
        subgroup["frame"].deleteLater()
        if had_text:
            self.debounce.start()

    def _build_level_legend(self):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 8, 0, 0)
        h.setSpacing(16)
        for color, label in (
            (LVL_ERROR, "ERROR / FATAL"),
            (LVL_WARN, "WARN"),
            (LVL_INFO, "INFO"),
            (LVL_DEBUG, "DEBUG / TRACE"),
        ):
            h.addWidget(self._legend_item(color, label))
        h.addStretch(1)
        return row

    def _legend_item(self, color, text):
        item = QWidget()
        h = QHBoxLayout(item)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background:{color}; border-radius:2px;")
        label = QLabel(text)
        label.setStyleSheet(f"color:{TEXT_DIM}; font-size:10.5px;")
        h.addWidget(dot)
        h.addWidget(label)
        return item

    def _build_statusbar(self):
        bar = QFrame()
        bar.setObjectName("statusbar")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(18, 6, 18, 6)
        outer.setSpacing(2)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(16)
        self.status_label = QLabel("No file loaded")
        row1.addWidget(self.status_label)
        row1.addStretch(1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(16)
        self.filter_expr_label = QLabel("")
        self.filter_expr_label.setVisible(False)
        row2.addWidget(self.filter_expr_label)
        row2.addStretch(1)
        outer.addLayout(row2)

        return bar

    def _build_empty_state(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(14)
        big = QLabel("Open one or more log files, or drag them in here")
        big.setObjectName("emptyBig")
        big.setAlignment(Qt.AlignCenter)
        small = QLabel(
            "Files are memory-mapped and streamed — never modified or rewritten. "
            "When you load more than one file, lines are merged into a single "
            "timeline and sorted by timestamp."
        )
        small.setObjectName("emptyDot")
        small.setAlignment(Qt.AlignCenter)
        small.setWordWrap(True)
        small.setMaximumWidth(440)
        v.addWidget(big)
        v.addWidget(small, 0, Qt.AlignCenter)
        return w

    def _build_table(self):
        self.table = LogTableView()
        self.table.setModel(self.model)
        # set_files() and set_filtered() both wrap their changes in
        # beginResetModel()/endResetModel(), which Qt turns into this signal —
        # one hook covers every case where a previously-selected row's text
        # could no longer exist or now mean something different.
        self.model.modelReset.connect(self.table.clear_text_selection)
        self.table.json_clicked.connect(self._show_json_dialog)
        self.source_delegate = SourceBadgeDelegate(self.model)
        self.lineno_delegate = LineNoDelegate()
        self.line_delegate = LogLineDelegate(self.model)
        self.line_delegate.table_ref = self.table
        self.table.set_line_delegate(self.line_delegate)
        self.table.setItemDelegateForColumn(0, self.source_delegate)
        self.table.setItemDelegateForColumn(1, self.lineno_delegate)
        self.table.setItemDelegateForColumn(2, self.line_delegate)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)  # click/shift-click/ctrl-click on source/line-number columns
        self.table.setWordWrap(False)  # we lay out wrapped text ourselves in the delegate
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QTableView.NoEditTriggers)

        # Column 2 (the log text) supports real cursor-drag character
        # selection (see LogTableView) — click and drag across the text,
        # then Ctrl+C or right-click Copy, just like a text editor.
        # Columns 0/1 (source, line number) still use row selection, since
        # there's nothing meaningful to select character-by-character there.
        copy_shortcut = QShortcut(QKeySequence.Copy, self.table)
        copy_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        copy_shortcut.activated.connect(self.copy_selected_rows)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 70)
        hh.setVisible(False)
        self.table.setColumnHidden(0, True)  # shown only with 2+ files loaded
        self.table.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self.set_wrap_enabled(True)  # matches the web version's default (wrapped)
        return self.table

    def set_wrap_enabled(self, enabled):
        """Toggle between wrapped rows (matches the web version, variable
        row height) and single-line rows with horizontal scroll (fixed
        row height). Wrapped row heights are computed lazily for only the
        rows on screen (see _update_visible_row_heights) rather than via
        Qt's built-in ResizeToContents, which computes every row up front —
        fine for a few thousand rows, but a multi-minute hang for the
        multi-million-row files this app exists to handle. Unwrapping
        remains available as a fully fixed-height fallback."""
        self.line_delegate.wrap_enabled = enabled
        hh = self.table.horizontalHeader()
        if enabled:
            hh.setSectionResizeMode(2, QHeaderView.Stretch)
            vh = self.table.verticalHeader()
            vh.setSectionResizeMode(QHeaderView.Fixed)
            vh.setDefaultSectionSize(ROW_HEIGHT)  # estimate for not-yet-visible rows
        else:
            hh.setSectionResizeMode(2, QHeaderView.Fixed)
            self.table.setColumnWidth(2, 20000)
            # Wrapped mode records an explicit height per visited row (via
            # setRowHeight in _update_visible_row_heights); switching back
            # doesn't discard those on its own, so rows would keep their old
            # wrapped height forever. Installing a fresh header is how Qt
            # discards that per-row size cache in O(1) rather than us
            # visiting every row — which matters when there can be tens of
            # millions of them.
            vh = QHeaderView(Qt.Vertical, self.table)
            vh.setSectionResizeMode(QHeaderView.Fixed)
            vh.setDefaultSectionSize(ROW_HEIGHT)
            self.table.setVerticalHeader(vh)
        vh.setVisible(False)
        self._relayout_rows()

    def _relayout_rows(self):
        if self.wrap_checkbox.isChecked():
            col_width = self.table.columnWidth(2)
            if col_width > 0:
                self.line_delegate.wrap_width = col_width
            self._update_visible_row_heights()
        self.table.viewport().update()

    def _on_scroll(self, _value):
        if self.wrap_checkbox.isChecked():
            self._update_visible_row_heights()

    def _update_visible_row_heights(self):
        """Compute and set exact wrapped heights for just the rows on
        screen (plus a small buffer), so cost stays bounded by viewport
        size — a few dozen rows — no matter how many rows the model has."""
        row_count = self.model.rowCount()
        if row_count == 0:
            return
        viewport_h = self.table.viewport().height()
        top_row = self.table.rowAt(0)
        if top_row == -1:
            top_row = 0
        bottom_row = self.table.rowAt(max(0, viewport_h - 1))
        if bottom_row == -1:
            bottom_row = min(row_count - 1, top_row + 60)
        top_row = max(0, top_row - 5)
        bottom_row = min(row_count - 1, bottom_row + 5)

        col_width = self.table.columnWidth(2)
        if col_width > 0:
            self.line_delegate.wrap_width = col_width
        width = self.line_delegate.wrap_width - 2 * LogLineDelegate.PAD_X
        font = self.table.font()

        for row in range(top_row, bottom_row + 1):
            idx = self.model.index(row, 2)
            text = self.model.data(idx, Qt.DisplayRole) or ""
            _layout, h, _line_spacing = self.line_delegate._build_layout(text, width, font)
            needed = max(ROW_HEIGHT, int(h) + 2 * LogLineDelegate.PAD_Y)
            if self.table.rowHeight(row) != needed:
                self.table.setRowHeight(row, needed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.wrap_checkbox.isChecked():
            self.wrap_resize_debounce.start()

    # -- Drag & drop ---------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.add_files(paths)

    # -- File loading (supports multiple files, added incrementally) ----
    def open_file_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open log files", "", "Log files (*.log *.txt);;All files (*.*)"
        )
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        if not paths:
            return
        if self.index_worker is not None:
            self.index_worker.cancel()
        self._pending_paths = list(paths)
        self.open_btn.setEnabled(False)
        self._index_next_pending()

    def _index_next_pending(self):
        if not self._pending_paths:
            self.open_btn.setEnabled(True)
            self.rebuild_and_apply()
            return
        path = self._pending_paths.pop(0)
        self.status_label.setText(f"Indexing {os.path.basename(path)}…")
        worker = IndexWorker(path)
        self.index_worker = worker
        worker.progress.connect(
            lambda p, name=os.path.basename(path): self.status_label.setText(f"Indexing {name}… {p}%")
        )
        worker.finished_ok.connect(lambda offsets, size: self._on_one_indexed(path, offsets, size))
        worker.failed.connect(lambda msg: self._on_index_failed(path, msg))
        worker.start()

    def _on_one_indexed(self, path, offsets, file_size):
        name = os.path.basename(path)
        for f in self.files:
            if f.name == name:
                f.close()
        self.files = [f for f in self.files if f.name != name]
        color = SOURCE_COLORS[len(self.files) % len(SOURCE_COLORS)]
        lf = LoadedFile(path, color)
        lf.set_index(offsets, file_size)
        lf.open_mmap()
        self.files.append(lf)
        self._index_next_pending()

    def _on_index_failed(self, path, message):
        self.status_label.setText(
            f'<span style="color:{DANGER}">Could not read {os.path.basename(path)}: {message}</span>'
        )
        self._index_next_pending()

    def rebuild_and_apply(self):
        if len(self.files) <= 1:
            total_rows = self.files[0].total_lines() if self.files else 0
            self.model.set_files(list(self.files), None, None, total_rows)
            self._after_files_changed()
            self.run_filter()
            return

        self.status_label.setText("Merging files… 0%")
        self.export_btn.setEnabled(False)
        self.remove_all_btn.setEnabled(False)
        worker = MergeSortWorker(list(self.files))
        self.merge_worker = worker
        worker.progress.connect(lambda p: self.status_label.setText(f"Merging files… {p}%"))
        worker.finished_ok.connect(lambda of, ol, tr: self._on_merged(worker, of, ol, tr))
        worker.failed.connect(lambda msg: self._on_merge_failed(worker, msg))
        worker.start()

    def _on_merged(self, worker, order_file, order_line, total_rows):
        if worker is not self.merge_worker:
            return
        self.model.set_files(list(self.files), order_file, order_line, total_rows)
        self._after_files_changed()
        self.run_filter()

    def _on_merge_failed(self, worker, message):
        if worker is not self.merge_worker:
            return
        self.status_label.setText(f'<span style="color:{DANGER}">{message}</span>')
        self.remove_all_btn.setEnabled(bool(self.files))

    def _after_files_changed(self):
        if not self.files:
            self.stack.setCurrentIndex(0)
            self.filename_label.setText("")
            self.table.setColumnHidden(0, True)
            self._rebuild_source_legend()
            self.export_btn.setEnabled(False)
            self.remove_all_btn.setEnabled(False)
            return
        self.stack.setCurrentIndex(1)
        if len(self.files) == 1:
            self.filename_label.setText(f"{self.files[0].name}  ({human_size(self.files[0].file_size)})")
        else:
            self.filename_label.setText(f"{len(self.files)} files loaded, {self.model.total_rows:,} lines merged")
        self.table.setColumnHidden(0, len(self.files) < 2)
        self._rebuild_source_legend()
        self.export_btn.setEnabled(True)
        self.remove_all_btn.setEnabled(True)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _rebuild_source_legend(self):
        self._clear_layout(self.source_legend_layout)
        if len(self.files) < 2:
            return
        for lf in self.files:
            chip = QWidget()
            h = QHBoxLayout(chip)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(5)
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(f"background:{lf.color}; border-radius:2px;")
            name = QLabel(lf.name)
            name.setStyleSheet(f"color:{TEXT_DIM}; font-size:10.5px;")
            remove_btn = QPushButton("×")
            remove_btn.setFixedWidth(18)
            remove_btn.setStyleSheet(
                f"border:none; background:transparent; color:{TEXT_DIM}; font-size:13px; padding:0;"
            )
            remove_btn.setToolTip(f"Remove {lf.name}")
            remove_btn.clicked.connect(lambda _=None, n=lf.name: self.remove_file(n))
            h.addWidget(dot)
            h.addWidget(name)
            h.addWidget(remove_btn)
            self.source_legend_layout.addWidget(chip)
        self.source_legend_layout.addStretch(1)

    def remove_file(self, name):
        lf = next((f for f in self.files if f.name == name), None)
        if lf is None:
            return
        lf.close()
        self.files = [f for f in self.files if f.name != name]
        if not self.files:
            self.remove_all_files()
            return
        self.rebuild_and_apply()

    def remove_all_files(self):
        if self.filter_worker is not None:
            self.filter_worker.cancel()
        if self.merge_worker is not None:
            self.merge_worker.cancel()
        for f in self.files:
            f.close()
        self.files = []
        self.model.set_files([], None, None, 0)
        self._after_files_changed()
        self.status_label.setText("No file loaded")
        self.filter_expr_label.setVisible(False)
    def clear_filters(self):
        # Collapse down to a single group with a single empty term and no
        # subgroups — removing extras here directly (not via
        # _remove_keyword_group / _remove_subgroup / _remove_term) since
        # those trigger their own re-filter via debounce, and clear_filters
        # ends with one explicit run_filter() for the whole reset.
        while len(self.keyword_groups) > 1:
            group = self.keyword_groups.pop()
            group["frame"].setParent(None)
            group["frame"].deleteLater()
        group0 = self.keyword_groups[0]
        for sg in list(group0["subgroups"]):
            group0["subgroups"].remove(sg)
            sg["frame"].setParent(None)
            sg["frame"].deleteLater()
        while len(group0["terms"]) > 1:
            edit = group0["terms"].pop()
            row_widget = edit.parentWidget()
            if row_widget is not None:
                row_widget.setParent(None)
                row_widget.deleteLater()
        group0["terms"][0].blockSignals(True)
        group0["terms"][0].setText("")
        group0["terms"][0].blockSignals(False)
        group0["combo"].blockSignals(True)
        group0["combo"].setCurrentIndex(0)
        group0["combo"].blockSignals(False)
        self._update_group_remove_buttons()

        for w in (self.from_input, self.to_input):
            w.blockSignals(True)
            w.setText("")
            w.blockSignals(False)
        self.case_checkbox.blockSignals(True)
        self.case_checkbox.setChecked(False)
        self.case_checkbox.blockSignals(False)
        self.regex_checkbox.blockSignals(True)
        self.regex_checkbox.setChecked(False)
        self.regex_checkbox.blockSignals(False)
        self.combine_combo.blockSignals(True)
        self.combine_combo.setCurrentIndex(0)
        self.combine_combo.blockSignals(False)
        self.run_filter()

    def run_filter(self):
        if not self.files:
            return

        # Each top-level group becomes {'mode', 'terms', 'subgroups'}, where
        # subgroups is itself a list of {'mode', 'terms'} — one extra level
        # of (…) nesting. Groups/subgroups with no non-blank terms anywhere
        # inside them are dropped so they don't count as an active filter
        # unit at the outer AND/OR level.
        kw_groups = []
        all_terms = []
        for g in self.keyword_groups:
            terms = [t for t in (e.text().strip() for e in g["terms"]) if t]
            subgroups = []
            for sg in g["subgroups"]:
                sg_terms = [t for t in (e.text().strip() for e in sg["terms"]) if t]
                if sg_terms:
                    subgroups.append({"mode": sg["combo"].currentData(), "terms": sg_terms})
                    all_terms.extend(sg_terms)
            if terms or subgroups:
                kw_groups.append({"mode": g["combo"].currentData(), "terms": terms, "subgroups": subgroups})
                all_terms.extend(terms)

        case_sensitive = self.case_checkbox.isChecked()
        regex_mode = self.regex_checkbox.isChecked()
        from_dt = parse_datetime_field(self.from_input.text())
        to_dt = parse_datetime_field(self.to_input.text())
        combine_mode = self.combine_combo.currentData()

        kw_re_str = None
        if all_terms:
            flags = 0 if case_sensitive else re.IGNORECASE
            parts = [t if regex_mode else re.escape(t) for t in all_terms]
            # One combined regex covers highlighting for every term in every
            # group — grouping/AND/OR only affects which *rows* survive
            # filtering; every term that appears in a surviving row still
            # gets highlighted, regardless of which group it came from.
            combined_pattern = "|".join(f"(?:{p})" for p in parts)
            try:
                kw_re_str = re.compile(combined_pattern, flags)
            except re.error:
                self.status_label.setText(f'<span style="color:{DANGER}">Invalid regular expression</span>')
                return
        self.model.set_highlight(kw_re_str, case_sensitive)

        no_filter = not kw_groups and from_dt is None and to_dt is None
        if no_filter:
            if self.filter_worker is not None:
                self.filter_worker.cancel()
                # cancel() only sets a flag the worker thread checks; if it
                # had already finished and queued its finished_ok signal
                # before this, that signal is still coming. Clearing the
                # reference (rather than leaving it pointing at a "cancelled"
                # worker) makes _on_filtered's "worker is not self.filter_worker"
                # check reject that stale result instead of applying it over
                # the None we're about to set.
                self.filter_worker = None
            self.model.set_filtered(None)
            self._relayout_rows()
            self.update_status()
            return

        if self.filter_worker is not None:
            self.filter_worker.cancel()

        self.export_btn.setEnabled(False)
        self.status_label.setText("Filtering… 0%")
        worker = FilterWorker(
            self.model.files, self.model.order_file, self.model.order_line, self.model.total_rows,
            kw_groups, case_sensitive, regex_mode, from_dt, to_dt, combine_mode
        )
        self.filter_worker = worker
        worker.progress.connect(lambda p: self.status_label.setText(f"Filtering… {p}%"))
        worker.finished_ok.connect(lambda matches, total: self._on_filtered(worker, matches, total))
        worker.failed.connect(lambda msg: self._on_filter_failed(worker, msg))
        worker.start()

    def _on_filtered(self, worker, matches, total):
        if worker is not self.filter_worker:
            return  # superseded by a newer search
        self.model.set_filtered(matches)
        self._relayout_rows()
        self.export_btn.setEnabled(True)
        self.update_status()

    def _on_filter_failed(self, worker, message):
        if worker is not self.filter_worker:
            return
        self.export_btn.setEnabled(True)
        self.status_label.setText(f'<span style="color:{DANGER}">{message}</span>')

    def _describe_container(self, container, wrap):
        """Renders one group or subgroup as text, e.g. 'A OR B'. Subgroup
        text is always parenthesized when it has 2+ parts (it's always
        nested inside something else); a top-level group is only
        parenthesized when `wrap` is True, which build_filter_expression
        sets based on whether there's more than one top-level group — so a
        single group renders as "(A OR B) AND C" rather than the
        technically-equivalent but noisier "((A OR B) AND C)"."""
        parts = []
        for sg in container.get("subgroups", []):
            sg_terms = [t for t in (e.text().strip() for e in sg["terms"]) if t]
            if sg_terms:
                parts.append(self._describe_container(sg, wrap=True))
        parts.extend(t for t in (e.text().strip() for e in container["terms"]) if t)
        if not parts:
            return None
        mode_str = " OR " if container["combo"].currentData() == "or" else " AND "
        if len(parts) == 1:
            return parts[0]
        joined = mode_str.join(parts)
        return f"({joined})" if wrap else joined

    def build_filter_expression(self):
        """Human-readable form of the active keyword filter, e.g.
        '(A OR B) AND C' or '((A OR B) AND C) OR D' — shown in the status
        line so the boolean logic that's actually being applied is visible
        at a glance instead of something you have to reason through."""
        multi = len(self.keyword_groups) > 1
        group_exprs = [self._describe_container(g, wrap=multi) for g in self.keyword_groups]
        group_exprs = [e for e in group_exprs if e]
        if not group_exprs:
            return None
        if len(group_exprs) == 1:
            return group_exprs[0]
        mode_str = " OR " if self.combine_combo.currentData() == "or" else " AND "
        return mode_str.join(group_exprs)

    def update_status(self):
        if not self.files:
            self.status_label.setText("No file loaded")
            self.filter_expr_label.setVisible(False)
            return
        if len(self.files) == 1:
            current_name = self.files[0].name
        else:
            current_name = f"{len(self.files)} files (" + ", ".join(f.name for f in self.files) + ")"

        parts = [f"File: <b>{current_name}</b>"]
        if len(self.files) > 1:
            parts.append(f"Files: <b>{len(self.files)}</b>")
        parts.append(f"Total lines: <b>{self.model.total_rows:,}</b>")
        if self.model.filtered is not None:
            parts.append(f'Matches: <b style="color:{ACCENT_2}">{len(self.model.filtered):,}</b>')
        else:
            parts.append("Showing all lines")
        self.status_label.setText("&nbsp;&nbsp;&nbsp;".join(parts))

        expr = self.build_filter_expression()
        if expr:
            self.filter_expr_label.setText(f'Filter: <b style="color:{ACCENT}">{expr}</b>')
            self.filter_expr_label.setVisible(True)
        else:
            self.filter_expr_label.setVisible(False)

    # -- Copy ------------------------------------------------------------
    # -- JSON pretty-print dialog -----------------------------------------
    def _show_json_dialog(self, raw_text):
        pretty, was_valid = prettify_json_text(raw_text)

        dialog = QDialog(self)
        dialog.setWindowTitle("JSON" if was_valid else "JSON (best-effort formatting)")
        dialog.resize(720, 560)
        dialog.setStyleSheet(f"QDialog {{ background: {BG}; }}")

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        if not was_valid:
            warning = QLabel(
                "Couldn't fully parse this as strict JSON (likely unescaped nested "
                "quotes in the source log) — Prettify falls back to the original text."
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color:{LVL_WARN}; font-size:11px;")
            layout.addWidget(warning)

        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        view.setStyleSheet(
            f"QTextEdit {{ background:{PANEL}; color:{TEXT}; border:1px solid {LINE}; "
            f"selection-background-color:{ACCENT_2}; }}"
        )
        layout.addWidget(view, 1)

        # Starts on the prettified view (when parsing succeeded); the toggle
        # switches to the exact original matched text and back, so you can
        # get at either form — formatted for reading, raw for an exact copy.
        state = {"showing_pretty": was_valid}

        def render():
            view.setPlainText(pretty if state["showing_pretty"] else raw_text)
            toggle_btn.setText("Show Raw" if state["showing_pretty"] else "Prettify")

        def toggle():
            state["showing_pretty"] = not state["showing_pretty"]
            render()

        btn_row = QHBoxLayout()
        toggle_btn = QPushButton()
        toggle_btn.clicked.connect(toggle)
        btn_row.addWidget(toggle_btn)
        btn_row.addStretch(1)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(view.toPlainText()))
        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        render()

        dialog.exec()

    def copy_selected_rows(self):
        if not self.files:
            return

        # A cursor-drag text selection (exact character range, possibly
        # spanning rows) takes priority over row selection when both could
        # apply — it's the more specific, more recently made choice.
        if self.table.has_text_selection():
            QApplication.clipboard().setText(self.table.get_selected_text())
            return

        selection_model = self.table.selectionModel()
        if selection_model is None or not selection_model.hasSelection():
            return
        rows = sorted(idx.row() for idx in selection_model.selectedRows())
        if not rows:
            return
        multi = self.model.multi
        lines = []
        for row in rows:
            file_idx, line_no = self.model.entry_for_row(row)
            lf = self.model.files[file_idx]
            text = lf.line_text(line_no)
            lines.append(f"[{lf.name}] {text}" if multi else text)
        QApplication.clipboard().setText("\n".join(lines))

    def _show_table_context_menu(self, pos):
        if not self.table.selectionModel().hasSelection() and not self.table.has_text_selection():
            return
        menu = QMenu(self.table)
        copy_action = menu.addAction("Copy")
        copy_action.triggered.connect(self.copy_selected_rows)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # -- Export --------------------------------------------------------
    def export_matches(self):
        if not self.files:
            return
        multi = self.model.multi
        base = self.files[0].name.rsplit('.', 1)[0] if len(self.files) == 1 else "merged-logs"
        default_name = f"{base}-filtered.txt"
        out_path, _ = QFileDialog.getSaveFileName(self, "Export matches", default_name, "Text files (*.txt)")
        if not out_path:
            return

        row_numbers = self.model.filtered  # None -> export every row

        self.export_btn.setEnabled(False)
        self.status_label.setText("Exporting… 0%")
        worker = ExportWorker(
            self.model.files, self.model.order_file, self.model.order_line, self.model.total_rows,
            row_numbers, multi, out_path
        )
        self.export_worker = worker
        worker.progress.connect(lambda p: self.status_label.setText(f"Exporting… {p}%"))
        worker.finished_ok.connect(lambda count: self._on_exported(count))
        worker.failed.connect(lambda msg: self._on_export_failed(msg))
        worker.start()

    def _on_exported(self, count):
        self.export_btn.setEnabled(True)
        self.status_label.setText(f"Exported {count:,} lines.")
        QTimer.singleShot(2500, self.update_status)

    def _on_export_failed(self, message):
        self.export_btn.setEnabled(True)
        self.status_label.setText(f'<span style="color:{DANGER}">Export failed: {message}</span>')

    def closeEvent(self, event):
        for w in (self.index_worker, self.merge_worker, self.filter_worker, self.export_worker):
            if w is not None:
                w.cancel()
                w.wait(2000)
        for f in self.files:
            f.close()
        super().closeEvent(event)


def main():
    # Windows groups a running app's taskbar entry (and picks its icon) by
    # process identity, not by setWindowIcon() alone — without this, a
    # plain `python.exe`-launched GUI app can still show python.exe's own
    # icon in the taskbar even though the window icon is set correctly.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LogLens.DesktopApp")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())
    win = LogLensWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
