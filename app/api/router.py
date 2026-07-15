from fastapi import APIRouter, Depends

from app.core.security import MemoryActor, bind_trusted_user_context, require_memory_actor
from app.dependencies import get_invocation_service, get_plan_executor, get_router_service
from app.schemas.invocation import InvokeRequest, RouteAndInvokeResponse
from app.schemas.routing import RouteAndExecuteResponse, RouteRequest, RouteResponse
from app.services.invocation_service import InvocationService
from app.services.plan_executor import PlanExecutor
from app.services.router_service import RouterService

router = APIRouter(prefix="/api/v1", tags=["router"])


@router.post("/route", response_model=RouteResponse)
async def route(
    payload: RouteRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    router_service: RouterService = Depends(get_router_service),
) -> RouteResponse:
    payload = _bind_route_user(payload, actor)
    return await router_service.route(payload)


@router.post("/invoke")
async def invoke(
    payload: InvokeRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    invocation_service: InvocationService = Depends(get_invocation_service),
):
    payload = payload.model_copy(update={"user": bind_trusted_user_context(payload.user, actor)})
    return await invocation_service.invoke(payload)


@router.post("/route-and-invoke", response_model=RouteAndInvokeResponse)
async def route_and_invoke(
    payload: RouteRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    router_service: RouterService = Depends(get_router_service),
    invocation_service: InvocationService = Depends(get_invocation_service),
) -> RouteAndInvokeResponse:
    payload = _bind_route_user(payload, actor)
    route_response = await router_service.route(payload)
    result = await invocation_service.invoke_from_route(payload, route_response)
    return RouteAndInvokeResponse(route=route_response.model_dump(), result=result)


@router.post("/route-and-execute", response_model=RouteAndExecuteResponse)
async def route_and_execute(
    payload: RouteRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    router_service: RouterService = Depends(get_router_service),
    invocation_service: InvocationService = Depends(get_invocation_service),
    plan_executor: PlanExecutor = Depends(get_plan_executor),
) -> RouteAndExecuteResponse:
    payload = _bind_route_user(payload, actor)
    route_response = await router_service.route(payload)
    if route_response.plan is None:
        result = await invocation_service.invoke_from_route(payload, route_response)
        return RouteAndExecuteResponse(
            route=route_response,
            results=[result.model_dump(mode="json")] if result else [],
            next_action=route_response.next_action,
        )
    if route_response.execution_policy == "auto_execute":
        execution = await plan_executor.execute(
            route_response.plan.plan_id,
            user=payload.user,
            input_values={
                "text": payload.input.text,
                "query": payload.input.text,
                "title": payload.input.text,
            },
            context={"route_reason": route_response.decision.reason},
        )
        return RouteAndExecuteResponse(
            route=route_response.model_copy(update={"plan": execution.plan}),
            results=execution.results,
            next_action=execution.next_action,
        )
    return RouteAndExecuteResponse(
        route=route_response, results=[], next_action=route_response.next_action
    )


def _bind_route_user(payload: RouteRequest, actor: MemoryActor) -> RouteRequest:
    return payload.model_copy(update={"user": bind_trusted_user_context(payload.user, actor)})
