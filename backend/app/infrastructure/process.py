import subprocess
from pathlib import Path


class ProcessError(RuntimeError):
    pass


def run_command(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> list[str]:
    logs: list[str] = []
    with subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            logs.append(line.rstrip())
        code = process.wait()
    if code != 0:
        tail = "\n".join(logs[-30:])
        raise ProcessError(f"Command failed with exit code {code}: {' '.join(command)}\n{tail}")
    return logs
