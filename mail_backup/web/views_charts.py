"""Графики: объёмы, размеры ящиков, длительность и результаты бэкапов."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import app_settings, stats
from ..db import get_session
from ..models import Mailbox
from .common import render, require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


def _to_int(value: str | None) -> int | None:
    """Число из параметра запроса; пустая строка («все ящики» в фильтре) и мусор — None."""
    try:
        return int(value) if value else None
    except ValueError:
        return None


@router.get("/charts")
def charts_page(request: Request, days: str | None = None, mailbox: str | None = None,
                session: Session = Depends(get_session)):
    # Параметры — строками: форма фильтра шлёт «mailbox=» для «все ящики»,
    # а int-параметр FastAPI на пустой строке отвечает 422
    days, mailbox = _to_int(days), _to_int(mailbox)
    if days not in stats.PERIODS:
        days = stats.DEFAULT_PERIOD
    boxes = list(session.scalars(select(Mailbox).order_by(Mailbox.name)))
    if mailbox is not None and not any(b.id == mailbox for b in boxes):
        mailbox = None
    data = stats.collect(session, app_settings.load(session).tz, days, mailbox)
    return render(request, session, "charts.html", {
        "data": data,
        "days": days,
        "periods": stats.PERIODS,
        "boxes": boxes,
        "mailbox": mailbox,
    })
