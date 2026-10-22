from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from agentpal.auth.api import router
from agentpal.auth.config import AuthSettings
from agentpal.auth.service import AuthService
from agentpal.policy import router as policy_router
from agentpal.approvals import router as approval_router
from agentpal.vault.api import router as vault_router
from agentpal.vault.security import VaultUnavailable


def create_app(*, settings: AuthSettings | None = None, engine: Engine | None = None) -> FastAPI:
    if (settings is None) != (engine is None):
        raise ValueError("Provide both authentication settings and engine when injecting dependencies")

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            if application.state.auth_owned_engine and application.state.auth_service is not None:
                application.state.auth_service.engine.dispose()

    application = FastAPI(title="AgentPal API", version="0.1.0", lifespan=lifespan)
    application.state.auth_lock = Lock()
    application.state.auth_owned_engine = False
    application.state.auth_service = AuthService(engine, settings) if engine is not None else None
    application.state.vault_service = None
    application.include_router(router)
    application.include_router(policy_router)
    application.include_router(approval_router)
    application.include_router(vault_router)

    @application.middleware("http")
    async def authentication_headers(request: Request, call_next):
        if request.url.path in ("/paypal/vault/return", "/paypal/vault/cancel"):
            # Uvicorn access logging observes this shared ASGI scope at response
            # time. Keep correlation/provider parameters out of its request URL.
            request.scope["vault_callback_query"] = request.scope.get("query_string", b"")
            request.scope["query_string"] = b""
        response = await call_next(request)
        if request.url.path.startswith(("/auth/", "/policy", "/budget/", "/purchases/", "/paypal/vault/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/paypal/vault/"):
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @application.exception_handler(VaultUnavailable)
    async def vault_unavailable(request: Request, error: VaultUnavailable):
        return JSONResponse(status_code=503, content={"detail": "Vault unavailable"},
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, error: SQLAlchemyError):
        # Do not expose SQL, URLs, password hashes, session material or parameters.
        if request.url.path.startswith("/purchases/") and isinstance(error, DBAPIError):
            state = getattr(error.orig, "sqlstate", None)
            if state in ("40001", "40P01", "23505", "23514"):
                code = ("transaction_conflict" if state in ("40001", "40P01") else
                        "decision_conflict" if state == "23505" else "approval_ineligible")
                return JSONResponse(status_code=409, content={"detail": {
                    "code": code, "message": "Reload the proposal and recorded decision before retrying"}},
                    headers={"Cache-Control": "no-store"})
        if (request.url.path in ("/policy", "/budget/current") and isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) in ("40001", "40P01")):
            return JSONResponse(status_code=409, content={"detail": {
                "code": "transaction_conflict", "message": "Refresh policy/budget and retry the same request"}},
                headers={"Cache-Control": "no-store"})
        return JSONResponse(status_code=503, content={"detail": "Service unavailable"},
                            headers={"Cache-Control": "no-store"})

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
