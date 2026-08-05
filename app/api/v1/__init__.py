"""Versioned API routers owned by the platform and later domain changes."""

from fastapi import APIRouter

from .health import router as health_router

router = APIRouter()
router.include_router(health_router)

__all__ = ["router"]
