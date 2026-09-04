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
from array import array
from datetime import datetime

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QThread, Signal, QTimer, QRect, QSize
)
from PySide6.QtGui import QColor, QPainter, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QCheckBox, QComboBox, QFileDialog, QTableView,
    QHeaderView, QStyledItemDelegate, QStyle, QStackedWidget, QFrame
)

# ---------------------------------------------------------------------------
# Theme (mirrors the original web UI's CSS variables)
# ---------------------------------------------------------------------------
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

KW_MARK_BG = QColor(74, 144, 226, 82)     # rgba(74,144,226,0.32)
KW_MARK_TEXT = QColor("#eaf3ff")
TRACE_MARK_BG = QColor(255, 180, 84, 82)  # rgba(255,180,84,0.32)
TRACE_MARK_TEXT = QColor("#fff4e0")

SOURCE_COLORS = ['#4a90e2', '#5fd68a', '#f2b155', '#c792ea', '#ff8fa3', '#4fc1c9']

ROW_HEIGHT = 24
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
                 keyword, traceid, case_sensitive, regex_mode, from_dt, to_dt, combine_mode):
        super().__init__()
        self.files = files
        self.order_file = order_file  # None => single-file direct mode (row == line_no in files[0])
        self.order_line = order_line
        self.total_rows = total_rows
        self.keyword = keyword
        self.traceid = traceid
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
            kw_search = None
            if self.keyword:
                pattern = self.keyword if self.regex_mode else re.escape(self.keyword)
                flags = 0 if self.case_sensitive else re.IGNORECASE
                try:
                    kw_search = re.compile(pattern.encode('utf-8', 'surrogateescape'), flags).search
                except re.error as e:
                    self.failed.emit(f"Invalid regular expression: {e}")
                    return

            trace_needle = None
            if self.traceid:
                tb = self.traceid.encode('utf-8', 'surrogateescape')
                trace_needle = tb if self.case_sensitive else tb.lower()

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
                if kw_search is not None:
                    active += 1
                    if kw_search(raw) is not None:
                        passed += 1
                if trace_needle is not None:
                    active += 1
                    hay = raw if case_sensitive else raw.lower()
                    if trace_needle in hay:
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
        self.trace_needle_str = None
        self.trace_case_sensitive = False

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

    def set_highlight(self, kw_re_str, trace_needle_str, case_sensitive):
        self.kw_re_str = kw_re_str
        self.trace_needle_str = trace_needle_str
        self.trace_case_sensitive = case_sensitive

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
        """Background highlight spans: keyword matches and trace-id hits."""
        spans = []
        if self.kw_re_str:
            try:
                for m in self.kw_re_str.finditer(text):
                    if m.end() > m.start():
                        spans.append((m.start(), m.end(), 'kw'))
            except re.error:
                pass
        if self.trace_needle_str:
            hay = text if self.trace_case_sensitive else text.lower()
            needle = self.trace_needle_str if self.trace_case_sensitive else self.trace_needle_str.lower()
            if needle:
                start = 0
                while True:
                    i = hay.find(needle, start)
                    if i == -1:
                        break
                    spans.append((i, i + len(needle), 'trace'))
                    start = i + len(needle)
        if not spans:
            return spans
        spans.sort(key=lambda s: s[0])
        merged = [spans[0]]
        for s in spans[1:]:
            last = merged[-1]
            if s[0] < last[1]:
                if s[2] == 'trace':  # trace wins over keyword on overlap
                    merged[-1] = (last[0], max(last[1], s[1]), 'trace')
                continue
            merged.append(s)
        return merged

    def _fg_spans(self, text):
        """Foreground color spans: the log-level word and any method:[name]."""
        spans = []
        window = text[:LEVEL_SCAN_WINDOW]
        for _level, color, pattern in LEVEL_PATTERNS:
            m = pattern.search(window)
            if m:
                spans.append((m.start(), m.end(), color))
                break
        for m in METHOD_PATTERN.finditer(text):
            spans.append((m.start(2), m.end(2), ACCENT_2))
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


