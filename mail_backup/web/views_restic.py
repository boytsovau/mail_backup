"""Страница «Снимки»: restic вкл/выкл, репозиторий, удалённые серверы, обслуживание."""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import app_settings, restic, restic_tasks
from ..config import get_settings
from ..db import get_session
from ..jobs import check_backup_storage
from ..models import Mailbox, ResticRemote, ResticTask, ResticTaskKind
from ..schedule import parse_schedule
from .common import csrf_protect, flash, render, require_admin, templates
from .views_runs import log_tail

router = APIRouter(prefix="/restic", dependencies=[Depends(require_admin)])

HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:]+)$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
# Без пробелов и кавычек: путь уходит в URL репозитория restic
PATH_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]{0,399}$")


@dataclass
class BoxSnapshots:
    """Снимки одного ящика (или ящика, удалённого из панели)."""

    box: Mailbox | None
    mailbox_id: int | None
    items: list[restic.Snapshot] = field(default_factory=list)

    @property
    def newest(self) -> restic.Snapshot | None:
        return self.items[0] if self.items else None

    @property
    def oldest(self) -> restic.Snapshot | None:
        return self.items[-1] if self.items else None


def snapshot_groups(session: Session) -> tuple[list[BoxSnapshots], str | None]:
    """Снимки локального репозитория по ящикам; вторым — ошибка restic, если была."""
    boxes = list(session.scalars(select(Mailbox).order_by(Mailbox.name)))
    groups = {box.id: BoxSnapshots(box, box.id) for box in boxes}
    if not restic.ready():
        return list(groups.values()), None
    try:
        snaps = restic.snapshots(restic.local_repo())
    except restic.ResticError as exc:
        return list(groups.values()), str(exc)
    orphans: dict[int | None, BoxSnapshots] = defaultdict(lambda: BoxSnapshots(None, None))
    for snap in snaps:
        group = groups.get(snap.mailbox_id) if snap.mailbox_id is not None else None
        if group is None:
            group = orphans[snap.mailbox_id]
            group.mailbox_id = snap.mailbox_id
        group.items.append(snap)
    return [*groups.values(), *orphans.values()], None


@dataclass
class RemoteRow:
    remote: ResticRemote
    last_copy: ResticTask | None
    active: ResticTask | None
    fingerprints: list[str]


def _remote_rows(session: Session) -> list[RemoteRow]:
    rows = []
    for remote in session.scalars(select(ResticRemote).order_by(ResticRemote.name)):
        active = session.scalars(
            select(ResticTask).where(ResticTask.remote_id == remote.id, ResticTask.status.in_(restic_tasks.ACTIVE))
        ).first()
        rows.append(RemoteRow(remote, restic_tasks.last_copy(session, remote.id), active,
                              restic.host_fingerprints(remote.host, remote.port)))
    return rows


def _page(request: Request, session: Session, *, password: str | None = None, form: dict | None = None,
          errors: dict | None = None, status_code: int = 200):
    settings = get_settings()
    ver = restic.version()
    groups, snap_error = snapshot_groups(session)
    initialized = restic.local_initialized()
    tasks = restic_tasks.last_tasks(session)
    return render(request, session, "restic.html", {
        "rprefs": app_settings.load_restic(session),
        "version": ver,
        "version_ok": restic.version_ok(ver),
        "min_version": ".".join(map(str, restic.MIN_VERSION)),
        "initialized": initialized,
        "ready": restic.ready(),
        "repo_path": settings.restic_repo,
        "repo_size": restic.repo_size(settings.restic_repo) if initialized else None,
        "groups": groups,
        "snap_error": snap_error,
        "remotes": _remote_rows(session),
        "public_key": restic.public_key(),
        "tasks": tasks,
        "tasks_active": any(t.status in restic_tasks.ACTIVE for t in tasks),
        "password": password,
        "form": form or {"port": "22", "path": "mail_backup"},
        "errors": errors or {},
        "TASK_LABELS": restic_tasks.TASK_LABELS,
    }, status_code)


@router.get("")
def restic_page(request: Request, session: Session = Depends(get_session)):
    return _page(request, session)


@router.post("/toggle", dependencies=[Depends(csrf_protect)])
async def restic_toggle(request: Request, session: Session = Depends(get_session)):
    enabled = (await request.form()).get("enabled") == "1"
    app_settings.save_restic(session, app_settings.load_restic(session), enabled=enabled)
    if enabled:
        flash(request, "Снимки включены: после каждого бэкапа делается снимок, зеркало снова удаляет "
                       "письма, удалённые на хостере (они остаются в снимках на срок хранения)")
    else:
        flash(request, "Снимки выключены. Уже сделанные снимки остаются; зеркало больше не удаляет письма, "
                       "удалённые на хостере", "warn")
    return RedirectResponse("/restic", 303)


