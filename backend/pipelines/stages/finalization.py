"""Finalization stage."""

from __future__ import annotations

from typing import Any, Dict

from .base import PipelineStageRunner
from ..shared import _compact_execution_artifacts, _json_safe, _render_prompt


class FinalizationStage(PipelineStageRunner):
    @staticmethod
    def _enforce_execution_outcome(result: Any, payload: Dict[str, Any]) -> Any:
        if not isinstance(result, dict):
            return result
        execution = payload.get("execution") or {}
        if not isinstance(execution, dict):
            return result
        status = str(execution.get("status") or "").casefold()
        if status == "passed":
            if isinstance(result.get("final_result"), dict):
                result["final_result"]["test_passed"] = "true"
            return result

        metadata = execution.get("test_results") or {}
        phase = metadata.get("execution_phase") if isinstance(metadata, dict) else None
        diagnosis = execution.get("stderr") or execution.get("stacktrace") or "O executor terminou sem status PASSED."
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result.update({
                "test_passed": "false",
                "automation_reliability": "LOW",
                "production_readiness": "NOT_READY",
                "business_validation_status": "NOT_VALIDATED" if phase == "script_validation" else "PARTIALLY_VALIDATED",
                "main_failure_reason": diagnosis[:1800],
                "technical_diagnosis": (
                    "A pré-validação rejeitou o script antes de iniciar os passos do teste; nenhuma interação nem asserção de negócio foi executada. "
                    if phase == "script_validation" else "A execução não terminou com status PASSED. "
                ) + diagnosis[:1800],
                "final_conclusion": "Não aprovar esta automação. Corrija o diagnóstico registrado pelo executor e execute novamente antes de avaliar o comportamento funcional.",
            })
        result["pipeline_status"] = "FAILED"
        return result

    async def run(
        self,
        current_provider: str,
        current_model: str,
        current_api_key: str,
        current_temperature: float,
        payload: Dict[str, Any],
        temperature: float | None = None,
        prompt_override: str | None = None,
        llm_override: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        provider, model, api_key, resolved_temperature = self.resolve_stage_llm(
            current_provider,
            current_model,
            current_api_key,
            current_temperature,
            "finalization",
            {"finalization": llm_override} if llm_override else None,
        )
        return await self.execute(
            payload,
            api_key,
            model,
            provider,
            temperature=temperature if temperature is not None else resolved_temperature,
            prompt_override=prompt_override,
        )

    async def execute(
        self,
        payload: Dict[str, Any],
        api_key: str,
        model: str,
        provider: str,
        temperature: float = 0.0,
        prompt_override: str | None = None,
    ) -> Dict[str, Any]:
        del provider
        prompt = prompt_override or self.prompt_manager.load_prompt("finalization")
        execution_artifacts = _compact_execution_artifacts(payload.get("execution"))
        homologation_artifacts = _compact_execution_artifacts(payload.get("execution"))
        final_prompt = _render_prompt(
            prompt,
            final_report=payload,
            pipeline_context={
                "pipeline_id": payload.get("pipeline_id"),
                "inputs": payload.get("inputs"),
                "structured": payload.get("structured"),
                "extracted": payload.get("extracted"),
                "refined": payload.get("refined"),
                "script": payload.get("script"),
                "execution": payload.get("execution"),
                "homologation": payload.get("homologation"),
                "timeline": payload.get("timeline"),
                "logs": payload.get("logs"),
                "prompts_used": payload.get("prompts_used"),
                "execution_artifacts": execution_artifacts,
                "homologation_artifacts": homologation_artifacts,
            },
            execution_results=payload.get("execution"),
            execution_screenshots=execution_artifacts.get("screenshots"),
            primary_screenshot=execution_artifacts.get("primary_screenshot"),
            image_evidence=execution_artifacts.get("image_evidence"),
            homologation_results=payload.get("homologation"),
            homologation_screenshots=homologation_artifacts.get("screenshots"),
            validation_data=payload.get("inputs", {}).get("validation"),
            manual_changes=payload.get("inputs", {}).get("manual_changes"),
            logs=payload.get("logs"),
            timeline=payload.get("timeline"),
        )
        result = self.llm.generate_json(api_key, model, final_prompt, temperature=temperature)
        return self._enforce_execution_outcome(_json_safe(result), payload)


__all__ = ["FinalizationStage"]
