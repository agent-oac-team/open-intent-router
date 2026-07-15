import re
import subprocess
import sys
from pathlib import Path

from app.schemas.memory import MemoryFormationReasonCode

CHANGE_DIR = Path("openspec/changes/add-conversation-memory-auto-formation")


REQUIREMENT_COVERAGE = {
    "Conversation turn 展示实际 Recall Used": "web/src/App.test.tsx::Memory 和 Knowledge 标签按选中轮次分开展示",
    "Conversation turn 支持异步 Formation Decisions": "web/src/App.test.tsx::同一 multi-turn job 在参与轮次中共享且不重复 decision",
    "Memory inspector 分离 Recall 和 Formation 区域": "web/src/App.test.tsx::失败轮次不复用上一轮 memory/knowledge trace",
    "Pending decisions 提供受控用户操作": "web/src/App.test.tsx::五轮 job 展示完整 source range 并提供 pending 更新控制",
    "全局 Memory Debug 不得冒充本轮 trace": "web/src/App.test.tsx::debug API 失败时显示错误且保留选中轮次 trace",
    "所有自动记忆来源使用统一形成流水线": "tests/test_memory_invocation_plan_integration.py::test_real_processor_runs_conversation_jobs_through_rollout_mode",
    "完整对话轮次持久化为有界 Turn Capsule": "tests/test_memory_turn_capture.py::test_turn_capsule_builder_bounds_text_and_keeps_only_canonical_refs",
    "普通对话按五轮窗口触发形成": "tests/test_memory_trigger_runtime.py::test_coordinator_freezes_five_turns_resets_idle_and_starts_next_range",
    "普通对话在空闲三十秒时触发形成": "tests/test_memory_trigger_runtime.py::test_idle_sweeper_recovers_persisted_deadline_after_restart",
    "Formation job 对竞态和重试幂等": "tests/test_memory_trigger_runtime.py::test_fifth_turn_and_idle_race_create_only_one_executable_job",
    "普通对话由 OIR 形成模型输出严格候选": "tests/test_conversation_formation_model.py::test_openai_formation_adapter_uses_independent_model_prompt_and_timeout",
    "结构化任务事件立即形成确定性投影": "tests/test_memory_invocation_plan_integration.py::test_structured_plan_jobs_are_idempotent_and_update_one_task_memory",
    "普通形成不阻塞主对话响应": "tests/test_memory_invocation_plan_integration.py::test_invocation_captures_after_run_result_and_capture_failure_keeps_success",
    "自动形成支持可回滚 rollout": "tests/test_memory_invocation_plan_integration.py::test_real_processor_runs_conversation_jobs_through_rollout_mode",
    "mem0 只存储 OIR 已治理的单条 canonical memory": "tests/test_memory_indexing.py::test_governed_add_sends_one_canonical_item_with_inference_disabled",
    "mem0 adapter 支持按 external ID 更新 canonical memory": "tests/test_memory_indexing.py::test_add_adopts_existing_record_removes_duplicates_and_update_is_in_place",
    "mem0 搜索结果受 canonical lifecycle 校验": "tests/test_memory_indexing.py::test_search_uses_only_matching_active_canonical_state_and_content",
    "mem0 写入经过 OIR 治理": "tests/test_mem0_memory_loop.py::test_memory_service_policy_happens_before_adapter_extract",
    "mem0 ID 与 OIR ID 可追踪": "tests/test_memory_indexing.py::test_index_worker_completes_mapping_and_never_completes_provider_failure",
    "Task memory 只作为 canonical 任务状态投影": "tests/test_memory_invocation_plan_integration.py::test_task_memory_resolves_owned_canonical_plan_only_for_continuation",
    "mem0 provides memory strategy behind an adapter": "tests/test_mem0_memory_loop.py::test_mem0_adapter_add_search_delete_and_history_traceability",
    "OIR governs memory lifecycle": "tests/test_memory_lifecycle_service.py::test_lifecycle_add_update_revision_and_idempotency",
    "Memory TTL defaults are scope-specific": "tests/test_memory_lifecycle_service.py::test_ttl_sweeper_uses_same_deletion_path",
    "Memory observability is admin/debug visible": "tests/test_memory_debug_management_metrics.py::test_debug_filters_assemble_bounded_separate_formation_trace",
    "OIR 确定性 policy 拥有最终 side-effect 裁决权": "tests/test_memory_candidate_policy.py::test_policy_thresholds_add_pending_and_reject",
    "候选策略分离确定性硬规则与结构化语义校验": "tests/test_memory_candidate_policy_layers.py::test_semantic_validator_rejects_field_inconsistency_to_pending",
    "可选语义 verifier 只能提供只读复核信号": "tests/test_memory_candidate_policy_layers.py::test_confirmed_verifier_reruns_current_state_before_accepting",
    "长期记忆必须有可验证证据和敏感信息治理": "tests/test_memory_candidate_policy.py::test_policy_rejects_invalid_assistant_only_and_recalled_evidence",
    "Memory key 与 candidate hash 分别支持逻辑身份和幂等": "tests/test_memory_candidate_policy.py::test_memory_key_and_candidate_hash_are_deterministic_and_bounded",
    "Current projection 与 revision history 分离": "tests/test_memory_lifecycle_service.py::test_lifecycle_add_update_revision_and_idempotency",
    "去重和冲突在 lifecycle side effect 前完成": "tests/test_memory_candidate_policy.py::test_policy_same_value_override_update_and_ambiguous_conflict",
    "用户删除必须唯一授权并硬删除全部正文": "tests/test_memory_indexing.py::test_delete_worker_hard_deletes_after_provider_success",
    "TTL 到期执行不可召回和物理删除": "tests/test_memory_lifecycle_service.py::test_ttl_sweeper_uses_same_deletion_path",
    "Canonical state 与派生向量索引可恢复一致": "tests/test_memory_indexing.py::test_repair_rebuilds_missing_and_cleans_duplicate_and_orphan_records",
    "后台 consolidation 保持 policy 和 revision 语义": "tests/test_memory_lifecycle_service.py::test_consolidation_discovers_partitioned_duplicates_and_summaries",
    "Memory 生命周期严格隔离 tenant 和主体": "tests/test_memory_end_to_end_acceptance.py::test_cross_tenant_formation_consolidation_debug_management_and_recall_isolation",
    "Memory Debug API 支持形成与生命周期关联查询": "tests/test_memory_debug_management_metrics.py::test_debug_filters_assemble_bounded_separate_formation_trace",
    "Formation Trace 解释每个候选和 lifecycle operation": "tests/test_memory_debug_management_metrics.py::test_debug_filters_assemble_bounded_separate_formation_trace",
    "Memory 调试和审计默认不暴露敏感正文": "tests/test_memory_debug_management_metrics.py::test_debug_redacts_secrets_regulated_values_and_internal_job_payload",
    "用户和管理员可以受控管理 Memory lifecycle": "tests/test_memory_debug_management_metrics.py::test_pending_update_confirm_is_preconditioned_and_idempotent",
    "Formation 和 lifecycle 指标支持上线治理": "tests/test_memory_debug_management_metrics.py::test_health_and_metrics_are_content_free",
    "Plans have required server-controlled ownership": "tests/test_routing_invocation.py::test_router_overwrites_forged_plan_owner_requires_tenant_and_preserves_event_owner",
    "Plan ownership has no legacy compatibility path": "tests/test_plan_repository_ownership.py::test_plan_http_contract_rejects_missing_or_cross_user_identity",
    "Plan execution retries expose a stable idempotency contract": "tests/test_plan_repository_ownership.py::test_expired_claim_reuses_execution_idempotency_key",
    "Router can create multi-step plans": "tests/test_routing_invocation.py::test_mock_router_creates_and_persists_multi_agent_plan",
}


