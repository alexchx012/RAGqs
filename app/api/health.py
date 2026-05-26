"""健康检查接口"""

from fastapi import APIRouter

from app.models.response import envelope_json_response
from app.operations.health import HealthChecker, create_default_health_checker


def create_health_router(checker: HealthChecker | None = None) -> APIRouter:
    router = APIRouter()
    active_checker = checker or create_default_health_checker()

    @router.get("/health")
    async def health_check():
        health_data, status_code = active_checker.as_response()
        return envelope_json_response(
            health_data,
            code=status_code,
            message="success" if status_code < 400 else "unhealthy",
            include_message=False,
        )

    return router


router = create_health_router()
