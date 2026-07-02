from dataclasses import dataclass, field

from backend.app.domain.artifacts import Artifact
from backend.app.domain.jobs import StageName


@dataclass
class StageResult:
    name: StageName
    artifacts: list[Artifact] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)


class PipelineStage:
    name: StageName

    def run(self, context: "PipelineContext") -> StageResult:
        raise NotImplementedError

    def hydrate(self, context: "PipelineContext") -> bool:
        """Reload this stage's outputs from disk into ``context.artifacts`` WITHOUT
        re-running it. Returns False when the artifacts are missing/unreadable — the
        stage (and everything after it) must then run for real. This is what lets a
        failed job RESUME at the broken stage instead of re-downloading everything."""
        return False


@dataclass
class PipelineContext:
    job_id: str
    input_url: str
    job_dir: object
    settings: object
    config: object
    artifacts: dict[str, object] = field(default_factory=dict)
    series_id: str | None = None  # library link: lets stages share per-series state (e.g. glossary)
