from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOREUP_", env_file=".env", extra="ignore")

    app_name: str = "AutoReup"
    api_prefix: str = "/api/v1"
    schema_version: str = "2026-06-18"
    root_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3])
    data_dir_name: str = "data"
    frontend_dir_name: str = "frontend"
    llm_provider: str = "responses"
    llm_base_url: str = "http://localhost:8090/v1"
    translate_model: str = "gpt-5.4-mini"
    polish_model: str = "gpt-5.4"
    llm_stream: bool = True
    content_genre: str = ""  # optional hint; glossary is extracted data-driven per video
    asr_worker_url: str | None = None
    asr_model: str = "@cf/openai/whisper-large-v3-turbo"
    cookies_file: str | None = None
    browser_path: str | None = None  # Chrome/Edge binary for the headless Douyin extractor
    node_bin: str = "node"
    max_chars: int = 60
    display_max_chars: int = 32
    display_max_lines: int = 1
    max_cps: float = 32.0
    target_cps: float = 22.0
    max_low_quality_rate: float = 0.15
    max_merged_with_pause: int = 0
    max_merged_with_turn_boundary: int = 0
    min_duration: float = 0.45
    max_duration: float = 4.8
    min_gap: float = 0.03
    snap_gap: float = 0.12
    tail_out: float = 0.28
    tts_enabled: bool = False
    tts_server_url: str | None = None
    tts_voice: str = "BV421_vivn_streaming"
    tts_speed: float = 1.0  # neutral synth; timing is fitted by ffmpeg tempo, not provider speed
    tts_pitch: float = 1.0
    tts_volume: float = 1.0
    tts_text_mode: str = "natural"  # keep punctuation so the voice-over pauses naturally
    tts_target_chars_per_second: float = 12.0
    tts_post_tempo: float = 1.12
    # Stable forward pace: never slow below 1.0 (no dragging), speed up to 1.18 to
    # fit long lines; beyond that the repair loop shortens the text. Set
    # tts_fixed_tempo to force one rigid rate for every clip (costs more repair).
    tts_min_tempo: float = 1.0
    tts_max_tempo: float = 1.18
    tts_fixed_tempo: float | None = None
    tts_assembly_mode: str = "sequential"
    tts_group_blocks: bool = True
    tts_group_max_gap: float = 2.0
    tts_group_max_duration: float = 30.0
    tts_group_max_chars: int = 1000

    @property
    def data_dir(self) -> Path:
        return self.root_dir / self.data_dir_name

    @property
    def frontend_dir(self) -> Path:
        return self.root_dir / self.frontend_dir_name

    @property
    def frontend_static_dir(self) -> Path:
        dist_dir = self.frontend_dir / "dist"
        return dist_dir if dist_dir.exists() else self.frontend_dir

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def subtitles_dir(self) -> Path:
        return self.data_dir / "subtitles"

    @property
    def packages_dir(self) -> Path:
        return self.data_dir / "packages"

    @property
    def tts_dir(self) -> Path:
        return self.data_dir / "tts"

    @property
    def tts_cache_dir(self) -> Path:
        return self.tts_dir / "cache"

    @property
    def private_dir(self) -> Path:
        return self.root_dir / ".autoreup-private"

    @property
    def provider_cookies_dir(self) -> Path:
        return self.private_dir / "cookies"

    @property
    def scripts_dir(self) -> Path:
        return self.root_dir / "scripts"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.jobs_dir, self.videos_dir, self.subtitles_dir, self.packages_dir, self.tts_dir, self.tts_cache_dir, self.provider_cookies_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
