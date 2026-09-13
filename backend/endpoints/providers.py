from typing import Annotated

import httpx
from fastapi import HTTPException, Query, Request
from rq.exceptions import NoSuchJobError
from rq.job import Job
from starlette.concurrency import run_in_threadpool

from config import PROVIDER_QBITTORRENT_URL, ROM_PROVIDERS_ENABLED
from decorators.auth import protected_route
from endpoints.responses.providers import (
    ProviderFilesRequest,
    ProviderId,
    ProviderImportRequest,
    ProviderJob,
    ProviderMegaFile,
    ProviderResult,
    ProviderSearch,
    ProviderStatus,
)
from handler.auth.constants import Scope
from handler.auth.dependencies import assert_admin
from handler.database import db_platform_handler
from handler.providers import jobs, minerva, sources
from handler.redis_handler import async_cache, redis_client
from utils.router import APIRouter

router = APIRouter(prefix="/providers", tags=["providers"])


def enabled(request: Request) -> None:
    assert_admin(request)
    if not ROM_PROVIDERS_ENABLED:
        raise HTTPException(503, "ROM providers are disabled by the administrator")


async def resolve(result_id: str) -> ProviderResult:
    if result_id.startswith("minerva:"):
        try:
            return await run_in_threadpool(minerva.get_result, result_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "Minerva entry unavailable; search again") from exc
    raw = await async_cache.get(f"provider:result:{result_id}")
    if not raw:
        raise HTTPException(404, "Search result expired; search again")
    return ProviderResult.model_validate_json(raw)


@protected_route(router.get, "/status", [Scope.TASKS_RUN])
async def get_status(request: Request) -> ProviderStatus:
    assert_admin(request)
    state = await run_in_threadpool(minerva.index_status)
    return ProviderStatus(
        enabled=ROM_PROVIDERS_ENABLED,
        torrent_configured=bool(PROVIDER_QBITTORRENT_URL),
        jobs=await run_in_threadpool(jobs.list_jobs),
        **state,
    )


@protected_route(router.get, "/search", [Scope.TASKS_RUN])
async def search(
    request: Request,
    provider: ProviderId = "minerva",
    query: Annotated[str, Query(max_length=200)] = "",
    platform: Annotated[str, Query(max_length=100)] = "",
    region: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ProviderSearch:
    enabled(request)
    try:
        if provider == "minerva":
            return await run_in_threadpool(
                minerva.search_index, query, platform, region, page, limit
            )
        return await sources.search(provider, query, platform, region, page, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, "Provider unavailable or rate limited; try again later"
        ) from exc


@protected_route(router.post, "/index", [Scope.TASKS_RUN])
async def refresh_index(request: Request) -> ProviderJob:
    enabled(request)
    if any(
        j.kind == "index" and j.state in ("queued", "started")
        for j in await run_in_threadpool(jobs.list_jobs)
    ):
        raise HTTPException(409, "An index refresh is already queued or running")
    return await run_in_threadpool(jobs.enqueue, "index", "Minerva", {})


@protected_route(router.post, "/files", [Scope.TASKS_RUN])
async def mega_files(
    request: Request, body: ProviderFilesRequest
) -> list[ProviderMegaFile]:
    enabled(request)
    result = await resolve(body.result_id)
    if body.option >= len(result.options):
        raise HTTPException(422, "Select a MEGA download option")
    selected = result.options[body.option]
    if selected.method != "mega" and not (
        selected.method == "verify" and body.verified_url
    ):
        raise HTTPException(422, "Select a MEGA download option")
    from handler.providers.mega import listing

    try:
        files = await run_in_threadpool(
            listing,
            (body.verified_url or "") if selected.method == "verify" else selected.url,
        )
        return [
            ProviderMegaFile(id=f["id"], name=f["name"], size=f["size"]) for f in files
        ]
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(
            502, "MEGA folder unavailable; check the source link and try again"
        ) from exc


@protected_route(router.post, "/downloads", [Scope.TASKS_RUN, Scope.ROMS_WRITE])
async def download(request: Request, body: ProviderImportRequest) -> ProviderJob:
    enabled(request)
    result = await resolve(body.result_id)
    if body.option >= len(result.options):
        raise HTTPException(422, "Download option does not exist")
    if not db_platform_handler.get_platform(body.platform_id):
        raise HTTPException(404, "Destination platform does not exist")
    selected = result.options[body.option]
    if selected.method == "torrent" and not PROVIDER_QBITTORRENT_URL:
        raise HTTPException(422, "Configure provider qBittorrent before downloading")
    if selected.method == "verify" and not body.verified_url:
        raise HTTPException(
            422, "Supply the generated download URL after provider verification"
        )
    if body.verified_url:
        if selected.method != "verify":
            raise HTTPException(422, "This provider does not need a replacement URL")
        try:
            sources.download_url(body.verified_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return await run_in_threadpool(
        jobs.enqueue,
        "download",
        result.name,
        {**body.model_dump(), "result": result.model_dump()},
    )


@protected_route(router.post, "/jobs/{job_id}/cancel", [Scope.TASKS_RUN])
async def cancel(request: Request, job_id: str) -> None:
    enabled(request)
    try:
        await run_in_threadpool(jobs.cancel, job_id)
    except (ValueError, NoSuchJobError) as exc:
        raise HTTPException(404, "Provider job not found") from exc


@protected_route(
    router.post, "/jobs/{job_id}/retry", [Scope.TASKS_RUN, Scope.ROMS_WRITE]
)
async def retry(request: Request, job_id: str) -> ProviderJob:
    enabled(request)
    try:
        job = await run_in_threadpool(Job.fetch, job_id, connection=redis_client)
        if not job.meta.get("provider_job"):
            raise HTTPException(404, "Provider job not found")
        if jobs.status(job).state not in ("failed", "cancelled", "stopped"):
            raise HTTPException(409, "Only failed or cancelled jobs can be retried")
        return await run_in_threadpool(
            jobs.enqueue, job.meta["kind"], job.meta["name"], job.args[1]
        )
    except NoSuchJobError as exc:
        raise HTTPException(404, "Provider job expired") from exc
