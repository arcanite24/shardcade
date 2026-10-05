"""Provider catalog adapters, with server-owned download descriptors."""

import hashlib
import html
import json
import re
from typing import Literal
from urllib.parse import unquote, urljoin, urlsplit
from uuid import UUID

from endpoints.responses.providers import (
    ProviderId,
    ProviderOption,
    ProviderResult,
    ProviderSearch,
)
from handler.providers.platforms import infer_platform, region_from_filename
from handler.redis_handler import async_cache
from utils.context import ctx_httpx_client

DOWNLOAD_HOSTS = (
    "edgeemu.net",
    "vimm.net",
    "vikingfile.com",
    "datavaults.co",
    "ddownload.com",
    "fileq.net",
    "1fichier.com",
    "mega.nz",
    "mega.co.nz",
    "romstime.com",
    "moondl.com",
)


def download_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("A public HTTPS download link is required")
    if not any(
        host == domain or host.endswith("." + domain) for domain in DOWNLOAD_HOSTS
    ) and not re.fullmatch(
        r"vikingfile\.[0-9a-f]{32}\.r2\.cloudflarestorage\.com", host
    ):
        raise ValueError("This download host is not supported")
    return value


def strip_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


async def fetch_text(
    url: str, *, data: dict | None = None, params: dict | None = None
) -> tuple[str, dict]:
    client = ctx_httpx_client.get()
    async with client.stream(
        "POST" if data else "GET",
        url,
        data=data,
        params=params,
        follow_redirects=True,
        timeout=30,
    ) as response:
        response.raise_for_status()
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 8 * 1024 * 1024:
                raise ValueError("Provider response exceeds its size limit")
        return chunks.decode("utf-8", errors="replace"), dict(response.headers)


def option(url: str, label: str = "") -> ProviderOption | None:
    try:
        download_url(url)
    except ValueError:
        return None
    host = urlsplit(url).hostname or ""
    method: Literal["mega", "http", "verify"] = (
        "mega"
        if host in ("mega.nz", "mega.co.nz")
        else "http"
        if host.endswith(("edgeemu.net", "vikingfile.com"))
        else "verify"
    )
    return ProviderOption(label=label or host, method=method, url=url)


def result(
    provider: ProviderId,
    name: str,
    source_url: str,
    options: list[ProviderOption],
    platform: str = "",
    filename: str = "",
) -> ProviderResult:
    identity = hashlib.sha256((source_url + "|" + name).encode()).hexdigest()[:32]
    return ProviderResult(
        id=f"{provider}:{identity}",
        provider=provider,
        name=strip_html(name),
        platform=platform or infer_platform(name),
        region=region_from_filename(name),
        source_url=source_url,
        filename=filename,
        options=options,
    )


def parse_edge(text: str) -> list[ProviderResult]:
    items = []
    for block in re.findall(
        r"<details\b.*?</details>", text, re.IGNORECASE | re.DOTALL
    ):
        link = re.search(
            r'<a\s+href=["\']([^"\']*/download/[^"\']+)["\']', block, re.IGNORECASE
        )
        if not link:
            continue
        url = urljoin("https://edgeemu.net", html.unescape(link[1]))
        candidate = option(url)
        if not candidate:
            continue
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        candidate.filename = filename
        title = re.search(r"<summary>(.*?)</summary>", block, re.IGNORECASE | re.DOTALL)
        platform = re.search(
            r"system:\s*<span>(.*?)</span>", block, re.IGNORECASE | re.DOTALL
        )
        items.append(
            result(
                "edgeemu",
                title[1] if title else filename,
                url,
                [candidate],
                infer_platform(strip_html(platform[1])) if platform else "",
                filename,
            )
        )
    return items


def parse_vimm(text: str) -> list[ProviderResult]:
    items = []
    for row in re.findall(r"<tr\b.*?</tr>", text, re.IGNORECASE | re.DOTALL):
        links = re.finditer(
            r'<a\b[^>]*href=\s*["\'](?:https://vimm.net)?(/vault/(\d+))["\'][^>]*>(.*?)</a>',
            row,
            re.IGNORECASE | re.DOTALL,
        )
        link = next((link for link in links if link[2] != "999999"), None)
        if not link:
            continue
        url = urljoin("https://vimm.net", link[1])
        items.append(
            result(
                "vimm",
                link[3],
                url,
                [ProviderOption(label="Vimm's Lair", method="verify", url=url)],
                infer_platform(strip_html(row)),
            )
        )
    return items


def romstime_data(text: str, field: str) -> list[dict]:
    payload = ""
    for frame in re.findall(
        r"self\.__next_f\.push\((\[.*?\])\)</script>", text, re.DOTALL
    ):
        value = json.loads(frame)
        if value[0] == 1 and isinstance(value[1], str):
            payload += value[1]
    match = re.search(r'"' + re.escape(field) + r'":\s*(?=\[)', payload)
    if not match:
        raise ValueError("RomsTime did not return its catalog")
    value, _ = json.JSONDecoder().raw_decode(payload[match.end() :])
    return value


def parse_romstime(text: str) -> list[ProviderResult]:
    items = []
    for game in romstime_data(text, "roms"):
        game_id = str(UUID(game["id"]))
        item = result(
            "romstime",
            game["title"],
            f"https://romstime.com/download/{game_id}",
            [],
            infer_platform(game.get("platform", "")),
        )
        item.region = (game.get("display_region") or "Unknown").replace(
            "United States", "USA"
        )
        item.size = game.get("computed_min_size_bytes")
        items.append(item)
    return items


