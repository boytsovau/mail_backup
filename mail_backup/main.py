"""Точка входа веб-панели: uvicorn mail_backup.main:app."""

from .web.app import create_app

app = create_app()
