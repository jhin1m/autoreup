from pydantic import BaseModel, Field


class ProviderSessionStatus(BaseModel):
    platform: str
    label: str
    configured: bool
    updated_at: str | None = None
    cookie_count: int
    cookie_names: list[str]
    required_missing: list[str]
    recommended_missing: list[str]
    note: str


class CookieImportRequest(BaseModel):
    cookies: str = Field(min_length=4)


class ProviderSessionsResponse(BaseModel):
    sessions: list[ProviderSessionStatus]
