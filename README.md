# Log Lens (desktop)

A Windows desktop port of `log-lens.html`, kept in sync with it feature for
feature, and rebuilt to handle multi-gigabyte log files (tested with a 5 GB
target). Same dark UI as the web version — keyword search (plain or regex),
trace/request ID, date range, case sensitivity, AND/OR filter combining,
multi-file loading with a merged timeline, log-level and method-name
coloring — plus export of the filtered lines.

## Why the web version doesn't scale, and what changed

The original reads each file into memory as a JS array of line strings.
That's fine for small files but falls over well before 5 GB, and merging
several such files gets expensive fast.

The desktop version never loads file content into memory:

- Opening a file scans it once in a background thread and records the
  **byte offset** of every line start (an array of 64-bit ints — for a 5 GB
  file with, say, 40–50M lines, that's roughly 300–400 MB of index, not 5 GB
  of text).
- The file is then memory-mapped (`mmap`). With exactly **one** file loaded,
  the table needs no scan at all: row → `offsets[row]`, read on demand.
  Opening a file and scrolling through it is instant regardless of size.
- With **two or more** files loaded, their lines are merged into one
  timeline sorted by timestamp (carrying a file's last-seen timestamp
  forward for lines that don't have their own, e.g. stack traces) — same
  behavior as the web version. This does require a one-time scan of every
  loaded file to extract timestamps; there's no way to merge-order lines
  without knowing where they fall in time. That scan runs in a background
  thread with a progress readout, and only kicks in once a second file is
  added — a single huge file stays on the instant, no-scan path.
- Applying a filter (keyword / trace id / date range, combined with AND or
  OR) runs once in a background thread over the memory-mapped file(s) at
  the byte level, and produces a second, much smaller index: the row
  numbers that matched. Only that index is kept in memory. The UI thread
  never blocks — a status bar shows scan progress, and starting a new
  search cancels the one in flight.

One intentional UI difference: log lines don't wrap (the web version used
`white-space: pre-wrap`). The desktop table scrolls horizontally instead —
standard for log viewers, and avoids per-row variable-height layout, which
would be a real cost at tens of millions of rows.

## Feature parity with the web version

- Keyword/text search, plain or regex, case-sensitive toggle
- Trace/Request ID search (plain substring, same case-sensitivity toggle)
- From/To date-range filtering
- **Combine filters: Match ALL (AND) / Match ANY (OR)**
- **Multiple files**, merged into one timestamp-sorted timeline; each
  source gets a colored badge and its own "×" remove button; removing all
  files but one drops back to the fast single-file path automatically
- **Log-level coloring** (ERROR/FATAL/CRITICAL, WARN/WARNING, INFO,
  DEBUG/TRACE — colored, bold, only the level word itself, only near the
  start of the line so a message that merely mentions "error" isn't
  mis-tagged)
- **Method-name coloring** (`method: [name]` → the bracketed name in blue)
- Export matches (source-prefixed with `[filename]` when multiple files are
  loaded, same as the web version)
- Clear filters

## Measured performance

On this machine, a synthetic ~500 MB / 5.76M-line log file, single-file mode:

| Operation | Time | Throughput |
|---|---|---|
| Indexing (on open) | 1.0 s | ~485 MB/s |
| Keyword/trace filter | 4.7 s | ~106 MB/s |
| Date-range filter | 10.8 s | ~46 MB/s (parses a timestamp per line) |
| Random line read (scrolling) | 0.001 ms/line | effectively instant |

Extrapolating to 5 GB: indexing ~10 s, keyword/trace filtering ~45 s,
date-range filtering ~1.5–2 min. All of this runs off the UI thread with a
live percentage in the status bar. Peak RAM is dominated by the line-offset
index (proportional to line *count*, not file size) plus whatever the OS
pages in from the memory-mapped file — not the full file size.

Loading a **second** file triggers the merge-sort scan, which has to read
and timestamp-parse every line of every loaded file up front (there's no
way around that for a feature whose whole point is ordering by timestamp).
Budget roughly the same per-GB cost as the date-range filter above for that
one-time merge, then instant scrolling and normal filter speeds afterward.

`tests/perf_test.py` regenerates the single-file benchmark (pass a byte
count to change the target file size, e.g.
`python tests/perf_test.py 5000000000` for a full 5 GB run — this will use
~5 GB of disk and take several minutes just to generate the file).

## Running it

```bash
pip install -r requirements.txt
python log_lens.py
```

Requires Python 3.9+ (tested on 3.14) and PySide6 (Qt for Python), the only
dependency.

## Quick correctness check

```bash
python tests/smoke_test.py
```

Exercises indexing, single- and multi-file filtering (keyword/regex/trace/
date, AND and OR combine), the merge-sort ordering, level/method-name color
spans, and source-prefixed export — headless, no window needed, runs in a
couple of seconds.

```bash
python tests/gui_launch_test.py
```

Launches the real window, loads two files, filters, clears, removes one
file (falling back to single-file mode), and closes — end-to-end check of
the actual Qt UI, not just the logic underneath it.

## Packaging as a standalone .exe (optional)

So it can be launched without a Python install:

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name "Log Lens" log_lens.py
```

The executable is written to `dist/Log Lens.exe`. `--onefile` is slower to
start (it unpacks to a temp dir each launch); drop it for a `dist/Log Lens/`
folder build that starts faster if that matters more than a single file.

## Notes / limitations

- Text is assumed to be UTF-8 (invalid bytes are replaced, not dropped).
- Timestamp parsing supports the same three formats as the original:
  ISO 8601 (`YYYY-MM-DD HH:MM:SS`, with optional fraction and `Z`/offset),
  `MM/DD/YYYY HH:MM:SS`, and `DD-MM-YYYY HH:MM:SS`. Date filtering looks
  first at the start of the line (fast path) and only falls back to
  scanning the whole line if that misses.
- The From/To fields are plain text (`YYYY-MM-DD HH:MM:SS`); leave either
  empty to leave that bound open, same as leaving the original's
  `datetime-local` inputs blank.
- Level-word and method-name coloring always run (not gated by any filter),
  matching the web version.
