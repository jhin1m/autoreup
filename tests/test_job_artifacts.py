from backend.app.domain.artifacts import Artifact, ArtifactKind
from backend.app.domain.jobs import JobInput, JobManifest, JobState
from backend.app.services.job_service import drop_missing_artifacts


def _artifact(kind: ArtifactKind, path: str) -> Artifact:
    return Artifact(kind=kind, path=path, mime_type="application/octet-stream")


def _manifest(state: JobState, artifacts: list[Artifact]) -> JobManifest:
    return JobManifest(schema_version="test", job_id="job1", input=JobInput(url="https://example.com/v"), state=state, artifacts=artifacts)


def test_terminal_manifests_drop_artifacts_whose_file_is_gone(tmp_path):
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "ok.mp4").write_bytes(b"x")
    artifacts = [
        _artifact(ArtifactKind.dubbed_mp4, "packages/ok.mp4"),
        _artifact(ArtifactKind.hardsub_mp4, "packages/gone.mp4"),
    ]

    manifest = drop_missing_artifacts(_manifest(JobState.succeeded, artifacts), tmp_path)
    assert [a.kind for a in manifest.artifacts] == [ArtifactKind.dubbed_mp4]


def test_running_manifests_keep_pending_artifacts(tmp_path):
    artifacts = [
        _artifact(ArtifactKind.dubbed_mp4, "packages/ok.mp4"),
        _artifact(ArtifactKind.hardsub_mp4, "packages/gone.mp4"),
    ]
    manifest = drop_missing_artifacts(_manifest(JobState.running, artifacts), tmp_path)
    assert len(manifest.artifacts) == 2
