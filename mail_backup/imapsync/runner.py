"""Запуск процесса imapsync с потоковым разбором вывода.

Модуль не знает о БД: прогресс и PID отдаются через колбэки.
"""

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .command import Job, build_command
from .parser import OutputParser, Progress, Stats


@dataclass
class RunResult:
    exit_code: int
    stats: Stats
    last_progress: Progress | None


def _write_passfile(path: Path, password: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password + "\n")


def run_imapsync(
    job: Job,
    *,
    imapsync_bin: str,
    work_dir: Path,
    log_path: Path,
    on_start: Callable[[int], None] | None = None,
    on_progress: Callable[[Progress], None] | None = None,
) -> RunResult:
    """Запускает imapsync и ждёт завершения.

    work_dir — личный каталог запуска (файлы паролей, tmp imapsync);
    удаляется после завершения, чтобы пароли не оставались на диске.
    """
    work_dir.mkdir(parents=True, mode=0o700)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        passfile1, passfile2 = work_dir / "pass1", work_dir / "pass2"
        _write_passfile(passfile1, job.source.password)
        _write_passfile(passfile2, job.target.password)
        cmd = build_command(
            job,
            imapsync_bin=imapsync_bin,
            passfile1=passfile1,
            passfile2=passfile2,
            tmpdir=work_dir,
        )

        parser = OutputParser()
        with open(log_path, "a", encoding="utf-8") as log:
            # Паролей в команде нет — только пути к файлам, поэтому её можно писать в лог
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # Своя группа процессов: при отмене можно убить imapsync целиком
                start_new_session=True,
            )
            if on_start:
                on_start(proc.pid)
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                progress = parser.feed(line)
                if progress and on_progress:
                    on_progress(progress)
            exit_code = proc.wait()
            log.write(f"\n# код выхода imapsync: {exit_code}\n")

        return RunResult(exit_code=exit_code, stats=parser.stats, last_progress=parser.last_progress)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
