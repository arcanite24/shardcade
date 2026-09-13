"""Search an atomic SQLite FTS index built from Minerva's torrent manifests."""

import hashlib
import os
import re
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from config import RESOURCES_BASE_PATH
from endpoints.responses.providers import ProviderOption, ProviderResult, ProviderSearch
from handler.providers.platforms import (
    infer_platform,
    normalize,
    region_from_filename,
    title_from_filename,
)

MINERVA_CDN = "https://cdn.minerva-archive.org/torrents/"
INDEX_ROOT = Path(RESOURCES_BASE_PATH) / "providers" / "minerva"
MAX_TORRENT_BYTES = 128 * 1024 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024 * 1024


def safe_torrent_path(value: str) -> str:
    value = value.removeprefix("./")
    if not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid torrent path")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Unsafe torrent path")
    return "/".join(parts)


def parse_torrent(data: bytes) -> tuple[str, str, list[tuple[str, int]]]:
    """Return the exact info-hash, root name and ordered file manifest."""
    if not data or len(data) > MAX_TORRENT_BYTES:
        raise ValueError("Torrent metadata exceeds its size limit")
    position = 0
    nodes = 0
    info_span: tuple[int, int] | None = None

    def decode(depth: int = 0) -> Any:
        nonlocal position, nodes, info_span
        nodes += 1
        if depth > 32 or nodes > 4_000_000 or position >= len(data):
            raise ValueError("Invalid or excessively nested torrent")
        marker = data[position : position + 1]
        if marker == b"i":
            end = data.find(b"e", position + 1)
            raw = data[position + 1 : end]
            if end < 0 or not re.fullmatch(rb"-?(0|[1-9][0-9]{0,18})", raw):
                raise ValueError("Invalid bencoded integer")
            position = end + 1
            return int(raw)
        if marker in (b"l", b"d"):
            position += 1
            result: Any = [] if marker == b"l" else {}
            while data[position : position + 1] != b"e":
                if marker == b"l":
                    result.append(decode(depth + 1))
                else:
                    key = decode(depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError("Invalid torrent dictionary")
                    start = position
                    result[key] = decode(depth + 1)
                    if depth == 0 and key == b"info":
                        info_span = (start, position)
            position += 1
            return result
        colon = data.find(b":", position, position + 12)
        raw = data[position:colon]
        if colon < 0 or not raw.isdigit():
            raise ValueError("Invalid bencoded string")
        size = int(raw)
        position = colon + 1
        if position + size > len(data):
            raise ValueError("Truncated torrent")
        result = data[position : position + size]
        position += size
        return result

    root = decode()
    if position != len(data) or not info_span or not isinstance(root, dict):
        raise ValueError("Torrent has no valid info dictionary")
    info = root[b"info"]
    if not isinstance(info, dict):
        raise ValueError("Invalid torrent info")  # noqa: TRY004
    raw_name = info.get(b"name.utf-8", info.get(b"name", b""))
    if not isinstance(raw_name, bytes):
        raise ValueError("Invalid torrent name")  # noqa: TRY004
    name = safe_torrent_path(raw_name.decode("utf-8"))
    files = []
    for item in info.get(
        b"files", [{b"path": [name.encode()], b"length": info.get(b"length")}]
    ):
        if not isinstance(item, dict):
            raise ValueError("Invalid torrent file record")  # noqa: TRY004
        path_parts = item.get(b"path.utf-8", item.get(b"path"))
        if (
            not isinstance(path_parts, list)
            or not path_parts
            or not all(isinstance(p, bytes) for p in path_parts)
        ):
            raise ValueError("Torrent file is missing its path")
        try:
            path = safe_torrent_path("/".join(p.decode("utf-8") for p in path_parts))
        except ValueError, UnicodeDecodeError:
            # Keep the position: removing a bad path shifts every later torrent index.
            path = ""
        size = item.get(b"length")
        if not isinstance(size, int) or size < 0:
            raise ValueError("Torrent file is missing its size")
        files.append((path, size))
    return (
        hashlib.sha1(data[slice(*info_span)], usedforsecurity=False).hexdigest(),
        name,
        files,
    )


@contextmanager
def _connect(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro" if readonly else str(path), uri=readonly, timeout=15
    )
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, title_norm TEXT NOT NULL,
            filename TEXT NOT NULL, platform TEXT NOT NULL, region TEXT NOT NULL,
            collection TEXT NOT NULL, size INTEGER NOT NULL, file_path TEXT NOT NULL,
            info_hash TEXT NOT NULL, file_index INTEGER NOT NULL, torrent_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manifests (name TEXT PRIMARY KEY, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
            title, filename, collection, content='entries', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2', prefix='2 3 4'
        );
        CREATE INDEX IF NOT EXISTS entries_platform ON entries(platform);
        CREATE INDEX IF NOT EXISTS entries_title ON entries(title_norm);
    """)


def build_index(
    bundle: Path, report: Callable[..., None], root: Path = INDEX_ROOT
) -> int:
    """Checkpoint manifests in a staging database; publish only a complete index."""
    root.mkdir(parents=True, exist_ok=True)
    torrents = root / "torrents"
    torrents.mkdir(exist_ok=True)
    staging = root / "index.building.sqlite"
    with zipfile.ZipFile(bundle) as archive, _connect(staging) as db:
        _create_schema(db)
        members = [m for m in archive.infolist() if m.filename.endswith(".torrent")]
        signature = hashlib.sha256(
            "\n".join(f"{m.filename}:{m.CRC}:{m.file_size}" for m in members).encode()
        ).hexdigest()
        previous = db.execute(
            "SELECT value FROM metadata WHERE key='source'"
        ).fetchone()
        if previous and previous[0] != signature:
            db.executescript(
                "DELETE FROM entries; DELETE FROM manifests; DELETE FROM metadata;"
            )
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('source', ?)", (signature,))
        db.commit()
        if not members or sum(m.file_size for m in members) > 4 * MAX_INDEX_BYTES:
            raise ValueError("Minerva manifest bundle is empty or exceeds its limit")
        completed = {row[0] for row in db.execute("SELECT name FROM manifests")}
        for number, member in enumerate(members):
            if member.file_size > MAX_TORRENT_BYTES:
                raise ValueError("Minerva torrent exceeds its metadata limit")
            if member.filename in completed:
                continue
            data = archive.read(member)
            info_hash, _, files = parse_torrent(data)
            torrent_name = PurePosixPath(member.filename).name
            (torrents / f"{info_hash}.torrent").write_bytes(data)
            collection = torrent_name.removesuffix(".torrent").removeprefix(
                "Minerva_Myrient - "
            )
            rows = []
            for index, (path, size) in enumerate(files):
                filename = PurePosixPath(path).name
                if (
                    not path
                    or size == 0
                    or filename.startswith(".")
                    or "_____padding_file" in path
                ):
                    continue
                title = title_from_filename(filename)
                platform = infer_platform(
                    f"{collection} {PurePosixPath(path).parent!s}"
                )
                rows.append(
                    (
                        f"minerva:{info_hash}:{index}",
                        title,
                        normalize(title),
                        filename,
                        platform,
                        region_from_filename(filename),
                        collection,
                        size,
                        path,
                        info_hash,
                        index,
                        torrent_name,
                    )
                )
            db.executemany(
                "INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            db.execute(
                "INSERT OR REPLACE INTO manifests VALUES (?,?)",
                (member.filename, info_hash),
            )
            db.commit()
            report(
                phase="indexing",
                progress=(number + 1) / len(members),
                records=db.execute("SELECT count(*) FROM entries").fetchone()[0],
            )
        count = db.execute("SELECT count(*) FROM entries").fetchone()[0]
        if count == 0:
            raise ValueError("No files found in Minerva manifest bundle")
        report(phase="optimizing", progress=0.99, records=count)
        db.execute("INSERT INTO search(search) VALUES('rebuild')")
        db.execute("INSERT INTO search(search) VALUES('optimize')")
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('indexed_at', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        db.commit()
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Minerva index integrity check failed")
    os.replace(staging, root / "index.sqlite")
    return count


def index_status(root: Path = INDEX_ROOT) -> dict:
    if not (root / "index.sqlite").exists():
        return {"index_ready": False, "records": 0, "platforms": []}
    with _connect(root / "index.sqlite", readonly=True) as db:
        return {
            "index_ready": True,
            "records": db.execute("SELECT count(*) FROM entries").fetchone()[0],
            "indexed_at": db.execute(
                "SELECT value FROM metadata WHERE key='indexed_at'"
            ).fetchone()[0],
            "platforms": [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT platform FROM entries WHERE platform != '' ORDER BY platform"
                )
            ],
        }


def _result(row: sqlite3.Row) -> ProviderResult:
    url = MINERVA_CDN + quote(row["torrent_name"])
    return ProviderResult(
        id=row["id"],
        provider="minerva",
        name=row["title"],
        platform=row["platform"],
        region=row["region"],
        collection=row["collection"],
        filename=row["filename"],
        size=row["size"],
        source_url=MINERVA_CDN,
        options=[
            ProviderOption(
                label="Minerva torrent",
                method="torrent",
                url=url,
                filename=row["filename"],
                size=row["size"],
            )
        ],
    )


def search_index(
    query: str,
    platform: str,
    region: str,
    page: int,
    limit: int,
    root: Path = INDEX_ROOT,
) -> ProviderSearch:
    if not 1 <= page <= 10000 or not 1 <= limit <= 100:
        raise ValueError("Invalid search pagination")
    if not (root / "index.sqlite").exists():
        raise ValueError("Sync the Minerva index before searching")
    words = re.findall(r"[^\W_]+", query, re.UNICODE)[:12]
    joins = "JOIN search ON search.rowid = entries.rowid" if words else ""
    where, params = [], []
    if words:
        where.append("search MATCH ?")
        params.append(" AND ".join(f'"{word}"*' for word in words))
    if platform:
        where.append("platform = ?")
        params.append(platform)
    if region:
        where.append("region LIKE ? ESCAPE '\\'")
        params.append(
            "%"
            + region.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
    predicate = "WHERE " + " AND ".join(where) if where else ""
    order = (
        "bm25(search, 5.0, 1.0, 0.2), entries.id" if words else "title_norm, entries.id"
    )
    with _connect(root / "index.sqlite", readonly=True) as db:
        total = db.execute(
            f"SELECT count(*) FROM entries {joins} {predicate}", params
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT entries.* FROM entries {joins} {predicate} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, (page - 1) * limit],
        ).fetchall()
    return ProviderSearch(
        items=[_result(r) for r in rows], total=total, page=page, limit=limit
    )


def get_entry(result_id: str, root: Path = INDEX_ROOT) -> dict:
    with _connect(root / "index.sqlite", readonly=True) as db:
        row = db.execute("SELECT * FROM entries WHERE id=?", (result_id,)).fetchone()
        if row is None:
            raise ValueError("Minerva entry is no longer in the index; search again")
        return dict(row)


def get_result(result_id: str) -> ProviderResult:
    with _connect(INDEX_ROOT / "index.sqlite", readonly=True) as db:
        row = db.execute("SELECT * FROM entries WHERE id=?", (result_id,)).fetchone()
        if row is None:
            raise ValueError("Minerva entry is no longer in the index; search again")
        return _result(row)
