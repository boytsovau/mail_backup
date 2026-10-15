"""Первичная настройка, вход и выход."""

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_session
from .common import csrf_protect, flash, render, safe_next

router = APIRouter()

# Защита от перебора пароля: не больше MAX_FAILS неудач с одного IP за окно
MAX_FAILS = 5
FAIL_WINDOW = 300
_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _blocked(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


@router.get("/setup")
def setup_page(request: Request, token: str = "", session: Session = Depends(get_session)):
    if auth.admin_exists(session):
        return RedirectResponse("/login", 303)
    valid = auth.check_setup_token(session, token)
    return render(request, session, "setup.html", {"token": token, "valid": valid}, 200 if valid else 403)


@router.post("/setup", dependencies=[Depends(csrf_protect)])
def setup_submit(
    request: Request,
    token: str = Form(""),
    login: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    session: Session = Depends(get_session),
):
    if not auth.check_setup_token(session, token):
        return render(request, session, "setup.html", {"token": token, "valid": False}, 403)
    error = None
    if password != password2:
        error = "Пароли не совпадают"
    else:
        try:
            auth.set_admin(session, login, password)
        except ValueError as exc:
            error = str(exc)
    if error:
        return render(request, session, "setup.html", {"token": token, "valid": True, "error": error, "login": login}, 400)
    request.session["admin"] = auth.session_version(session)
    flash(request, "Администратор создан, добро пожаловать!")
    return RedirectResponse("/", 303)


@router.get("/login")
def login_page(request: Request, next: str = "/", session: Session = Depends(get_session)):
    if not auth.admin_exists(session):
        return RedirectResponse("/setup", 303)
    return render(request, session, "login.html", {"next": safe_next(next)})


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login_submit(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    session: Session = Depends(get_session),
):
    ip = _client_ip(request)
    if _blocked(ip):
        return render(
            request, session, "login.html",
            {"next": safe_next(next), "error": "Слишком много попыток, подождите 5 минут", "login": login}, 429,
        )
    if not auth.check_admin(session, login, password):
        _fails.setdefault(ip, []).append(time.monotonic())
        return render(
            request, session, "login.html",
            {"next": safe_next(next), "error": "Неверный логин или пароль", "login": login}, 401,
        )
    _fails.pop(ip, None)
    # Новая сессия после входа: старый CSRF-токен и прочее не переносим
    request.session.clear()
    request.session["admin"] = auth.session_version(session)
    return RedirectResponse(safe_next(next), 303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)
