"""Веб-приложение (админка). Пока только проверка работоспособности."""

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_session

app = FastAPI(title="mail_backup", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
