"""Versioned API routers owned by the platform and later domain changes."""

from fastapi import APIRouter

from .admin import router as admin_router
from .approvals import router as approvals_router
from .auth import router as auth_router
from .conversations import router as conversations_router
from .documents import router as documents_router
from .evaluation import router as evaluation_router
from .generations import router as generations_router
from .graph_builds import router as graph_builds_router
from .health import router as health_router
from .metrics import router as metrics_router
from .notifications import router as notifications_router
from .ops import router as ops_router
from .ops_backups import router as ops_backups_router
from .prompt_enhancements import router as prompt_enhancements_router
from .quota import router as quota_router
from .spaces import router as spaces_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(metrics_router)
router.include_router(spaces_router)
router.include_router(admin_router)
router.include_router(notifications_router)
router.include_router(ops_router)
router.include_router(ops_backups_router)
router.include_router(quota_router)
router.include_router(prompt_enhancements_router)
router.include_router(approvals_router)
router.include_router(documents_router)
router.include_router(conversations_router)
router.include_router(generations_router)
router.include_router(graph_builds_router)
router.include_router(evaluation_router)

__all__ = ["router"]