REASON_CODE_COVERAGE = {
    "accepted_new": "tests/test_memory_candidate_policy.py::test_policy_thresholds_add_pending_and_reject",
    "accepted_update": "tests/test_memory_candidate_policy.py::test_structured_canonical_change_authoritatively_updates_current",
    "authorized_delete": "tests/test_memory_candidate_policy.py::test_delete_requires_user_evidence_unique_current_and_matching_target",
    "same_value": "tests/test_memory_candidate_policy.py::test_policy_same_value_override_update_and_ambiguous_conflict",
    "duplicate_candidate": "tests/test_memory_invocation_plan_integration.py::test_out_of_order_structured_workers_cannot_regress_task_memory",
    "confidence_pending": "tests/test_memory_candidate_policy.py::test_policy_thresholds_add_pending_and_reject",
    "confidence_low": "tests/test_memory_candidate_policy.py::test_policy_thresholds_add_pending_and_reject",
    "ambiguous_conflict": "tests/test_memory_candidate_policy.py::test_policy_same_value_override_update_and_ambiguous_conflict",
    "ambiguous_delete": "tests/test_memory_candidate_policy.py::test_delete_requires_user_evidence_unique_current_and_matching_target",
    "current_turn_override": "tests/test_memory_candidate_policy.py::test_policy_same_value_override_update_and_ambiguous_conflict",
    "assistant_only_evidence": "tests/test_memory_candidate_policy.py::test_policy_rejects_invalid_assistant_only_and_recalled_evidence",
    "recalled_memory_repetition": "tests/test_memory_candidate_policy.py::test_policy_rejects_invalid_assistant_only_and_recalled_evidence",
    "sensitive_content": "tests/test_memory_candidate_policy.py::test_policy_rejects_sensitive_values_with_redacted_trace",
    "invalid_evidence": "tests/test_memory_candidate_policy.py::test_policy_rejects_invalid_assistant_only_and_recalled_evidence",
    "invalid_scope": "tests/test_memory_candidate_policy.py::test_policy_reconstructs_trusted_identity_and_scope",
    "identity_mismatch": "tests/test_memory_candidate_policy.py::test_policy_reconstructs_trusted_identity_and_scope",
    "temporary_request": "tests/test_memory_turn_capture.py::test_temporary_request_skips_buffer_and_records_only_redacted_event",
    "ttl_expired": "tests/test_memory_lifecycle_service.py::test_ttl_sweeper_uses_same_deletion_path",
    "provider_error": "tests/test_memory_lifecycle_service.py::test_delete_failure_never_reactivates_and_dead_letter_scrubs_ledger",
}


