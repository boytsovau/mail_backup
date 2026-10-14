"""Запуски: список, подробности с живым прогрессом и логом, отмена."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import storage_estimate
from ..db import get_session
from ..models import Mailbox, Run, RunStatus
from .common import csrf_protect, flash, render, require_admin

router = APIRouter(prefix="/runs", dependencies=[Depends(require_admin)])

PAGE_SIZE = 50
# Сколько байт с конца лога показываем в панели
LOG_TAIL_BYTES = 64 * 1024


def _get_run(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Запуск не найден")
    return run


def run_context(run: Run) -> dict:
    ctx = {"run": run, "log": log_tail(run), "folders": storage_estimate.folders_of(run)}
    if run.host1_bytes is not None and run.host1_messages is not None:
        ctx["estimate"] = {
            "mirror": storage_estimate.mirror_size(run.host1_messages, run.host1_bytes),
            "restic": storage_estimate.restic_size(run.host1_bytes),
            "recommended": storage_estimate.recommended_size(run.host1_messages, run.host1_bytes),
        }
    return ctx


def log_tail(run: Run) -> str:
    if not run.log_path:
        return ""
    path = Path(run.log_path)
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read()
    except FileNotFoundError:
        return "(лог удалён по сроку хранения)"
    text = data.decode("utf-8", errors="replace")
    if size > LOG_TAIL_BYTES:
        # Первая строка обрезана посередине — отбрасываем её
        text = "…\n" + text.split("\n", 1)[-1]
    return text


@router.get("")
def run_list(request: Request, mailbox: int | None = None, page: int = 1, session: Session = Depends(get_session)):
    query = select(Run)
    if mailbox:
        query = query.where(Run.mailbox_id == mailbox)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = max(1, page)
    runs = list(session.scalars(query.order_by(Run.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
    return render(request, session, "runs/list.html", {
        "runs": runs,
        "mailbox": session.get(Mailbox, mailbox) if mailbox else None,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
    })


@router.get("/{run_id}")
def run_detail(request: Request, run_id: int, session: Session = Depends(get_session)):
    run = _get_run(session, run_id)
    return render(request, session, "runs/detail.html", run_context(run))


@router.get("/{run_id}/live")
def run_live(request: Request, run_id: int, session: Session = Depends(get_session)):
    """Фрагмент для htmx: статус, прогресс, статистика и лог. Опрос, пока запуск активен."""
    run = _get_run(session, run_id)
    return render(request, session, "runs/_live.html", run_context(run) | {"flashes": []})


@router.post("/{run_id}/cancel", dependencies=[Depends(csrf_protect)])
def run_cancel(request: Request, run_id: int, session: Session = Depends(get_session)):
    run = _get_run(session, run_id)
    if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        run.cancel_requested = True
        session.commit()
        flash(request, "Отмена запрошена — воркер остановит imapsync в течение пары секунд")
    return RedirectResponse(f"/runs/{run.id}", 303)
