# HTML report

The CLI writes an HTML report next to the JSON: `diff.json` gives `diff.html`.
Without `-o`, it is `gtfs-diff.html`. `--html PATH` picks another path;
`--no-html` skips it.

From Python:

```python
from gtfs_diff.engine import run_diff
from gtfs_diff.report.html import render_html

run = run_diff("base.zip", "new.zip", shape_geometry=True)
Path("diff.html").write_text(render_html(run))
```

It is one self-contained file and works offline. Only the street map behind
maps comes from the internet (Esri, no key needed).

## What it shows

| Tab | Shows |
|---|---|
| Overview | Notable highlights ([highlights.md](highlights.md)), rows changed per file, the shapes that moved most |
| Files | Each file's added, deleted and modified rows, old → new. Click a column to filter; find searches the rows on the page |
| Shapes | Every changed shape, old against new (needs `--shape-geometry`). Rings mark the moves that are notable highlights |

The header gives the releases, their dates for Mobility Database datasets, and
row totals. **Settings used** explains every option. The URL keeps the open
tab, file and map, so a link reopens the same view.

## Maps

Moved shapes and moved stops open on a map; a removed stop opens its row in
`stops.txt`. ← and → step
through the list; Esc closes it.

| Drawn as | Means |
|---|---|
| Grey line | Same in both shapes |
| Red line | Only in the old shape |
| Green dashed line | Only in the new shape |
| Thick line | More than 20 m from the other line |
| Red or green dot | A point only in the old or new shape |
| Hollow red dot, green dot | A stop's old and new place |
| Orange ring | Each place a shape moved 50 m or more: its notable highlight (small drawings only) |

Scroll, double-click or + and − to zoom; drag to move. Click a line or dot for
that point. Click a legend line to hide it. The side list gives every change,
largest first; click one to zoom to it. Offline, the lines still draw and
the page says the street map is unavailable.


## Caps

The report shows fewer rows than the JSON, for speed. It never shows more than
the JSON holds.

| What | Cap | Set with |
|---|---|---|
| Rows per file, per kind | 100 | `--html-cap` |
| Rows per changed column | 100 | `--html-cap` |
| Shapes drawn | 100, worst first | `render_html(shape_cap=)` |
| Points per shape | 300, keeping the ones that moved most | `HTML_SHAPE_POINTS` |

When something is left out, the report says how many it shows of the total.
