"""Persistent provider jobs on RomM's existing RQ workers."""

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job

from config import PROVIDER_DOWNLOAD_PATH
from endpoints.responses.providers import ProviderJob, ProviderResult
from handler.providers import minerva
from handler.providers.downloads import (
    filename_safe,
    http_download,
    publish_file,
    torrent_download,
)
from handler.redis_handler import low_prio_queue, redis_client
from utils.context import create_httpx_client, initialize_context

JOB_LIST = "provider:jobs"


class Cancelled(Exception):
    pass


def enqueue(kind: str, name: str, payload: dict) -> ProviderJob:
    with redis_client.lock("provider:enqueue", timeout=15, blocking_timeout=5):
        pending = [
            job for job in list_jobs() if job.state in ("queued", "started", "deferred")
        ]
        if len(pending) >= 99:
            raise ValueError("Provider queue is full; wait for an import to finish")
        previous = [job.id for job in pending if job.kind == kind]
        # ponytail: serialize downloads; per-torrent chains if parallel imports become necessary.
        dependency = Dependency(previous[0], allow_failure=True) if previous else None
        job = low_prio_queue.enqueue(
            run,
            kind,
            payload,
            depends_on=dependency,
            description=f"Provider {kind}",
            job_timeout=8 * 86400,
            result_ttl=7 * 86400,
            failure_ttl=7 * 86400,
            meta={"provider_job": True, "kind": kind, "name": name, "phase": "queued"},
        )
        redis_client.lpush(JOB_LIST, job.id)
        redis_client.ltrim(JOB_LIST, 0, 99)
    return status(job)


def status(job: Job) -> ProviderJob:
    meta = job.get_meta()
    actual = job.get_status().value
    state = (
        actual
        if actual in ("failed", "stopped", "canceled")
        else meta.get("state", actual)
    )
    if state == "canceled":
        state = "cancelled"
    return ProviderJob(
        id=job.id,
        state=state,
        **{
            key: meta[key]
            for key in (
                "kind",
                "name",
                "phase",
                "progress",
                "completed_bytes",
                "total_bytes",
                "records",
                "error",
                "rom_id",
            )
            if key in meta
        },
    )


def list_jobs() -> list[ProviderJob]:
    jobs = []
    for identity in redis_client.lrange(JOB_LIST, 0, 99):
        try:
            jobs.append(status(Job.fetch(identity.decode(), connection=redis_client)))
        except NoSuchJobError:
            continue
    return jobs


def cancel(job_id: str) -> None:
    job = Job.fetch(job_id, connection=redis_client)
    if not job.meta.get("provider_job"):
        raise ValueError("Unknown provider job")
    redis_client.setex(f"provider:cancel:{job.id}", 8 * 86400, "1")
    # Let the worker finish cancelled jobs normally so RQ releases their dependents.


def sync_index(report) -> None:
    minerva.INDEX_ROOT.mkdir(parents=True, exist_ok=True)
    with create_httpx_client() as client:
        response = client.get(minerva.MINERVA_CDN, timeout=30)
        response.raise_for_status()
        links = [
            unquote(link)
            for link in re.findall(
                r'href="([^"/]+\.zip)"', response.text, re.IGNORECASE
            )
        ]
    candidates = [
        (int(match[1]), link)
        for link in links
        if (match := re.fullmatch(r"Minerva_Myrient - ALL - v(\d+)\.zip", link))
    ]
    if not candidates:
        raise ValueError("Minerva did not publish a manifest bundle")
    _, latest = max(candidates)
    bundle = minerva.INDEX_ROOT / latest
    if not bundle.exists():
        temporary = bundle.with_suffix(".part")
        http_download(
            urljoin(minerva.MINERVA_CDN, latest),
            temporary,
            report,
            maximum=minerva.MAX_INDEX_BYTES,
            trusted_index=True,
        )
        temporary.replace(bundle)
    minerva.build_index(bundle, report)


