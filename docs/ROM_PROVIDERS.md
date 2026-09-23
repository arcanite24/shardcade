# ROM providers

This fork adds an administrator-only **ROM providers** page to the new RomM UI.
Enable the new UI in User interface settings, then open ROM providers from the
account menu or Settings sidebar. Set `ROM_PROVIDERS_ENABLED=true` to enable its
API and downloads. Native RomM authentication and library APIs are unchanged.

## Sources

| Provider       | Catalog                                                       | Download                                                                         |
| -------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Minerva        | Local SQLite full-text index from published torrent manifests | Selective qBittorrent transfer of the chosen file                                |
| Axekin         | Website catalog search                                        | Direct links, MEGA, or browser-generated host links                              |
| Edge Emulation | Website search                                                | HTTP download                                                                    |
| Vimm's Lair    | Vault search                                                  | Open the source, complete its verification, paste the generated URL              |
| StartGame      | WordPress platform collections; an empty search browses them  | Open the collection, sign in when required, paste its direct or public MEGA link |

Providers and download hosts can change availability or impose account, quota,
verification, or regional restrictions. RomM does not bypass those gates or store
provider account passwords. Public MEGA file/folder links support a file picker,
streamed decryption, and integrity verification. Unsupported hosts can be downloaded
in the browser and imported with RomM's existing Upload ROMs screen.

## Minerva without the unreliable search API

Refresh index downloads the latest versioned manifest ZIP listed at
<https://cdn.minerva-archive.org/torrents/>. It parses exact torrent info hashes and
file paths into SQLite FTS5. Searching never calls Minerva's search API. The
published snapshot may lag new uploads; Refresh index checks for a newer bundle.

Search supports title tokens, accents, platform browsing, and pagination. Results
show collection names to distinguish duplicate releases. Platform inference is
heuristic: confirm the destination platform before importing. The index includes
non-ROM files too, so filter by platform when appropriate.

Index builds checkpoint each manifest and atomically replace the searchable
snapshot only after validation. Interrupted refreshes preserve the previous index.
Keep `/romm/resources/providers/minerva` persistent, preferably on a Linux Docker
volume: SQLite bulk indexing through a Windows bind mount is much slower. Reserve
several GB for the bundle, database, staging snapshot, and extracted manifests.

The April 2026 v3 bundle indexed **2,427,933** safe, nonempty files during validation.
Four GBA-filtered searches took **9–36 ms** locally. These are measured examples,
not latency guarantees. A real 125,123-byte 240p Test Suite homebrew ROM was
selectively downloaded, registered, and served through the authenticated library API.

## Download configuration

Use a dedicated qBittorrent instance on the private Docker network. Do not publish
its Web UI. Configure credentials using your deployment's secret environment file:

```dotenv
ROM_PROVIDERS_ENABLED=true
PROVIDER_QBITTORRENT_URL=http://romm-downloads:8080
PROVIDER_QBITTORRENT_USERNAME=romm
PROVIDER_QBITTORRENT_PASSWORD=
PROVIDER_QBITTORRENT_SAVE_PATH=/downloads/romm
PROVIDER_DOWNLOAD_PATH=/downloads/romm
```

Set a real qBittorrent password privately. Mount the same staging storage at
`/downloads` in both containers. If their mount paths differ, the save path is the
qBittorrent path and `PROVIDER_DOWNLOAD_PATH` is the corresponding RomM path.
The library's platform directories must be writable for imports. Keep enough free
space for staging plus the final copy; MEGA also needs its encrypted staging file.
Pre-create the shared save directory with ownership matching qBittorrent's runtime
user. If RomM creates it as root with mode 0755, a downloader running as UID 1000
cannot write there. Storage errors stop the job with an actionable permissions error.

RomM uses its existing low-priority RQ workers. Downloads run sequentially so two
imports cannot change the same torrent's file priorities concurrently. Only torrents
in the dedicated `romm-providers` category are managed; pre-existing torrents outside
that category are rejected. Selection matches exact path and size, not an API index.
Completed or cancelled transfers are stopped. Staged torrent data remains reusable;
remove completed staging data through qBittorrent when no imports are active.

The Downloads tab shows progress, errors, cancellation, and retry. Cancellation is
cooperative; a waiting job is skipped when its turn reaches the worker. HTTP retries
resume only with a matching strong ETag. Files are checked, copied through a temporary
file, and registered with RomM. Compressed downloads extract their largest ROM file
into staging first; the archive is not added to the library. Extraction is bounded by
available disk space, and existing files are never overwritten. Once registered, a
targeted unmatched scan uses the enabled metadata sources and the import waits for it
to finish. A quick scan would skip metadata lookups for the already registered ROM.
If metadata scanning fails, the imported ROM remains available and the job shows a
warning. If database registration fails after publication, the error explicitly
requests a library scan. Multi-file disc archives need manual import because choosing
one track would lose the others.

## API and handheld clients

Provider routes are under `/api/providers`. Every route requires native authentication,
the `tasks.run` scope, and administrator permission. Downloads also require
`roms.write`. Search results are resolved server-side; arbitrary download descriptors
from clients are not accepted. URLs are restricted to supported HTTPS hosts and the
existing SSRF-protected HTTP transport validates public destinations.

Keep `/api` reachable through the normal RomM reverse proxy, without an extra Google
login wall. NeoStation uses ordinary RomM credentials/tokens and `/api/roms` content
endpoints, not provider administrator access. Native token creation, refresh, anonymous
denial, and a 16-byte HTTP 206 range download were validated with an imported ROM.

## Development checks

```sh
cd backend
uv run python -m unittest tools.test_providers
uv run pytest tests/endpoints/test_providers.py
cd ../frontend
npm run typecheck
npm test
npm run build
```

The provider checks cover torrent identity/selection, interrupted index rebuilds,
path and URL boundaries, atomic no-overwrite publication, HTTP resume, provider HTML
contracts, worker status, and independent MEGA integrity vectors including tampering.
Endpoint tests cover authentication, administrator access, disabled operation, input
validation, and expired server-side search results. External provider availability
and actual handheld hardware still require live checks.
