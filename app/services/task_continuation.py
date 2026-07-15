import re

from app.schemas.memory import MemoryRecallRequest
from app.schemas.routing import RouteRequest

_CONTINUATION_PATTERNS = (
    re.compile(
        r"(?:继续|接着|恢复|回到)(?:处理|执行|完成|做)?"
        r"(?:上次|之前|先前|刚才|未完成|原来|那个)(?:的)?(?:任务|计划|工作)"
    ),
    re.compile(r"(?:上次|之前|未完成)(?:的)?(?:任务|计划|工作).*(?:继续|接着|恢复)"),
    re.compile(
        r"(?i)\b(?:continue|resume|pick\s+up|carry\s+on)(?:\s+(?:the|my|our))?"
        r"\s+(?:last|previous|unfinished)\s+(?:task|plan|work)\b"
    ),
    re.compile(r"(?i)\bwhere\s+(?:we|i)\s+left\s+off\b"),
)

_CONTINUATION_EXCLUSIONS = (
    re.compile(r"(?:不要|别|不再|无需|停止|取消).{0,8}(?:继续|接着|恢复|回到)"),
    re.compile(r"(?:全新|新的|新建|改做|换成|另一个|其他).{0,12}(?:任务|计划|项目|工作)"),
    re.compile(r"(?i)\b(?:do\s+not|don't|stop|cancel)\s+(?:continue|resume)\b"),
    re.compile(r"(?i)\b(?:new|different|another)\s+(?:task|plan|project|work)\b"),
)

_ACTIVE_PLAN_STATUSES = {"pending", "running", "blocked"}


def requests_plan_continuation(text: str) -> bool:
    normalized = " ".join(text.split())
    if not normalized or any(pattern.search(normalized) for pattern in _CONTINUATION_EXCLUSIONS):
        return False
    return any(pattern.search(normalized) for pattern in _CONTINUATION_PATTERNS)


class TaskMemoryPlanResolver:
    def __init__(self, *, memory_service, plan_service) -> None:
        self.memory_service = memory_service
        self.plan_service = plan_service

    async def resolve(self, request: RouteRequest):
        tenant_id = request.user.tenant_id
        if not tenant_id or not requests_plan_continuation(request.input.text):
            return None
        recalled = await self.memory_service.recall(
            MemoryRecallRequest(
                query=request.input.text,
                user=request.user,
                scopes=["task_memory"],
                subject_type="user",
                subject_id=request.user.id,
                max_items=20,
            )
        )
        for item in recalled.context.items:
            plan_id = _plan_id(item)
            if not plan_id:
                continue
            plan = await self.plan_service.get_plan(
                plan_id,
                tenant_id=tenant_id,
                user_id=request.user.id,
            )
            if plan is not None and plan.status in _ACTIVE_PLAN_STATUSES:
                return plan
        return None


def _plan_id(item) -> str | None:
    structured = item.structured_value
    if structured.get("object_type") == "plan" and structured.get("plan_id"):
        return str(structured["plan_id"])
    value = item.metadata.get("plan_id")
    return str(value) if value else None


__all__ = ["TaskMemoryPlanResolver", "requests_plan_continuation"]