class LogLineDelegate(QStyledItemDelegate):
    """Paints column 2 (the log text) with level/method coloring and
    keyword/trace highlight marks, single-line with horizontal scroll."""

    def __init__(self, model: LogTableModel, parent=None):
        super().__init__(parent)
        self.model_ref = model

    def paint(self, painter: QPainter, option, index):
        if index.column() != 2:
            super().paint(painter, option, index)
            return

        text = index.data(Qt.DisplayRole) or ""
        painter.save()
        painter.setClipRect(option.rect)

        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        fm = option.fontMetrics
        pad = 8
        x = option.rect.x() + pad
        y = option.rect.y()
        h = option.rect.height()
        max_x = option.rect.right()

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
            elif bg_cls == 'trace':
                painter.fillRect(QRect(x, y, min(w, max_x - x), h), TRACE_MARK_BG)
                default_color = TRACE_MARK_TEXT
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

        painter.restore()

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)


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
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class LogLensWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Log Lens")
        self.resize(1320, 820)
        self.setAcceptDrops(True)

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
        self.filename_label = QLabel("")
        self.filename_label.setObjectName("filenameLabel")
        self.filename_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        title_row.addWidget(dot)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.filename_label)
        v.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.open_btn = QPushButton("Open log files")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self.open_file_dialog)
        toolbar.addWidget(self.open_btn)

        self.remove_all_btn = QPushButton("Remove all files")
        self.remove_all_btn.setEnabled(False)
        self.remove_all_btn.clicked.connect(self.remove_all_files)
        toolbar.addWidget(self.remove_all_btn)

        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("e.g. exception, failed")
        self.keyword_input.setMinimumWidth(210)
        toolbar.addLayout(self._field("Keyword / text", self.keyword_input))

        self.trace_input = QLineEdit()
        self.trace_input.setPlaceholderText("e.g. 8f21ac-9c4b")
        self.trace_input.setMinimumWidth(160)
        toolbar.addLayout(self._field("Trace / Request ID", self.trace_input))

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
        toolbar.addWidget(self.case_checkbox)
        toolbar.addWidget(self.regex_checkbox)

        self.combine_combo = QComboBox()
        self.combine_combo.addItem("Match ALL (AND)", "and")
        self.combine_combo.addItem("Match ANY (OR)", "or")
        toolbar.addLayout(self._field("Combine filters", self.combine_combo))

        self.clear_btn = QPushButton("Clear filters")
        self.clear_btn.clicked.connect(self.clear_filters)
        toolbar.addWidget(self.clear_btn)

        self.export_btn = QPushButton("Export matches")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_matches)
        toolbar.addWidget(self.export_btn)

        toolbar.addStretch(1)
        v.addLayout(toolbar)

        v.addWidget(self._build_level_legend())
        self.source_legend_row = QWidget()
        self.source_legend_layout = QHBoxLayout(self.source_legend_row)
        self.source_legend_layout.setContentsMargins(0, 0, 0, 0)
        self.source_legend_layout.setSpacing(16)
        v.addWidget(self.source_legend_row)

        for w in (self.keyword_input, self.trace_input, self.from_input, self.to_input):
            w.textChanged.connect(lambda _=None: self.debounce.start())
        self.case_checkbox.toggled.connect(lambda _=None: self.debounce.start())
        self.regex_checkbox.toggled.connect(lambda _=None: self.debounce.start())
        self.combine_combo.currentIndexChanged.connect(lambda _=None: self.debounce.start())

        return header

    def _field(self, label_text, widget):
        col = QVBoxLayout()
        col.setSpacing(3)
        label = QLabel(label_text)
        label.setStyleSheet(f"color:{TEXT_DIM}; font-size:10px; padding-left:2px;")
        col.addWidget(label)
        col.addWidget(widget)
        return col

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
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 6, 18, 6)
        h.setSpacing(16)
        self.status_label = QLabel("No file loaded")
        h.addWidget(self.status_label)
        h.addStretch(1)
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
        self.table = QTableView()
        self.table.setModel(self.model)
        self.source_delegate = SourceBadgeDelegate(self.model)
        self.line_delegate = LogLineDelegate(self.model)
        self.table.setItemDelegateForColumn(0, self.source_delegate)
        self.table.setItemDelegateForColumn(2, self.line_delegate)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QTableView.NoEditTriggers)

        vh = self.table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(ROW_HEIGHT)
        vh.setVisible(False)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 20000)
        hh.setVisible(False)
        self.table.setColumnHidden(0, True)  # shown only with 2+ files loaded

        return self.table

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

    # -- Filtering ---------------------------------------------------------
    def clear_filters(self):
        for w in (self.keyword_input, self.trace_input, self.from_input, self.to_input):
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

        keyword = self.keyword_input.text().strip()
        traceid = self.trace_input.text().strip()
        case_sensitive = self.case_checkbox.isChecked()
        regex_mode = self.regex_checkbox.isChecked()
        from_dt = parse_datetime_field(self.from_input.text())
        to_dt = parse_datetime_field(self.to_input.text())
        combine_mode = self.combine_combo.currentData()

        kw_re_str = None
        if keyword:
            pattern = keyword if regex_mode else re.escape(keyword)
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                kw_re_str = re.compile(pattern, flags)
            except re.error:
                self.status_label.setText(f'<span style="color:{DANGER}">Invalid regular expression</span>')
                return
        self.model.set_highlight(kw_re_str, traceid or None, case_sensitive)

        no_filter = not keyword and not traceid and from_dt is None and to_dt is None
        if no_filter:
            if self.filter_worker is not None:
                self.filter_worker.cancel()
            self.model.set_filtered(None)
            self.table.viewport().update()
            self.update_status()
            return

        if self.filter_worker is not None:
            self.filter_worker.cancel()

        self.export_btn.setEnabled(False)
        self.status_label.setText("Filtering… 0%")
        worker = FilterWorker(
            self.model.files, self.model.order_file, self.model.order_line, self.model.total_rows,
            keyword, traceid, case_sensitive, regex_mode, from_dt, to_dt, combine_mode
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
        self.table.viewport().update()
        self.export_btn.setEnabled(True)
        self.update_status()

    def _on_filter_failed(self, worker, message):
        if worker is not self.filter_worker:
            return
        self.export_btn.setEnabled(True)
        self.status_label.setText(f'<span style="color:{DANGER}">{message}</span>')

    def update_status(self):
        if not self.files:
            self.status_label.setText("No file loaded")
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
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    win = LogLensWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
