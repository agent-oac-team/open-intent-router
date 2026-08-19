from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass
class ErrorPayload:
    code: str
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = _json_safe(self.details)
        return payload


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class AuthenticationError(AppError):
    status_code = 401
    code = "authentication_error"


class RegistryError(AppError):
    code = "registry_error"


class RegistryUnavailableError(RegistryError):
    status_code = 503
    code = "registry_unavailable"


class RegistryVersionConflict(RegistryError):
    status_code = 409
    code = "registry_version_conflict"


class RoutingError(AppError):
    code = "routing_error"


class LLMError(AppError):
    status_code = 502
    code = "llm_error"


class InvocationError(AppError):
    status_code = 502
    code = "invocation_error"


class DirectInvocationUnsupportedError(AppError):
    """A Direct Invoke target uses a non-Invocation Handling branch."""

    status_code = 409
    code = "direct_invocation_not_supported"


class InvocationBindingUnavailableError(AppError):
    """A selected Invocation Definition cannot bind in this deployment."""

    status_code = 503
    code = "invocation_binding_unavailable"


class RuntimeCatalogUnavailableError(AppError):
    status_code = 503
    code = "runtime_catalog_unavailable"


class AgentUnavailableError(AppError):
    status_code = 404
    code = "agent_not_available"


class StorageError(AppError):
    status_code = 503
    code = "storage_error"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": ErrorPayload(exc.code, exc.message, exc.details).to_dict()},
        )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)