@router.post("/settings", dependencies=[Depends(csrf_protect)])
async def restic_settings(request: Request, session: Session = Depends(get_session)):
    schedule = str((await request.form()).get("schedule", "")).strip()
    try:
        times = parse_schedule(schedule)
    except ValueError as exc:
        flash(request, str(exc), "error")
    else:
        schedule = ", ".join(t.strftime("%H:%M") for t in times)
        app_settings.save_restic(session, app_settings.load_restic(session), schedule=schedule)
        flash(request, f"Обслуживание: {schedule}" if schedule else "Обслуживание по расписанию выключено")
    return RedirectResponse("/restic", 303)


@router.post("/init", dependencies=[Depends(csrf_protect)])
def restic_init(request: Request, session: Session = Depends(get_session)):
    try:
        check_backup_storage()
        created = restic.init_local()
    except (restic.ResticError, RuntimeError) as exc:
        flash(request, f"Репозиторий не создан: {exc}", "error")
    else:
        flash(request, "Репозиторий создан" if created else "Репозиторий уже есть")
    return RedirectResponse("/restic", 303)


@router.post("/password", dependencies=[Depends(csrf_protect)])
def restic_password(request: Request, session: Session = Depends(get_session)):
    """Пароль репозиториев — по явному запросу, не в обычной выдаче страницы."""
    path = get_settings().restic_password_file
    password = path.read_text().strip() if path.is_file() else ""
    return _page(request, session, password=password or "(файл пароля не найден)")


@router.post("/tasks", dependencies=[Depends(csrf_protect)])
async def restic_task_start(request: Request, session: Session = Depends(get_session)):
    """Очистка или проверка локального репозитория вручную."""
    kind = (await request.form()).get("kind")
    if kind not in (ResticTaskKind.PRUNE, ResticTaskKind.CHECK):
        raise HTTPException(400, "Неизвестная задача")
    task = restic_tasks.enqueue_task(session, ResticTaskKind(kind))
    return RedirectResponse(f"/restic/tasks/{task.id}", 303)


# --- SSH-ключ и удалённые серверы ----------------------------------------------------------

@router.post("/ssh-key", dependencies=[Depends(csrf_protect)])
def ssh_key(request: Request, session: Session = Depends(get_session)):
    replaced = restic.public_key() is not None
    restic.generate_ssh_key()
    flash(request, "Создан новый ключ — замените его в authorized_keys на всех серверах" if replaced
          else "Ключ создан — добавьте его на серверы для выгрузки", "warn" if replaced else "ok")
    return RedirectResponse("/restic#remotes", 303)


def _get_remote(session: Session, remote_id: int) -> ResticRemote:
    remote = session.get(ResticRemote, remote_id)
    if remote is None:
        raise HTTPException(404, "Сервер не найден")
    return remote


def validate_remote(form: dict) -> dict[str, str]:
    errors = {}
    if not HOST_RE.match(form["host"]):
        errors["host"] = "Имя или IP-адрес сервера"
    if not form["port"].isdigit() or not 1 <= int(form["port"]) <= 65535:
        errors["port"] = "Порт — число от 1 до 65535"
    if not USER_RE.match(form["user"]):
        errors["user"] = "Имя пользователя: латиница, цифры, «.», «_», «-»"
    if not PATH_RE.match(form["path"]) or ".." in form["path"].split("/"):
        errors["path"] = "Путь: латиница, цифры, «.», «_», «-», «/»; без пробелов и «..»"
    return errors


@router.post("/remotes", dependencies=[Depends(csrf_protect)])
async def remote_add(request: Request, session: Session = Depends(get_session)):
    data = await request.form()
    form = {k: str(data.get(k, "")).strip() for k in ("name", "host", "port", "user", "path")}
    if errors := validate_remote(form):
        return _page(request, session, form=form, errors=errors, status_code=400)
    remote = ResticRemote(name=form["name"] or form["host"], host=form["host"], port=int(form["port"]),
                          user=form["user"], path=form["path"].rstrip("/") or "/")
    session.add(remote)
    session.commit()
    flash(request, f"Сервер «{remote.name}» добавлен. Нажмите «Проверить» — при первом подключении "
                   "запоминается ключ сервера; репозиторий создаётся при первой выгрузке")
    return RedirectResponse("/restic#remotes", 303)