@initialize_context()
async def register_rom(platform_id: int, name: str, fs_path: str) -> int:
    from handler.database import db_platform_handler, db_rom_handler
    from handler.filesystem import fs_rom_handler
    from handler.rom_files import refresh_rom_files
    from models.rom import Rom

    platform = db_platform_handler.get_platform(platform_id)
    if not platform:
        raise ValueError("Destination platform no longer exists")
    tags = fs_rom_handler.parse_tags(name)
    existing = db_rom_handler.get_roms_by_fs_name(
        platform_id=platform.id, fs_names=[name]
    )
    rom = next((r for r in existing.values() if r.fs_path == fs_path), None)
    if rom is None:
        rom = db_rom_handler.add_rom(
            Rom(
                platform_id=platform.id,
                fs_name=name,
                fs_path=fs_path,
                name=fs_rom_handler.get_file_name_with_no_tags(name),
                regions=tags.regions,
                languages=tags.languages,
                revision=tags.revision,
                version=tags.version,
                tags=tags.other_tags,
            )
        )
    await refresh_rom_files(rom)
    return rom.id


def import_rom(payload: dict, report) -> None:
    from handler.database import db_platform_handler
    from handler.filesystem import fs_rom_handler

    result = ProviderResult.model_validate(payload["result"])
    selected = result.options[payload["option"]]
    platform = db_platform_handler.get_platform(payload["platform_id"])
    if not platform:
        raise ValueError("Destination platform no longer exists")
    fs_path = fs_rom_handler.get_roms_upload_path(platform.fs_slug)
    directory = fs_rom_handler.validate_path(fs_path)
    directory.mkdir(parents=True, exist_ok=True)
    staging = Path(PROVIDER_DOWNLOAD_PATH) / "http" / result.id.replace(":", "_")
    if selected.method == "torrent":
        entry = minerva.get_entry(result.id)
        name = filename_safe(entry["filename"])
        if (directory / name).exists():
            raise FileExistsError("A ROM with this filename already exists")
        source = torrent_download(entry, report)
    elif selected.method == "mega" or urlsplit(
        payload.get("verified_url") or ""
    ).hostname in ("mega.nz", "mega.co.nz"):
        from handler.providers.mega import download

        source, name = download(
            payload.get("verified_url") or selected.url,
            payload.get("mega_node_id"),
            staging,
            report,
        )
        name = filename_safe(name)
    else:
        url = payload.get("verified_url") or selected.url
        if selected.method == "verify" and not payload.get("verified_url"):
            raise ValueError(
                "Complete provider verification and supply its generated download link"
            )
        name = http_download(url, staging, report, expected_size=selected.size)
        name = filename_safe(selected.filename or name)
        source = staging
    destination = directory / name
    publish_file(source, destination, report)
    report(phase="registering", import_committed=True)
    try:
        rom_id = asyncio.run(register_rom(platform.id, name, fs_path))
    except Exception as exc:
        raise ValueError(
            "File imported safely, but registration failed. Run a library scan to register it"
        ) from exc
    report(phase="completed", progress=1, rom_id=rom_id)
    if source == staging:
        source.unlink(missing_ok=True)


def run(kind: str, payload: dict) -> None:
    job = get_current_job()
    if job is None:
        raise RuntimeError("Provider jobs require an RQ worker")
    last_save = 0.0

    def report(**values):
        nonlocal last_save
        if (
            not values.get("import_committed")
            and not job.meta.get("import_committed")
            and redis_client.exists(f"provider:cancel:{job.id}")
        ):
            raise Cancelled()
        changed = values.get("phase") != job.meta.get("phase")
        job.meta.update(values)
        if changed or time.monotonic() - last_save > 1:
            job.save_meta()
            last_save = time.monotonic()

    job.meta.update(state="started")
    job.save_meta()
    try:
        report(phase="starting")
        sync_index(report) if kind == "index" else import_rom(payload, report)
        job.meta.update(state="finished", phase="completed", progress=1)
    except Cancelled:
        job.meta.update(state="cancelled", phase="cancelled")
    except Exception as exc:  # noqa: BLE001
        # HTTP errors may contain signed URLs. Keep those out of API responses and RQ logs.
        message = (
            str(exc)
            if isinstance(exc, (ValueError, FileExistsError))
            else f"Provider operation failed ({type(exc).__name__}). Check configuration and retry"
        )
        job.meta.update(state="failed", error=message)
    finally:
        job.save_meta()
