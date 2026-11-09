"""Opening feeds: zip archives, folders and public HTTP(S) folder URLs."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ..gtfs_definitions import (
    SUPPORTED_FILES,
)
from .csv_utils import (
    _is_url,
)

# A "lazy opener" maps a filename (e.g. "stops.txt") to a zero-arg callable
# that opens the file and returns a text stream.
LazyOpeners = dict[str, Callable[[], TextIO]]


@dataclass
class FeedFileMeta:
    """Side-channel metadata about a feed file, used to route large files to the
    DuckDB backend without changing the text-opener fast path.

    Attributes:
        size:        Uncompressed size in bytes, if cheaply known (directory
                     ``stat``, zip ``file_size``, or HTTP ``Content-Length``);
                     ``None`` when unknown.
        local_path:  Real filesystem path DuckDB can read directly (set only for
                     plain-directory feeds); ``None`` for zip members and URLs.
        materialize: Writes the file's raw bytes to a given destination path,
                     streaming (bounded memory). Used to stage zip members to a
                     temp file for DuckDB.
        url:         Direct ``http(s)://`` URL of the file. When set, DuckDB reads
                     it in place via its ``httpfs`` extension (HTTP range
                     requests) instead of us downloading the whole file first —
                     our side only performs HEAD requests (size / existence).
    """

    size: int | None = None
    local_path: str | None = None
    materialize: Callable[[str], None] | None = None
    url: str | None = None


@dataclass
class FeedHandle:
    """An opened feed: lazy text openers plus per-file routing metadata."""

    openers: LazyOpeners = field(default_factory=dict)
    meta: dict[str, FeedFileMeta] = field(default_factory=dict)


@contextmanager
def _materialized_path(
    meta: FeedFileMeta | None,
) -> Generator[str | None, None, None]:
    """Yield a real on-disk path *or* a remote URL for a feed file (for DuckDB),
    or None.

    Remote files expose their direct URL, which DuckDB reads in place via its
    ``httpfs`` extension (HTTP range requests) — nothing is downloaded by us.
    Directory feeds expose their original path directly (no copy). Zip members
    are streamed to a temporary file that is removed on exit, so nothing is left
    on disk after processing.
    """
    if meta is None:
        yield None
        return
    if meta.url is not None:
        yield meta.url
        return
    if meta.local_path is not None:
        yield meta.local_path
        return
    if meta.materialize is None:
        yield None
        return
    fd, tmp_name = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        meta.materialize(tmp_name)
        yield tmp_name
    finally:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()


# ---------------------------------------------------------------------------
# Feed opener
# ---------------------------------------------------------------------------


@contextmanager
def _open_feed(
    path: str | Path, files: Iterable[str] | None = None
) -> Generator[FeedHandle, None, None]:
    """Open a GTFS feed (zip archive, directory, or HTTP folder URL).

    Yields a :class:`FeedHandle` whose ``openers`` map each ``.txt`` name to a
    zero-arg callable returning a utf-8-sig text stream (callers close it), and
    whose ``meta`` carries size / materialization info used to route large files
    to the DuckDB backend.

    Supports:
    * ``.zip`` archives (files at root *or* inside a single sub-directory).
    * Plain directories containing ``.txt`` files.
    * ``http(s)://`` folder URLs (e.g. a public GCP bucket folder). Remote
      folders cannot be listed, so *files* must be supplied: it is the
      authoritative list of ``.txt`` names to compare. Each name is probed with
      an HTTP request to determine presence (so added/deleted files are still
      detected) and fetched lazily.

    Args:
        path:  Feed location — a path to a zip/directory, or an ``http(s)://``
               folder URL.
        files: Optional explicit list of file names to consider. For local
               feeds this *filters* the discovered files to just these names;
               for remote URL feeds it is *required* and authoritative.
    """
    requested = list(files) if files is not None else None

    if _is_url(path):
        yield _open_remote_feed(str(path), requested)
        return

    path = Path(path)

    if path.is_dir():
        openers: LazyOpeners = {}
        meta: dict[str, FeedFileMeta] = {}
        for txt_file in sorted(path.glob("*.txt")):

            def _make_opener(p: Path) -> Callable[[], TextIO]:
                return lambda: p.open(encoding="utf-8-sig")

            def _make_materialize(p: Path) -> Callable[[str], None]:
                def _materialize(dest: str) -> None:
                    with p.open("rb") as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)

                return _materialize

            openers[txt_file.name] = _make_opener(txt_file)
            try:
                size: int | None = txt_file.stat().st_size
            except OSError:
                size = None
            meta[txt_file.name] = FeedFileMeta(
                size=size,
                local_path=str(txt_file),
                materialize=_make_materialize(txt_file),
            )
        yield _filter_handle(FeedHandle(openers, meta), requested)

    elif zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path, "r")
        try:
            # Accept both root-level files and files inside exactly one subdirectory.
            name_map: dict[str, str] = {}
            for member in zf.namelist():
                if member.endswith(".txt"):
                    basename = member.rsplit("/", 1)[-1]
                    if basename:
                        name_map[basename] = member

            openers = {}
            meta = {}
            for basename, internal_path in name_map.items():

                def _make_opener(ip: str) -> Callable[[], TextIO]:  # type: ignore[misc]
                    return lambda: io.TextIOWrapper(zf.open(ip), encoding="utf-8-sig")

                def _make_materialize(ip: str) -> Callable[[str], None]:
                    def _materialize(dest: str) -> None:
                        with zf.open(ip, "r") as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)

                    return _materialize

                openers[basename] = _make_opener(internal_path)
                try:
                    zsize: int | None = zf.getinfo(internal_path).file_size
                except KeyError:
                    zsize = None
                meta[basename] = FeedFileMeta(
                    size=zsize,
                    local_path=None,
                    materialize=_make_materialize(internal_path),
                )
            yield _filter_handle(FeedHandle(openers, meta), requested)
        finally:
            zf.close()

    else:
        raise ValueError(
            f"Unsupported feed path: {path!r}.  Must be a .zip file, a directory, "
            f"or an http(s):// folder URL."
        )


def _filter_handle(handle: FeedHandle, requested: list[str] | None) -> FeedHandle:
    """Restrict a feed handle's openers/meta to the requested file names (if any)."""
    if requested is None:
        return handle
    wanted = set(requested)
    return FeedHandle(
        openers={n: o for n, o in handle.openers.items() if n in wanted},
        meta={n: m for n, m in handle.meta.items() if n in wanted},
    )


