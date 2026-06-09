"""NovelForge FastAPI app 装配（§8.1）。

基址：http://127.0.0.1:8787
前缀：/v1
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .deps import get_registry
from .security import _BYPASS_PATHS, check_api_key
from .api.projects import router as projects_router
from .api.memory import router as memory_router
from .api.governance import router as governance_router
from .api.orchestrator import router as orchestrator_router
from .api.autopilot import router as autopilot_router
from .api.volumes import router as volumes_router
from .api.cold_start import router as cold_start_router
from .api.craft import router as craft_router
from .api.sessions import router as sessions_router
from .api.admin import router as admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化项目注册表
    get_registry()
    yield
    # 关闭：nothing to cleanup


app = FastAPI(
    title="NovelForge",
    version="0.3.0",
    description="AI 网文写作引擎 REST API（MVP3）",
    lifespan=lifespan,
)


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Bearer / X-API-Key 认证中间件（§11）。"""
    # OPTIONS 预检（CORS）与豁免路径直接放行
    if request.method == "OPTIONS" or request.url.path in _BYPASS_PATHS:
        return await call_next(request)
    auth = request.headers.get("Authorization")
    x_key = request.headers.get("X-API-Key")
    if not check_api_key(auth, x_key):
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthorized", "message": "API key 无效或缺失"}},
        )
    return await call_next(request)


# CORS：允许 Vite 前端（dev :5173 / preview :4173）跨源调用。
# 在 api_key 中间件之后注册 → CORS 处于最外层，预检与错误响应都会带 CORS 头。
# 生产可经 NOVELFORGE_CORS_ORIGINS（逗号分隔）覆盖。
import os as _os
_cors_env = _os.environ.get("NOVELFORGE_CORS_ORIGINS")
_cors_origins = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env else
    ["http://localhost:5173", "http://127.0.0.1:5173",
     "http://localhost:4173", "http://127.0.0.1:4173"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 统一错误格式
@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse(status_code=404,
                        content={"error": {"code": "not_found", "message": str(exc.detail)}})


@app.exception_handler(409)
async def conflict(request, exc):
    return JSONResponse(status_code=409,
                        content={"error": {"code": "conflict", "message": str(exc.detail)}})


# 挂载路由（/v1 前缀）
PREFIX = "/v1"
app.include_router(projects_router, prefix=PREFIX)
app.include_router(memory_router, prefix=PREFIX)
app.include_router(governance_router, prefix=PREFIX)
app.include_router(orchestrator_router, prefix=PREFIX)
app.include_router(autopilot_router, prefix=PREFIX)
app.include_router(volumes_router, prefix=PREFIX)
app.include_router(cold_start_router, prefix=PREFIX)
app.include_router(craft_router, prefix=PREFIX)
app.include_router(sessions_router, prefix=PREFIX)
app.include_router(admin_router, prefix=PREFIX)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.3.0"}
