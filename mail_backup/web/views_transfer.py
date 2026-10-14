"""Мастер восстановления и переноса: выбор получателя, пробный прогон, запуск.

Восстановление — из бэкапа (локальный Dovecot) в любой ящик, в т.ч. в исходный.
Перенос — напрямую с хостера ящика в другой ящик. Источник в обоих случаях
только читается; в получателе imapsync ничего не удаляет (см. command.check_safe).
"""

from dataclasses import dataclass, fields

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import crypto
from ..db import get_session
from ..imapcheck import check_login
from ..imapsync.command import SUBFOLDER_RE
from ..jobs import DONE_OK, active_run, enqueue_run, is_local_host, last_backup
from ..models import Mailbox, Run, RunKind, RunTrigger, utcnow
from ..providers import PROVIDERS, Security, get_provider
from .common import csrf_protect, flash, render, require_admin, templates
from .views_mailboxes import _presets

router = APIRouter(dependencies=[Depends(require_admin)])

TRANSFER_KINDS = (RunKind.RESTORE, RunKind.MIGRATE)


def default_subfolder() -> str:
    return f"Restored-{utcnow():%Y-%m-%d}"


@dataclass
class TransferForm:
    """Значения формы мастера (строки — как пришли из браузера)."""

    mode: str = RunKind.RESTORE.value  # restore — из бэкапа, migrate — напрямую с хостера
    target: str = "same"  # same — в этот же ящик, other — в другой
    provider: str = "yandex"
    host: str = ""
    port: str = "993"
    security: str = Security.SSL.value
    login: str = ""
    password: str = ""
    placement: str = "as_is"  # as_is — как в источнике, subfolder — всё в одну папку
    subfolder: str = ""

    @classmethod
    async def from_request(cls, request: Request) -> "TransferForm":
        form = await request.form()
        values = {f.name: str(form.get(f.name, "")).strip() for f in fields(cls)}
        # Пароль не обрезаем: пробелы могут быть его частью
        values["password"] = str(form.get("password", ""))
        return cls(**values)

    @classmethod
    def for_mailbox(cls, box: Mailbox, *, has_backup: bool) -> "TransferForm":
        preset = get_provider("yandex")
        return cls(
            mode=RunKind.RESTORE.value if has_backup else RunKind.MIGRATE.value,
            target="same" if has_backup else "other",
            host=preset.host,
            port=str(preset.port),
            security=preset.security.value,
            subfolder=default_subfolder(),
        )

    @classmethod
    def from_run(cls, run: Run) -> "TransferForm":
        """Параметры прошлого запуска (кроме пароля) — чтобы не вводить заново."""
        box = run.mailbox
        same = (run.target_host or "").lower() == box.host.lower() and (run.target_login or "").lower() == box.login.lower()
        provider = next((p.key for p in PROVIDERS.values() if p.host and p.host == run.target_host), "custom")
        return cls(
            mode=str(run.kind),
            target="same" if same else "other",
            provider=provider,
            host=run.target_host or "",
            port=str(run.target_port or 993),
            security=str(run.target_security or Security.SSL),
            login=run.target_login or "",
            placement="subfolder" if run.target_subfolder else "as_is",
            subfolder=run.target_subfolder or default_subfolder(),
        )

    def validate(self, box: Mailbox, *, has_backup: bool) -> dict[str, str]:
        """Возвращает ошибки по полям; пустой словарь — всё верно."""
        errors = {}
        if self.mode not in TRANSFER_KINDS:
            errors["mode"] = "Выберите, что делать"
        elif self.mode == RunKind.RESTORE and not has_backup:
            errors["mode"] = "У ящика ещё нет удачного бэкапа — восстанавливать нечего"

        if self.target == "same":
            if self.mode == RunKind.MIGRATE:
                errors["target"] = "Перенос в тот же ящик ничего не даст — укажите другой ящик"
        elif self.target == "other":
            if not self.host:
                errors["host"] = "Укажите IMAP-сервер"
            elif is_local_host(self.host):
                errors["host"] = "Это адрес самого сервера бэкапов — туда восстанавливать нельзя"
            if not self.port.isdigit() or not 1 <= int(self.port) <= 65535:
                errors["port"] = "Порт — число от 1 до 65535"
            if self.security not in {s.value for s in Security}:
                errors["security"] = "Выберите тип шифрования"
            if not self.login:
                errors["login"] = "Укажите логин ящика-получателя"
            if not self.password:
                errors["password"] = "Укажите пароль (пароль приложения)"
            if (
                self.mode == RunKind.MIGRATE
                and self.host.lower() == box.host.lower()
                and self.login.lower() == box.login.lower()
            ):
                errors["login"] = "Это тот же ящик, что и источник — укажите другой"
        else:
            errors["target"] = "Выберите получателя"

        if self.placement == "subfolder":
            if not SUBFOLDER_RE.match(self.subfolder):
                errors["subfolder"] = (
                    "Только латинские буквы, цифры, пробел, «-» и «_», до 60 символов "
                    "(imapsync выбрасывает остальные символы из имени)"
                )
        elif self.placement != "as_is":
            errors["placement"] = "Выберите, куда класть письма"
        return errors

    def run_fields(self, box: Mailbox) -> dict:
        """Поля получателя для Run; пароль — зашифрованный."""
        if self.target == "same":
            target = {
                "target_host": box.host,
                "target_port": box.port,
                "target_security": box.security,
                "target_login": box.login,
                "target_password_enc": box.password_enc,
            }
        else:
            target = {
                "target_host": self.host,
                "target_port": int(self.port),
                "target_security": Security(self.security),
                "target_login": self.login,
                "target_password_enc": crypto.encrypt(self.password),
            }
        target["target_subfolder"] = self.subfolder if self.placement == "subfolder" else None
        return target


