"""Bounded downloads and selective torrents staged outside the library."""

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urljoin
from uuid import uuid4

import httpx

from config import (
    PROVIDER_DOWNLOAD_PATH,
    PROVIDER_QBITTORRENT_PASSWORD,
    PROVIDER_QBITTORRENT_SAVE_PATH,
    PROVIDER_QBITTORRENT_URL,
    PROVIDER_QBITTORRENT_USERNAME,
)
from handler.providers.minerva import INDEX_ROOT, safe_torrent_path
from handler.providers.sources import download_url
from utils.context import create_httpx_client


def filename_safe(name: str) -> str:
    if (
        not name
        or name in (".", "..")
        or name != Path(name).name
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        or name.endswith((".", " "))
    ):
        raise ValueError("The provider filename is unsafe for this library")
    if name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        raise ValueError("The provider filename is reserved")
    if len(name.encode()) > 240:
        raise ValueError("The provider filename is too long")
    return name


def http_download(
    url: str,
    target: Path,
    report: Callable,
    *,
    expected_size: int | None = None,
    maximum: int = 1024**4,
    trusted_index: bool = False,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = target.with_name(target.name + ".http.json")
    identity = hashlib.sha256(url.encode()).hexdigest()
    metadata = {}
    try:
        metadata = json.loads(metadata_path.read_text())
    except OSError, ValueError:
        pass
    etag = metadata.get("etag", "") if metadata.get("source") == identity else ""
    offset = (
        target.stat().st_size
        if target.exists() and etag and not etag.startswith("W/")
        else 0
    )
    with create_httpx_client() as client:
        # Validate each redirect; the shared transport also pins public DNS addresses.
        for _ in range(6):
            if not trusted_index:
                download_url(url)
            headers = {"Range": f"bytes={offset}-", "If-Range": etag} if offset else {}
            with client.stream(
                "GET", url, headers=headers, follow_redirects=False, timeout=60
            ) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code in (412, 416) and offset:
                    offset = 0
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if (
                    "text/" in content_type
                    or "json" in content_type
                    or "html" in content_type
                ):
                    raise ValueError(
                        "This link returned a web page. Complete provider verification and use its generated download link"
                    )
                length = int(response.headers.get("content-length", 0))
                if response.status_code == 206:
                    content_range = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)",
                        response.headers.get("content-range", ""),
                    )
                    if (
                        not offset
                        or not content_range
                        or int(content_range[1]) != offset
                        or response.headers.get("etag") != etag
                    ):
                        raise ValueError(
                            "The server returned an inconsistent download range"
                        )
                    length = int(content_range[3])
                else:
                    offset = 0
                if (
                    length > maximum
                    or expected_size is not None
                    and length
                    and length != expected_size
                ):
                    raise ValueError("Download size does not match the selected file")
                if (
                    length
                    and shutil.disk_usage(target.parent).free
                    < length - offset + 64 * 1024**2
                ):
                    raise ValueError("Not enough free space for this download")
                disposition = response.headers.get("content-disposition", "")
                match = re.search(
                    r"filename\*=UTF-8''([^;]+)|filename=\"([^\"]+)\"|filename=([^;]+)",
                    disposition,
                    re.IGNORECASE,
                )
                name = (
                    unquote(next(v for v in match.groups() if v))
                    if match
                    else unquote(response.url.path.rsplit("/", 1)[-1])
                )
                metadata_path.write_text(
                    json.dumps(
                        {"source": identity, "etag": response.headers.get("etag", "")}
                    )
                )
                count = offset
                last_report = 0.0
                with target.open("ab" if offset else "wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if count == 0 and chunk.lstrip().lower().startswith(
                            (b"<!doctype html", b"<html")
                        ):
                            raise ValueError(
                                "This link returned a verification page, not a ROM"
                            )
                        count += len(chunk)
                        if (
                            count > maximum
                            or expected_size is not None
                            and count > expected_size
                        ):
                            raise ValueError("Download exceeded the expected size")
                        output.write(chunk)
                        if time.monotonic() - last_report > 1:
                            report(
                                phase="downloading",
                                completed_bytes=count,
                                total_bytes=length or expected_size,
                                progress=count / length if length else 0,
                            )
                            last_report = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                if (
                    not count
                    or length
                    and count != length
                    or expected_size is not None
                    and count != expected_size
                ):
                    raise ValueError("Download is incomplete")
                return name
        raise ValueError("Too many download redirects")


def selected_torrent_file(files: list[dict], entry: dict) -> dict:
    matches = [
        f
        for f in files
        if int(f["size"]) == entry["size"]
        and (
            f["name"] == entry["file_path"]
            or f["name"].split("/", 1)[-1] == entry["file_path"]
        )
    ]
    if len(matches) != 1:
        raise ValueError("Torrent file list does not match the catalog entry")
    return matches[0]


