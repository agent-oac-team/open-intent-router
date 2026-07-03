from fastapi import APIRouter, Depends

from app.dependencies import get_chat_history_service
from app.schemas.sessions import AppendChatMessageRequest, ChatHistoryResponse, ChatMessage
from app.services.chat_history_service import ChatHistoryService

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.get("/sessions/{session_id}/messages", response_model=ChatHistoryResponse)
async def session_messages(
    session_id: str,
    chat_history: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatHistoryResponse:
    messages = await chat_history.get_host_history(session_id)
    return ChatHistoryResponse(session_id=session_id, messages=messages)


@router.post("/sessions/{session_id}/messages", response_model=ChatMessage)
async def append_session_message(
    session_id: str,
    payload: AppendChatMessageRequest,
    chat_history: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatMessage:
    return await chat_history.append_message(session_id=session_id, payload=payload)