def _requirement_titles() -> set[str]:
    titles = set()
    for path in (CHANGE_DIR / "specs").glob("*/spec.md"):
        titles.update(
            match.group(1).strip()
            for match in re.finditer(
                r"^### Requirement: (.+)$",
                path.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
    return titles


def _assert_locator_exists(locator: str) -> None:
    path_value, marker = locator.split("::", 1)
    path = Path(path_value)
    assert path.is_file(), locator
    assert marker in path.read_text(encoding="utf-8"), locator


def test_each_openspec_requirement_has_an_executable_acceptance_scenario() -> None:
    assert set(REQUIREMENT_COVERAGE) == _requirement_titles()
    for locator in REQUIREMENT_COVERAGE.values():
        _assert_locator_exists(locator)
    _assert_python_locators_are_collected(REQUIREMENT_COVERAGE.values())


def test_each_formation_reason_code_has_a_behavior_test() -> None:
    assert set(REASON_CODE_COVERAGE) == {reason.value for reason in MemoryFormationReasonCode}
    for locator in REASON_CODE_COVERAGE.values():
        _assert_locator_exists(locator)
    _assert_python_locators_are_collected(REASON_CODE_COVERAGE.values())


def _assert_python_locators_are_collected(locators) -> None:
    python_locators = sorted({locator for locator in locators if locator.startswith("tests/")})
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *python_locators],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for locator in python_locators:
        assert locator in collected, f"pytest did not collect {locator}"