def _get_box(session: Session, mailbox_id: int) -> Mailbox:
    box = session.get(Mailbox, mailbox_id)
    if box is None:
        raise HTTPException(404, "Ящик не найден")
    return box


def _page(request: Request, session: Session, box: Mailbox, form: TransferForm, *,
          errors: dict | None = None, status_code: int = 200):
    return render(request, session, "mailboxes/transfer.html", {
        "box": box,
        "form": form,
        "errors": errors or {},
        "backup": last_backup(session, box.id),
        "active": active_run(session, box.id),
        "presets": _presets(),
        "provider_note": get_provider(form.provider).note,
    }, status_code)


@router.get("/mailboxes/{mailbox_id}/transfer")
def transfer_form(request: Request, mailbox_id: int, from_run: int | None = None,
                  session: Session = Depends(get_session)):
    box = _get_box(session, mailbox_id)
    prev = session.get(Run, from_run) if from_run else None
    if prev and prev.mailbox_id == box.id and prev.kind in TRANSFER_KINDS:
        form = TransferForm.from_run(prev)
    else:
        form = TransferForm.for_mailbox(box, has_backup=last_backup(session, box.id) is not None)
    return _page(request, session, box, form)


@router.post("/mailboxes/{mailbox_id}/transfer", dependencies=[Depends(csrf_protect)])
async def transfer_start(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    box = _get_box(session, mailbox_id)
    form = await TransferForm.from_request(request)
    dry_run = (await request.form()).get("action") != "run"
    if errors := form.validate(box, has_backup=last_backup(session, box.id) is not None):
        return _page(request, session, box, form, errors=errors, status_code=400)
    if active_run(session, box.id):
        # Параметры не теряем: форма остаётся, наверху — ссылка на текущий запуск
        return _page(request, session, box, form, status_code=409)

    run = enqueue_run(session, box, RunKind(form.mode), RunTrigger.MANUAL, dry_run=dry_run, **form.run_fields(box))
    what = "Восстановление" if run.kind == RunKind.RESTORE else "Перенос"
    flash(request, f"{what} «{box.name}» → {run.target_login} поставлено в очередь"
                   + (" (пробный прогон, ничего не копируется)" if dry_run else ""))
    return RedirectResponse(f"/runs/{run.id}", 303)


@router.post("/mailboxes/{mailbox_id}/transfer/check", dependencies=[Depends(csrf_protect)],
             response_class=HTMLResponse)
async def transfer_check(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    """Проверка входа в ящик-получатель (только LOGIN/LOGOUT)."""
    box = _get_box(session, mailbox_id)
    form = await TransferForm.from_request(request)
    if form.target == "same":
        host, port, security, login = box.host, box.port, Security(box.security), box.login
        password = crypto.decrypt(box.password_enc)
    else:
        host, login, password = form.host, form.login, form.password
        port = int(form.port) if form.port.isdigit() else 0
        security = Security(form.security) if form.security in {s.value for s in Security} else Security.SSL
    if not (host and port and login and password):
        result = {"ok": False, "message": "Заполните сервер, порт, логин и пароль получателя"}
    elif is_local_host(host):
        result = {"ok": False, "message": "Это адрес самого сервера бэкапов — туда восстанавливать нельзя"}
    else:
        res = await run_in_threadpool(check_login, host, port, security, login, password)
        result = {"ok": res.ok, "message": res.message}
    return templates.TemplateResponse(request, "mailboxes/_check.html", result)


@router.post("/runs/{run_id}/repeat", dependencies=[Depends(csrf_protect)])
def run_repeat(request: Request, run_id: int, session: Session = Depends(get_session)):
    """После удачного пробного прогона — тот же запуск по-настоящему."""
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Запуск не найден")
    if not run.dry_run or run.status not in DONE_OK or run.kind not in (RunKind.BACKUP, *TRANSFER_KINDS):
        flash(request, "Повторить по-настоящему можно только удачный пробный прогон", "error")
        return RedirectResponse(f"/runs/{run.id}", 303)
    if run.kind in TRANSFER_KINDS and not run.target_password_enc:
        flash(request, "Пароль получателя уже удалён из БД (хранится сутки) — введите его заново", "warn")
        return RedirectResponse(f"/mailboxes/{run.mailbox_id}/transfer?from_run={run.id}", 303)
    if existing := active_run(session, run.mailbox_id):
        flash(request, f"Ящик сейчас занят запуском #{existing.id} — дождитесь окончания", "error")
        return RedirectResponse(f"/runs/{run.id}", 303)

    target = {
        name: getattr(run, name)
        for name in ("target_host", "target_port", "target_security", "target_login",
                     "target_password_enc", "target_subfolder")
    }
    new = enqueue_run(session, run.mailbox, RunKind(run.kind), RunTrigger.MANUAL, dry_run=False, **target)
    # Пароль перешёл в новый запуск — у пробного он больше не нужен
    run.target_password_enc = None
    session.commit()
    flash(request, f"Запуск #{new.id} поставлен в очередь с параметрами пробного прогона #{run.id}")
    return RedirectResponse(f"/runs/{new.id}", 303)
