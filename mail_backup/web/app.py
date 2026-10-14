"""Сборка FastAPI-приложения веб-панели."""

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp

from .. import crypto
from ..db import new_session
from . import dashboard, views_auth, views_mailboxes, views_runs, views_transfer
from .common import WEB_DIR, LoginRequired, render

SESSION_MAX_AGE = 12 * 3600


class _Sessions(SessionMiddleware):
    """Подписанная cookie-сессия; ключ читается при первом запросе, а не при импорте."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(
            app,
            secret_key=crypto.session_secret(),
            session_cookie="mb_session",
            max_age=SESSION_MAX_AGE,
            same_site="lax",
            # Cookie только по HTTPS (http://localhost через SSH-туннель браузеры тоже пускают)
            https_only=True,
        )


def create_app() -> FastAPI:
    app = FastAPI(title="mail_backup", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_Sessions)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
        )
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        # htmx-запросу нужен заголовок, обычному — редирект
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": exc.url})
        return RedirectResponse(exc.url, 303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        with new_session() as session:
            return render(request, session, "error.html", {"code": exc.status_code, "detail": exc.detail}, exc.status_code)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(views_auth.router)
    app.include_router(dashboard.router)
    app.include_router(views_mailboxes.router)
    app.include_router(views_runs.router)
    app.include_router(views_transfer.router)
    return app
