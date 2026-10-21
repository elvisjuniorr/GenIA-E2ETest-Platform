"""Homologation stage."""

from __future__ import annotations

import hashlib
from typing import Any, Dict

from backend.domain.models import ExecutionResult

from .base import PipelineStageRunner
from ..shared import _append_context_block, _compact_execution_artifacts, _json_safe, _render_prompt


class HomologationStage(PipelineStageRunner):
    @staticmethod
    def _enforce_execution_facts(result: Any, execution_result: ExecutionResult, script: str) -> Any:
        if not isinstance(result, dict):
            return result

        metadata = execution_result.test_results or {}
        phase = metadata.get("execution_phase")
        source_hash = metadata.get("source_script_sha256")
        supplied_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if source_hash and source_hash != supplied_hash:
            phase = "script_mismatch"

        if execution_result.status == "passed" and phase != "script_mismatch":
            return result

        result["homologation_status"] = "FAILED"
        if phase == "script_validation":
            framework = str(execution_result.framework or "executor").casefold()
            runner_name = "Robot Framework" if framework == "robotframework" else "o executor"
            detail = execution_result.stderr or execution_result.stacktrace or f"{runner_name} rejeitou o script na pré-validação."
            result.update({
                "execution_analysis": "A pré-validação do script falhou antes de o runner iniciar os passos do teste. Nenhuma interação com a interface (quando aplicável) nem asserção de negócio foi executada.",
                "logs_analysis": f"Falha na fase script_validation. {runner_name} rejeitou o script antes da execução do teste. Diagnóstico registrado: {detail[:1800]}",
                "success_rate": "0%: o teste não chegou a executar seus passos.",
                "failure_count": "1",
                "probable_cause": detail[:1800],
                "broken_elements": "Não avaliados; o teste não chegou à interação com a interface.",
                "selector_issues": "Não avaliados; o teste não chegou à interação com elementos.",
                "invalid_fields": "Não avaliados; nenhum formulário foi preenchido.",
                "execution_time": f"{execution_result.duration:.2f} segundos; falha durante a pré-validação.",
                "executed_steps": f"Preparação e pré-validação do script por {runner_name}.",
                "failed_steps": "A pré-validação rejeitou o script. Nenhum passo do teste foi executado.",
                "improvement_suggestions": "Corrigir o erro de sintaxe indicado pelo Robot Framework e executar novamente. A pré-validação do script deve passar antes da análise de seletores ou do comportamento da interface.",
                "general_recommendations": "Usar os diagnósticos da pré-validação como fonte da causa técnica e não inferir problemas de interface antes de o navegador iniciar.",
                "final_conclusion": "Execução reprovada antes do início dos passos do teste. O resultado não valida o comportamento funcional e não deve ser aprovado como teste executado.",
            })
        else:
            detail = execution_result.stderr or execution_result.stacktrace or "A execução terminou sem status PASSED."
            result["failed_steps"] = result.get("failed_steps") or detail[:1800]
            result["probable_cause"] = result.get("probable_cause") or detail[:1800]
            result["final_conclusion"] = result.get("final_conclusion") or "Execução reprovada; consulte os diagnósticos do executor antes de aprovar o teste."
            result["success_rate"] = result.get("success_rate") or "Não foi possível confirmar sucesso; a execução terminou com falha."

        if phase == "script_mismatch":
            result["homologation_status"] = "FAILED"
            result["probable_cause"] = "O hash do script recebido pela Homologação não corresponde ao script de origem registrado pelo executor; não há evidência de que o relatório analisou o mesmo script executado."
            result["final_conclusion"] = "Homologação reprovada por divergência de proveniência. Reexecute a pipeline com o script confirmado e correlacione o relatório ao hash registrado pelo executor."
        return result

    async def run(
        self,
        current_provider: str,
        current_model: str,
        current_api_key: str,
        current_temperature: float,
        execution_result: ExecutionResult,
        refined: Any,
        validation_data: Dict[str, Any],
        manual_changes: Dict[str, Any],
        script: str,
        temperature: float | None = None,
        prompt_override: str | None = None,
        llm_override: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        provider, model, api_key, resolved_temperature = self.resolve_stage_llm(
            current_provider,
            current_model,
            current_api_key,
            current_temperature,
            "homologation",
            {"homologation": llm_override} if llm_override else None,
        )
        return await self.execute(
            execution_result,
            refined,
            validation_data,
            manual_changes,
            script,
            api_key,
            model,
            provider,
            temperature=temperature if temperature is not None else resolved_temperature,
            prompt_override=prompt_override,
        )

    async def execute(
        self,
        execution_result: ExecutionResult,
        refined: Any,
        validation_data: Dict[str, Any],
        manual_changes: Dict[str, Any],
        script: str,
        api_key: str,
        model: str,
        provider: str,
        temperature: float = 0.0,
        prompt_override: str | None = None,
    ) -> Dict[str, Any]:
        del provider
        prompt = prompt_override or self.prompt_manager.load_prompt("homologation")
        visual_context = _compact_execution_artifacts(execution_result)
        final_prompt = _append_context_block(
            _render_prompt(
                prompt,
                execution_results=execution_result,
                execution=execution_result,
                execution_screenshots=visual_context.get("screenshots"),
                primary_screenshot=visual_context.get("primary_screenshot"),
                image_evidence=visual_context.get("image_evidence"),
                refined_data=refined,
                refined=refined,
                validation_data=validation_data,
                manual_changes=manual_changes,
                manual=manual_changes,
                generated_script=script,
                script=script,
                homologation_context={
                    "execution_result": execution_result,
                    "visual_context": visual_context,
                    "refined": refined,
                    "validation": validation_data,
                    "manual_changes": manual_changes,
                    "script": script,
                    "script_provenance": (execution_result.test_results or {}),
                },
            ),
            {
                "execution_results": execution_result,
                "execution_screenshots": visual_context.get("screenshots"),
                "primary_screenshot": visual_context.get("primary_screenshot"),
                "image_evidence": visual_context.get("image_evidence"),
                "refined_data": refined,
                "validation_data": validation_data,
                "manual_changes": manual_changes,
                "generated_script": script,
                "script_provenance": (execution_result.test_results or {}),
            },
        )
        result = self.llm.generate_json(api_key, model, final_prompt, temperature=temperature)
        return self._enforce_execution_facts(_json_safe(result), execution_result, script)


__all__ = ["HomologationStage"]
