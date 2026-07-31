from fastapi import APIRouter, Depends, HTTPException, status

from app.adapters.knowledge_sys import load_signing_private_key, public_jwks
from app.core.config import Settings, get_settings

router = APIRouter(tags=["identity"])


@router.get("/.well-known/jwks.json")
def get_provider_jwks(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    if not settings.knowledge_provider_base_url or not settings.knowledge_provider_jwt_key_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="knowledge_provider_issuer_not_configured",
        )
    try:
        private_key = load_signing_private_key(
            pem=settings.knowledge_provider_jwt_private_key,
            file_path=settings.knowledge_provider_jwt_private_key_file,
        )
        return public_jwks(
            signing_private_key=private_key,
            signing_key_id=settings.knowledge_provider_jwt_key_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge_provider_issuer_unavailable",
        ) from exc
