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
    # How many dub jobs run at once. Jobs beyond this wait in a FIFO queue. Most of a
    # job's wall time is waiting on remote services (download, ASR, LLM, CapCut TTS) and
    # the one CPU-heavy step (encode) is now GPU-offloaded, so the local machine isn't the
    # limit — the ceiling is remote fan-out (≈ max_concurrent_jobs × tts_workers requests
    # to CapCut). 4 overlaps I/O waits well; lower if CapCut/LLM start throttling.
    max_concurrent_jobs: int = 4
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
    # Native (in-process) CapCut TTS client — no external node service. Reads a
    # persisted CapCut session (cookies/deviceId/tdid) and signs requests itself.
    tts_native: bool = True
    capcut_session_file: str | None = None  # path to capcut-session.json (default: private dir)
    capcut_email: str = ""      # for native auto-login (mints/refreshes the session)
    capcut_password: str = ""
    capcut_login_host: str = "https://login-row.www.capcut.com"
    capcut_fallback_login_host: str = "https://login.us.capcut.com"
    capcut_page_locale: str = "vi-vn"
    capcut_locale: str = "vi-VN"
    capcut_session_max_age_days: float = 3.0  # re-login when the session is older
    capcut_edit_api: str = "https://edit-api-sg.capcut.com"
    capcut_web_url: str = "https://www.capcut.com"
    capcut_app_id: str = "348188"
    capcut_app_version: str = "5.8.0"
    capcut_platform_id: str = "7"
    capcut_sign_version: str = "1"
    capcut_sign_prefix: str = "9e2c"
    capcut_sign_suffix: str = "11ac"
    capcut_sign_path_tail: int = 7
    capcut_store_country: str = "vn"
    capcut_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    capcut_effect_id: str = "7252594014782755330"   # BV421_vivn_streaming
    capcut_resource_id: str = "7252594014782755330"
    capcut_voice_name: str = "Nguồn nhỏ ngọt ngào"
    capcut_batch_size: int = 8        # texts per multi_platform call
    capcut_batch_concurrency: int = 8  # concurrent batch requests
    tts_voice: str = "BV421_vivn_streaming"
    tts_speed: float = 1.0  # neutral synth; timing is fitted by ffmpeg tempo, not provider speed
    tts_synth_rate: float = 1.22  # CapCut playback rate at synth — >1.0 = snappier (less "chậm")
    tts_pitch_shift: float = 1.06  # master-stage pitch lift (tempo-preserving) — >1.0 brightens a "trầm" voice
    dub_bed_volume: float = 0.18  # original audio kept as a quiet bed under the VI voice (was 0.38)
    # Hardsub: cover the original burned-in subtitle (OCR/cv2-detected band) + burn VI
    hardsub_font: str = ""  # empty -> vendored Bangers (comic display, Vietnamese-capable)
    hardsub_font_size: int = 58
    hardsub_blur: int = 20
    hardsub_band_pad: int = 26
    hardsub_darken: float = 0.12
    # Prefer Apple hardware H.264 encoder on macOS (h264_videotoolbox) over libx264:
    # ~5-10x faster and offloads the CPU, so many concurrent dub jobs don't oversubscribe
    # the box. Falls back to libx264 off-macOS or when videotoolbox isn't in the ffmpeg
    # build. Hardsub already emits canonical 1080p/30fps so exports just stream-copy.
    video_hw_encode: bool = True
    video_bitrate: str = "8M"   # target for hardware encode (no CRF on videotoolbox)
    video_fps: int = 30
    video_width: int = 1920
    video_height: int = 1080
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
    # Each unique line costs ~6s upstream (CapCut) but the TTS server is fully
    # concurrent and caches by text — so synth throughput = workers / 6s. This is the
    # real voiceover bottleneck; raise for long videos (server handled 24 fine).
    tts_workers: int = 16
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
