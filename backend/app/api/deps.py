"""Shared request dependencies."""

from __future__ import annotations
from typing import Annotated
from fastapi import Depends, Header, HTTPException, Request, status
from ..config import Settings, get_settings
from ..providers.registry import ProviderBundle
from ..worker.runner import CampaignWorker


def settings_dep() -> Settings:
    return get_settings()


def providers_dep(request: Request) -> ProviderBundle:
    bundle = getattr(request.app.state, "providers", None)
    if bundle is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "providers are not initialised"
        )
    return bundle


def worker_dep(request: Request) -> CampaignWorker:
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "background worker is not running"
        )
    return worker


def idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Optional client-supplied key for safe retries of a mutating request.
    """
    if key is None:
        return None
    key = key.strip()
    if not 8 <= len(key) <= 128:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Idempotency-Key must be 8-128 characters"
        )
    return key


SettingsDep = Annotated[Settings, Depends(settings_dep)]
ProvidersDep = Annotated[ProviderBundle, Depends(providers_dep)]
WorkerDep = Annotated[CampaignWorker, Depends(worker_dep)]
IdempotencyDep = Annotated[str | None, Depends(idempotency_key)]
