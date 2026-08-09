from fastapi import APIRouter, Depends, HTTPException

from app.core.errors import AuthenticationError
from app.core.security import require_native_principal
from app.dependencies import get_chat_history_service
from app.schemas.security import NativePrincipal
from app.schemas.sessions import AppendChatMessageRequest, ChatHistoryResponse, ChatMessage
from app.services.chat_history_service import ChatHistoryService

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.get("/sessions/{session_id}/messages", response_model=ChatHistoryResponse)
async def session_messages(
    session_id: str,
    principal: NativePrincipal = Depends(require_native_principal),
    chat_history: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatHistoryResponse:
    messages = await chat_history.get_host_history(
        session_id,
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
    )
    if not messages:
        raise HTTPException(status_code=404, detail="Session not found")
    return ChatHistoryResponse(session_id=session_id, messages=messages)


@router.post("/sessions/{session_id}/messages", response_model=ChatMessage)
async def append_session_message(
    session_id: str,
    payload: AppendChatMessageRequest,
    principal: NativePrincipal = Depends(require_native_principal),
    chat_history: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatMessage:
    try:
        return await chat_history.append_owned_message(
            session_id=session_id,
            payload=payload,
            tenant_id=principal.tenant_id,
            user_id=principal.subject,
        )
    except ValueError as exc:
        raise AuthenticationError(str(exc)) from exc