async def romstime_options(item: ProviderResult) -> ProviderResult:
    game_id = str(UUID(urlsplit(item.source_url).path.rsplit("/", 1)[-1]))
    text, _ = await fetch_text(f"https://romstime.com/download/{game_id}")
    options = []
    for version in romstime_data(text, "regionalVersions"):
        version_id = str(UUID(version["id"]))
        for source in version.get("download_sources", []):
            if (
                source.get("status") != "active"
                or source.get("provider") not in ("moondl", "uploadg")
                or source.get("part_group")
                or source.get("part_number") is not None
                or source.get("extract_password")
            ):
                continue
            source_id = str(UUID(source["id"]))
            label = " · ".join(
                str(value)
                for value in (
                    version.get("region_code") or version.get("region_name"),
                    version.get("format"),
                    version.get("crypto_state"),
                    version.get("filename"),
                    source["provider"],
                )
                if value
            )
            options.append(
                ProviderOption(
                    label=label,
                    method="http",
                    url=f"https://romstime.com/api/roms/{game_id}/download?versionId={version_id}&sourceId={source_id}",
                )
            )
    if not options:
        raise ValueError(
            "RomsTime has no supported single-file download for this game; open the source"
        )
    item.options = options[:101]
    return item


async def search(
    provider: ProviderId, query: str, platform: str, region: str, page: int, limit: int
) -> ProviderSearch:
    if not query.strip() and provider != "startgame":
        return ProviderSearch(items=[], total=0, page=page, limit=limit)
    if provider == "edgeemu":
        text, _ = await fetch_text(
            "https://edgeemu.net/search.php", data={"search": query, "system": "all"}
        )
        items = parse_edge(text)
        remote_page = False
        total = len(items)
    elif provider == "vimm":
        text, _ = await fetch_text(
            "https://vimm.net/vault/", params={"p": "list", "q": query}
        )
        items = parse_vimm(text)
        if not items and ("Just a moment" in text or "challenge-platform" in text):
            raise ValueError(
                "Vimm requires browser verification. Open Vimm's Lair and try again later"
            )
        remote_page = False
        total = len(items)
    elif provider == "axekin":
        text, _ = await fetch_text(
            "https://www.axekin.com/search", params={"q": query, "page": page}
        )
        match = re.search(r'data-page=["\'](.*?)["\']', text, re.DOTALL)
        if not match:
            raise ValueError("Axekin did not return its catalog")
        props = json.loads(html.unescape(match[1]))["props"]
        games = props["games"]
        limit = max(1, min(100, int(games.get("meta", {}).get("perPage", 25))))
        items = []
        for game in games["data"]:
            options = []
            for link in game.get("downloadLinks", []):
                candidate = option(
                    link if isinstance(link, str) else link.get("url", ""),
                    "" if isinstance(link, str) else link.get("name", ""),
                )
                if candidate:
                    options.append(candidate)
            names = " ".join(p.get("name", "") for p in game.get("platforms", []))
            items.append(
                result(
                    provider,
                    game["title"],
                    "https://www.axekin.com/games/" + game["slug"],
                    options,
                    infer_platform(names),
                )
            )
        total = games.get("meta", {}).get("total", len(items))
        remote_page = True
    elif provider == "romstime":
        if region:
            raise ValueError("Choose the region in the RomsTime download options")
        text, _ = await fetch_text(
            "https://romstime.com/search", params={"q": query, "page": page}
        )
        if platform:
            slug = next(
                (
                    p["slug"]
                    for p in romstime_data(text, "platforms")
                    if infer_platform(p["name"]) == platform
                ),
                None,
            )
            if not slug:
                raise ValueError("RomsTime does not support this platform")
            text, _ = await fetch_text(
                "https://romstime.com/search",
                params={"q": query, "page": page, "platform": slug},
            )
        items = parse_romstime(text)
        count = re.search(
            r"Found\s+(?:<!--.*?-->\s*)?([\d,]+)\s+(?:<!--.*?-->\s*)?games?", text
        )
        if not count:
            raise ValueError("RomsTime did not return its search count")
        total = int(count[1].replace(",", ""))
        limit = 24
        remote_page = True
    elif provider == "startgame":
        text, headers = await fetch_text(
            "https://startgame.world/wp-json/wp/v2/posts",
            params={
                "search": query,
                "per_page": limit,
                "page": page,
                "_fields": "id,link,slug,title,content",
            },
        )
        items = []
        for post in json.loads(text):
            options = []
            for link in re.findall(
                r'href=["\']([^"\']+)["\']', post["content"]["rendered"], re.IGNORECASE
            ):
                candidate = option(html.unescape(link))
                if candidate and candidate.url not in [o.url for o in options]:
                    options.append(candidate)
            if not options:
                options.append(
                    ProviderOption(label="StartGame", method="verify", url=post["link"])
                )
            items.append(
                result(provider, post["title"]["rendered"], post["link"], options)
            )
        total = int(headers.get("x-wp-total", len(items))) if items else 0
        remote_page = True
    else:
        raise ValueError("Unknown provider")
    if remote_page and provider != "romstime" and (platform or region):
        raise ValueError("This catalog does not support platform or region filters")
    items = [
        r
        for r in items
        if (not platform or r.platform == platform)
        and (not region or region.casefold() in r.region.casefold())
    ]
    if not remote_page:
        total = len(items)
        items = items[(page - 1) * limit : page * limit]
    for item in items:
        if provider == "romstime":
            # Keep version indices stable for already-open import dialogs.
            await async_cache.set(
                f"provider:result:{item.id}", item.model_dump_json(), ex=86400, nx=True
            )
        else:
            await async_cache.setex(
                f"provider:result:{item.id}", 86400, item.model_dump_json()
            )
    return ProviderSearch(items=items, total=total, page=page, limit=limit)
