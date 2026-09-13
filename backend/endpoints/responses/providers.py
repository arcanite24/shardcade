from typing import Literal

from pydantic import BaseModel, Field

ProviderId = Literal["minerva", "axekin", "vimm", "edgeemu", "startgame"]


class ProviderOption(BaseModel):
    label: str
    method: Literal["torrent", "http", "verify", "mega"]
    url: str
    filename: str = ""
    size: int | None = None


class ProviderResult(BaseModel):
    id: str
    provider: ProviderId
    name: str
    platform: str = ""
    region: str = "Unknown"
    collection: str = ""
    filename: str = ""
    size: int | None = None
    source_url: str
    options: list[ProviderOption] = Field(default_factory=list)


class ProviderSearch(BaseModel):
    items: list[ProviderResult]
    total: int
    page: int
    limit: int


class ProviderJob(BaseModel):
    id: str
    kind: str
    state: str
    phase: str = "queued"
    name: str = ""
    progress: float = 0
    completed_bytes: int = 0
    total_bytes: int | None = None
    records: int = 0
    error: str | None = None
    rom_id: int | None = None


class ProviderStatus(BaseModel):
    enabled: bool
    torrent_configured: bool
    index_ready: bool
    records: int = 0
    indexed_at: str | None = None
    platforms: list[str] = Field(default_factory=list)
    jobs: list[ProviderJob] = Field(default_factory=list)


class ProviderImportRequest(BaseModel):
    result_id: str = Field(min_length=1, max_length=128)
    option: int = Field(default=0, ge=0, le=100)
    platform_id: int = Field(gt=0)
    verified_url: str | None = Field(default=None, max_length=4096)
    mega_node_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8}$")


class ProviderMegaFile(BaseModel):
    id: str
    name: str
    size: int


class ProviderFilesRequest(BaseModel):
    result_id: str = Field(min_length=1, max_length=128)
    option: int = Field(default=0, ge=0, le=100)
    verified_url: str | None = Field(default=None, max_length=4096)