def torrent_download(entry: dict, report: Callable) -> Path:
    if not PROVIDER_QBITTORRENT_URL:
        raise ValueError("Configure the provider qBittorrent connection first")
    info_hash = entry["info_hash"]
    category = "romm-providers"
    download_root = Path(PROVIDER_DOWNLOAD_PATH)
    download_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(download_root).free < entry["size"] + 64 * 1024**2:
        raise ValueError("Not enough free space for this torrent file")
    # This origin is explicitly configured by the administrator, never supplied by a request.
    with httpx.Client(
        base_url=PROVIDER_QBITTORRENT_URL.rstrip("/") + "/api/v2/",
        timeout=30,
        trust_env=False,
    ) as client:
        response = client.post(
            "auth/login",
            data={
                "username": PROVIDER_QBITTORRENT_USERNAME,
                "password": PROVIDER_QBITTORRENT_PASSWORD,
            },
        )
        response.raise_for_status()
        if response.text.strip() != "Ok.":
            raise ValueError("qBittorrent authentication failed")

        def api(path: str, **data):
            response = client.post(path, data=data)
            response.raise_for_status()
            return response

        def stop():
            response = client.post("torrents/stop", data={"hashes": info_hash})
            if response.status_code == 404:
                api("torrents/pause", hashes=info_hash)
            else:
                response.raise_for_status()

        existing = client.get("torrents/info", params={"hashes": info_hash})
        existing.raise_for_status()
        if existing.json():
            if any(t["category"] != category for t in existing.json()):
                raise ValueError(
                    "This torrent is already managed outside RomM; it was left unchanged"
                )
            stop()
        else:
            (
                api(
                    "torrents/createCategory",
                    category=category,
                    savePath=PROVIDER_QBITTORRENT_SAVE_PATH,
                )
                if not client.get("torrents/categories").json().get(category)
                else None
            )
            data = (INDEX_ROOT / "torrents" / f"{info_hash}.torrent").read_bytes()
            response = client.post(
                "torrents/add",
                data={
                    "savepath": PROVIDER_QBITTORRENT_SAVE_PATH,
                    "category": category,
                    "paused": "true",
                    "stopped": "true",
                    "autoTMM": "false",
                    "contentLayout": "Original",
                },
                files={
                    "torrents": ("manifest.torrent", data, "application/x-bittorrent")
                },
            )
            response.raise_for_status()
            if response.text.strip() != "Ok.":
                raise ValueError("qBittorrent rejected the torrent")
        try:
            deadline = time.monotonic() + 60
            files: list[dict] = []
            while not files:
                report(phase="metadata")
                response = client.get("torrents/files", params={"hash": info_hash})
                if response.status_code != 404:
                    response.raise_for_status()
                    files = response.json()
                if time.monotonic() > deadline:
                    raise ValueError("qBittorrent did not load the torrent metadata")
                if not files:
                    time.sleep(1)
            selected = selected_torrent_file(files, entry)
            api(
                "torrents/filePrio",
                hash=info_hash,
                id="|".join(str(f["index"]) for f in files),
                priority=0,
            )
            api(
                "torrents/filePrio",
                hash=info_hash,
                id=str(selected["index"]),
                priority=7,
            )
            response = client.post("torrents/start", data={"hashes": info_hash})
            if response.status_code == 404:
                api("torrents/resume", hashes=info_hash)
            else:
                response.raise_for_status()
            deadline = time.monotonic() + 7 * 86400
            while True:
                info = client.get("torrents/info", params={"hashes": info_hash})
                info.raise_for_status()
                if any(t.get("state") == "error" for t in info.json()):
                    raise ValueError(
                        "qBittorrent cannot access its download files. Check save-directory permissions and volume mappings, then retry"
                    )
                response = client.get("torrents/files", params={"hash": info_hash})
                response.raise_for_status()
                selected = selected_torrent_file(response.json(), entry)
                progress = float(selected["progress"])
                report(
                    phase="downloading",
                    progress=progress,
                    completed_bytes=int(entry["size"] * progress),
                    total_bytes=entry["size"],
                )
                if progress >= 1:
                    break
                if time.monotonic() > deadline:
                    raise ValueError(
                        "Torrent timed out; retry retains its completed pieces"
                    )
                time.sleep(2)
            path = (
                Path(PROVIDER_DOWNLOAD_PATH) / safe_torrent_path(selected["name"])
            ).resolve()
            if (
                not path.is_relative_to(Path(PROVIDER_DOWNLOAD_PATH).resolve())
                or not path.is_file()
                or path.stat().st_size != entry["size"]
            ):
                raise ValueError(
                    "The download volume mapping does not match qBittorrent"
                )
            return path
        finally:
            stop()


def publish_file(source: Path, destination: Path, report: Callable) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(
            "A ROM with this filename already exists; nothing was overwritten"
        )
    size = source.stat().st_size
    if shutil.disk_usage(destination.parent).free < size + 64 * 1024**2:
        raise ValueError("Not enough library disk space")
    temporary = destination.with_name("." + uuid4().hex + ".provider-part")
    claimed = False
    try:
        with source.open("rb") as incoming, temporary.open("xb") as output:
            copied = 0
            while chunk := incoming.read(1024 * 1024):
                output.write(chunk)
                copied += len(chunk)
                report(
                    phase="importing",
                    completed_bytes=copied,
                    total_bytes=size,
                    progress=copied / size,
                )
            output.flush()
            os.fsync(output.fileno())
        os.close(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        claimed = True
        temporary.replace(destination)
    except BaseException:
        if claimed:
            destination.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