# ---------------------------------------------------------------------------
# Remote (HTTP folder URL) feed support
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT_SECONDS = 30


def _join_url(base_url: str, name: str) -> str:
    """Join a folder *base_url* and a file *name* into a single object URL."""
    return base_url.rstrip("/") + "/" + name.lstrip("/")


def _http_exists(url: str) -> bool:
    """Return True if *url* points at a fetchable object, False if it is absent.

    Tries a cheap ``HEAD`` first, falling back to a single-byte ranged ``GET``
    when the server rejects ``HEAD``.

    Object stores such as Google Cloud Storage are common hosts for GTFS feeds,
    where the *folder* is often private (no list permission) even though the
    individual files are public. In that configuration a request for a file
    that does **not** exist returns ``403 Forbidden`` (the server will not
    confirm or deny existence without list permission) rather than ``404``.
    We therefore treat 401/403/404/410 as "absent / not fetchable" instead of
    raising, so probing a missing file simply skips it (and is reported as an
    added/deleted file when appropriate).
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return False
        # 403/401 may mean "missing in a private folder" *or* "HEAD not allowed
        # on an existing object"; 405/501 mean HEAD is unsupported. A ranged GET
        # disambiguates: it succeeds for a real (public) file and fails for a
        # missing one.
        if exc.code in (401, 403, 405, 501):
            return _http_exists_via_get(url)
        raise


def _http_exists_via_get(url: str) -> bool:
    """Fallback existence check using a ranged ``GET`` for servers without HEAD.

    Treats 401/403/404/410 as "absent / not fetchable" (see :func:`_http_exists`
    for why a missing file in a private folder reports ``403``).
    """
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            return False
        raise


def _http_get_text(url: str) -> TextIO:
    """Fetch *url* and return its body as a utf-8-sig decoded text stream."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        data = resp.read()
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig")


def _http_content_length(url: str) -> int | None:
    """Return the ``Content-Length`` of *url* (bytes), or None if unavailable.

    Used only as a cheap size hint for routing large files to DuckDB; any
    failure simply yields ``None`` (treated as "unknown size").
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length is not None else None
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _http_stream_to_file(url: str, dest: str) -> None:
    """Stream *url* to *dest* on disk with bounded memory (for the DuckDB path)."""
    req = urllib.request.Request(url, method="GET")
    with (
        urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp,
        open(dest, "wb") as out,
    ):
        shutil.copyfileobj(resp, out)


def _open_remote_feed(base_url: str, files: list[str] | None) -> FeedHandle:
    """Build a feed handle for an HTTP folder URL.

    Remote folders cannot be listed, so the file names to fetch are determined
    up front: either the caller-supplied *files* (authoritative) or, when none
    are given, every known GTFS file (:data:`gtfs_definitions.SUPPORTED_FILES`).
    Each candidate name is probed for presence (so missing files are correctly
    treated as added/deleted) and fetched lazily only when its opener is called.
    """
    candidates = list(files) if files else sorted(SUPPORTED_FILES)

    openers: LazyOpeners = {}
    meta: dict[str, FeedFileMeta] = {}
    for name in candidates:
        file_url = _join_url(base_url, name)
        if not _http_exists(file_url):
            continue

        def _make_opener(u: str) -> Callable[[], TextIO]:
            return lambda: _http_get_text(u)

        def _make_materialize(u: str) -> Callable[[str], None]:
            return lambda dest: _http_stream_to_file(u, dest)

        openers[name] = _make_opener(file_url)
        meta[name] = FeedFileMeta(
            size=_http_content_length(file_url),
            local_path=None,
            materialize=_make_materialize(file_url),
            url=file_url,
        )
    return FeedHandle(openers, meta)