@router.post("/remotes/{remote_id}/check", dependencies=[Depends(csrf_protect)], response_class=HTMLResponse)
async def remote_check(request: Request, remote_id: int, session: Session = Depends(get_session)):
    remote = _get_remote(session, remote_id)
    if restic.public_key() is None:
        ok, message = False, "Сначала создайте SSH-ключ и добавьте его на сервер"
    else:
        ok, message = await run_in_threadpool(restic.remote_status, restic.remote_repo(remote))
    fingerprints = restic.host_fingerprints(remote.host, remote.port)
    if ok and fingerprints:
        message += f". Ключ сервера: {fingerprints[0]}"
    return templates.TemplateResponse(request, "mailboxes/_check.html", {"ok": ok, "message": message})


@router.post("/remotes/{remote_id}/upload", dependencies=[Depends(csrf_protect)])
def remote_upload(request: Request, remote_id: int, session: Session = Depends(get_session)):
    remote = _get_remote(session, remote_id)
    task = restic_tasks.enqueue_task(session, ResticTaskKind.COPY, remote)
    return RedirectResponse(f"/restic/tasks/{task.id}", 303)


@router.post("/remotes/{remote_id}/toggle", dependencies=[Depends(csrf_protect)])
def remote_toggle(request: Request, remote_id: int, session: Session = Depends(get_session)):
    remote = _get_remote(session, remote_id)
    remote.enabled = not remote.enabled
    session.commit()
    flash(request, f"Выгрузка на «{remote.name}» по расписанию " + ("включена" if remote.enabled else "выключена"))
    return RedirectResponse("/restic#remotes", 303)


@router.post("/remotes/{remote_id}/forget-host", dependencies=[Depends(csrf_protect)])
def remote_forget_host(request: Request, remote_id: int, session: Session = Depends(get_session)):
    remote = _get_remote(session, remote_id)
    restic.forget_host(remote.host, remote.port)
    flash(request, f"Ключ сервера {remote.host} забыт — при следующем подключении запомнится новый", "warn")
    return RedirectResponse("/restic#remotes", 303)


@router.post("/remotes/{remote_id}/delete", dependencies=[Depends(csrf_protect)])
def remote_delete(request: Request, remote_id: int, session: Session = Depends(get_session)):
    remote = _get_remote(session, remote_id)
    if session.scalars(select(ResticTask).where(ResticTask.remote_id == remote.id,
                                                ResticTask.status.in_(restic_tasks.ACTIVE))).first():
        flash(request, "Сначала дождитесь окончания или отмените выгрузку на этот сервер", "error")
        return RedirectResponse("/restic#remotes", 303)
    name = remote.name
    session.delete(remote)
    session.commit()
    flash(request, f"Сервер «{name}» удалён из панели. Снимки на нём остались")
    return RedirectResponse("/restic#remotes", 303)


# --- Задачи ------------------------------------------------------------------------------------

def _get_task(session: Session, task_id: int) -> ResticTask:
    task = session.get(ResticTask, task_id)
    if task is None:
        raise HTTPException(404, "Задача не найдена")
    return task


def _task_ctx(task: ResticTask) -> dict:
    return {"task": task, "log": log_tail(task), "TASK_LABELS": restic_tasks.TASK_LABELS}


@router.get("/tasks/{task_id}")
def task_detail(request: Request, task_id: int, session: Session = Depends(get_session)):
    return render(request, session, "restic_task.html", _task_ctx(_get_task(session, task_id)))


@router.get("/tasks/{task_id}/live")
def task_live(request: Request, task_id: int, session: Session = Depends(get_session)):
    return render(request, session, "_restic_task_live.html", _task_ctx(_get_task(session, task_id)) | {"flashes": []})


@router.post("/tasks/{task_id}/cancel", dependencies=[Depends(csrf_protect)])
def task_cancel(request: Request, task_id: int, session: Session = Depends(get_session)):
    task = _get_task(session, task_id)
    if task.status in restic_tasks.ACTIVE:
        task.cancel_requested = True
        session.commit()
        flash(request, "Отмена запрошена — воркер остановит restic в течение пары секунд")
    return RedirectResponse(f"/restic/tasks/{task.id}", 303)


def snapshot_choices(mailbox_id: int) -> tuple[list[restic.Snapshot], str | None]:
    """Снимки ящика для мастера восстановления; вторым — ошибка, если restic недоступен."""
    if not restic.ready():
        return [], None
    try:
        return restic.snapshots(restic.local_repo(), mailbox_id), None
    except restic.ResticError as exc:
        return [], str(exc)

