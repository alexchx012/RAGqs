"""健康检查接口"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.operations.health import HealthChecker, create_default_health_checker


def create_health_router(checker: HealthChecker | None = None) -> APIRouter:
    router = APIRouter()
    active_checker = checker or create_default_health_checker()

    @router.get("/health")
    async def health_check():
        health_data, status_code = active_checker.as_response()
        return JSONResponse(status_code=status_code, content={"code": status_code, "data": health_data})

    return router


router = create_health_router()
