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
    content_notes: str = ""  # default per-video context for the translator (cast/gender/register)
    asr_worker_url: str | None = None
    asr_model: str = "@cf/openai/whisper-large-v3-turbo"
    cookies_file: str | None = None
    browser_path: str | None = None  # Chrome/Edge binary for the headless Douyin extractor
    node_bin: str = "node"
    max_chars: int = 60
    display_max_chars: int = 32
    display_max_lines: int = 1
    subtitle_max_line_chars: int = 40  # pro single-line width for the on-screen caption
    subtitle_lead: float = 0.12  # caption appears this much BEFORE the voice (pro lead-in ~3 frames)
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
    # Voice speaking-rate budget: the translator condenses each line to
    # ``duration * tts_voice_cps`` characters so it reads within the time the
    # character speaks at the voice's own natural pace (measured ~19.8 ch/s for
    # BV421). Budget sits just under that so lines fit at tempo ~1.0 with a touch of
    # natural pause — condensing the over-dense source (22-29 ch/s) without needless
    # content loss or underfill.
    tts_voice_cps: float = 18.0
    tts_post_tempo: float = 1.12
    # Read at (near-)natural pace so the dub is intelligible: never slow below 1.0
    # (no dragging) and only a hair of speed-up (1.08) for slightly-long lines.
    # Density is fixed at the source — the translator condenses to tts_voice_cps and
    # the repair loop shortens whatever still overruns — NOT by speeding the voice.
    tts_min_tempo: float = 1.0
    tts_max_tempo: float = 1.08
    tts_fixed_tempo: float | None = None
    # Voice is the master clock and stays neo'd at each anchor's onset so it matches
    # the picture ("khớp ảnh") AND the subtitle (which is derived from the voice
    # window). glue_gap=0 disables the earlier-shifting glue — it traded picture/sub
    # sync for marginal smoothness. crossfade still declicks clip edges.
    tts_glue_gap: float = 0.0
    tts_crossfade: float = 0.06
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
