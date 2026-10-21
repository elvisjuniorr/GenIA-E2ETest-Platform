"""Refactoring stage."""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Dict

from backend.domain.models import ExecutionResult
from backend.infrastructure.frameworks_languages import ExecutionStrategyRegistry
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .base import PipelineStageRunner
from ..shared import _append_context_block, _compact_execution_artifacts, _json_safe, _render_prompt


class RefactoringResponse(BaseModel):
    """Stable API contract returned by the refactoring stage."""

    model_config = ConfigDict(extra="ignore")

    original_script: str = Field(min_length=1)
    refactored_script: str = Field(min_length=1)
    diff: str = Field(min_length=1)
    justification: str = Field(min_length=1)
    possible_errors: str = Field(min_length=1)
    implemented_improvements: str = Field(min_length=1)
    suggested_follow_up: str = Field(min_length=1)

    @field_validator("*")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("refactoring response fields must not be blank")
        return value


_REFACTORING_FIELD_ALIASES = {
    "original_script": {"originalscript", "currentscript", "executedscript", "scriptoriginal", "scriptatual"},
    "refactored_script": {"refactoredscript", "improvedscript", "scriptrefatorado", "script"},
    "diff": {"diff", "scriptdiff", "changes", "differences", "alteracoes", "diferencas"},
    "justification": {"justification", "rationale", "reasoning", "justificativa"},
    "possible_errors": {"possibleerrors", "potentialerrors", "risks", "observedissues", "errospossiveis", "riscospossiveis"},
    "implemented_improvements": {"implementedimprovements", "improvementsimplemented", "implementedchanges", "melhoriasimplementadas"},
    "suggested_follow_up": {"suggestedfollowup", "suggestedfollowups", "followup", "nextsteps", "proximospassos"},
}


def _normalize_refactoring_fields(payload: Any) -> dict[str, str]:
    """Map common LLM key variants to the exact response contract."""
    if not isinstance(payload, dict):
        return {}

    names = {
        re.sub(r"[^a-z0-9]", "", alias.lower()): field
        for field, aliases in _REFACTORING_FIELD_ALIASES.items()
        for alias in ({field, *aliases})
    }
    normalized: dict[str, str] = {}

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            field = names.get(re.sub(r"[^a-z0-9]", "", str(key).lower()))
            if field and field not in normalized and item is not None and item != "":
                text = item.strip() if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                if text:
                    normalized[field] = text
        for item in value.values():
            if isinstance(item, dict):
                visit(item)

    visit(payload)
    return normalized


def _describe_script_diff(before: str, after: str) -> str:
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines())
    removed: list[str] = []
    added: list[str] = []
    for operation, start_before, end_before, start_after, end_after in matcher.get_opcodes():
        if operation in {"replace", "delete"}:
            removed.extend(before.splitlines()[start_before:end_before])
        if operation in {"replace", "insert"}:
            added.extend(after.splitlines()[start_after:end_after])

    if not removed and not added:
        return "Nenhuma alteração textual foi aplicada ao script."

    sections = []
    if removed:
        sections.append("Removido:\n" + "\n".join(removed[:20]))
    if added:
        sections.append("Adicionado:\n" + "\n".join(added[:20]))
    return "\n\n".join(sections)


class RefactoringStage(PipelineStageRunner):
    MAX_SCRIPT_REPAIR_ATTEMPTS = 2

    async def run(
        self,
        current_provider: str,
        current_model: str,
        current_api_key: str,
        current_temperature: float,
        original_script: str,
        execution_result: ExecutionResult,
        homologation: Dict[str, Any],
        final_report: Dict[str, Any],
        temperature: float | None = None,
        prompt_override: str | None = None,
        llm_override: Dict[str, Any] | None = None,
        framework: str | None = None,
        language: str | None = None,
    ) -> Dict[str, Any]:
        provider, model, api_key, resolved_temperature = self.resolve_stage_llm(
            current_provider,
            current_model,
            current_api_key,
            current_temperature,
            "refactoring",
            {"refactoring": llm_override} if llm_override else None,
        )
        return await self.execute(
            original_script,
            execution_result,
            homologation,
            final_report,
            api_key,
            model,
            provider,
            temperature=temperature if temperature is not None else resolved_temperature,
            prompt_override=prompt_override,
            framework=framework or execution_result.framework,
            language=language or (execution_result.test_results or {}).get("language"),
        )

    async def execute(
        self,
        original_script: str,
        execution_result: ExecutionResult,
        homologation: Dict[str, Any],
        final_report: Dict[str, Any],
        api_key: str,
        model: str,
        provider: str,
        temperature: float = 0.0,
        prompt_override: str | None = None,
        framework: str | None = None,
        language: str | None = None,
    ) -> Dict[str, Any]:
        del provider
        prompt = prompt_override or self.prompt_manager.load_prompt("refactoring")
        execution_artifacts = _compact_execution_artifacts(execution_result)
        confirmed_script = (
            final_report.get("script", {}).get("confirmed", original_script)
            if isinstance(final_report, dict)
            else original_script
        )
        final_prompt = _append_context_block(
            _render_prompt(
                prompt,
                original_script=original_script,
                script=original_script,
                confirmed_script=confirmed_script,
                execution_results=execution_result,
                execution=execution_result,
                execution_screenshots=execution_artifacts.get("screenshots"),
                primary_screenshot=execution_artifacts.get("primary_screenshot"),
                image_evidence=execution_artifacts.get("image_evidence"),
                homologation_results=homologation,
                homologation=homologation,
                finalization_context=final_report,
                final_report=final_report,
                framework=framework or execution_result.framework,
                language=language or (execution_result.test_results or {}).get("language"),
                refactoring_context={
                    "original_script": original_script,
                    "confirmed_script": confirmed_script,
                    "execution_result": execution_result,
                    "visual_context": execution_artifacts,
                    "homologation": homologation,
                    "final_report": final_report,
                    "framework": framework or execution_result.framework,
                    "language": language or (execution_result.test_results or {}).get("language"),
                },
            ),
            {
                "original_script": original_script,
                "confirmed_script": confirmed_script,
                "execution_result": execution_result,
                "execution_screenshots": execution_artifacts.get("screenshots"),
                "primary_screenshot": execution_artifacts.get("primary_screenshot"),
                "image_evidence": execution_artifacts.get("image_evidence"),
                "homologation": homologation,
                "final_report": final_report,
                "framework": framework or execution_result.framework,
                "language": language or (execution_result.test_results or {}).get("language"),
            },
        )
        raw_result = _json_safe(self.llm.generate_json(api_key, model, final_prompt, temperature=temperature))
        result = _normalize_refactoring_fields(raw_result)
        try:
            RefactoringResponse.model_validate(result)
        except ValidationError as exc:
            repair_prompt = _append_context_block(
                final_prompt,
                {
                    "required_response_contract_repair": {
                        "missing_or_invalid_fields": [error["loc"][0] for error in exc.errors()],
                        "partial_response": raw_result,
                        "required_fields": list(RefactoringResponse.model_fields),
                        "instruction": (
                            "Return a complete JSON object with every required field as a string. "
                            "Preserve the refactored script from the partial response when present. "
                            "Do not omit fields; use a concise, evidence-based explanation when a field has no findings."
                        ),
                    },
                },
            )
            try:
                repaired_contract = _normalize_refactoring_fields(
                    _json_safe(self.llm.generate_json(api_key, model, repair_prompt, temperature=0.0))
                )
                result.update(repaired_contract)
            except Exception:
                # The final response is completed deterministically below if the model
                # still fails to satisfy its required output schema.
                pass

        candidate = result.get("refactored_script") or ""
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = confirmed_script

        def complete_response(script: str, validation: dict[str, Any], validation_error: str = "") -> dict[str, Any]:
            response = dict(result)
            baseline = response.get("original_script") or confirmed_script or original_script
            response["original_script"] = baseline
            response["refactored_script"] = script or candidate or baseline
            if validation.get("status") in {"fallback", "failed"}:
                response["diff"] = _describe_script_diff(baseline, response["refactored_script"])
                response["justification"] = validation.get("message") or "A versão confirmada foi mantida após a pré-validação."
            response.setdefault("diff", _describe_script_diff(baseline, response["refactored_script"]))
            response.setdefault(
                "justification",
                "A versão refatorada foi comparada com a versão confirmada e validada antes de ser disponibilizada."
                if response["refactored_script"] != baseline
                else "A versão confirmada foi mantida porque não houve uma alteração de script validada.",
            )
            response.setdefault(
                "possible_errors",
                f"A pré-validação não aprovou a proposta: {validation_error}"
                if validation_error
                else "A pré-validação de sintaxe foi concluída. A execução da versão refatorada ainda deve ser avaliada para confirmar o comportamento funcional.",
            )
            response.setdefault(
                "implemented_improvements",
                "As alterações propostas passaram pela pré-validação do framework."
                if response["refactored_script"] != baseline
                else "Nenhuma alteração proposta foi aceita; a versão confirmada foi preservada.",
            )
            response.setdefault(
                "suggested_follow_up",
                "Execute a versão refatorada e revise os resultados, logs e evidências antes de promovê-la.",
            )
            validated_response = RefactoringResponse.model_validate(response).model_dump()
            validated_response["script_validation"] = validation
            return validated_response

        resolved_framework = framework or execution_result.framework or ""
        resolved_language = language or (execution_result.test_results or {}).get("language") or ""
        strategy = ExecutionStrategyRegistry().resolve(resolved_framework, resolved_language, candidate)
        if not strategy.supports_candidate_validation:
            validation = {
                "status": "not_supported",
                "message": f"A validação prévia de scripts ainda não está implementada para {resolved_framework or 'este framework'}.",
            }
            return complete_response(candidate, validation)

        candidate_error = ""
        transformations: list[dict] = []
        for attempt in range(self.MAX_SCRIPT_REPAIR_ATTEMPTS + 1):
            try:
                candidate, transformations = strategy.validate_script_candidate(
                    resolved_framework,
                    candidate,
                    resolved_language,
                )
                validation = {
                    "status": "passed",
                    "message": "",
                    "transformations": transformations,
                }
                if transformations:
                    correction_summary = "; ".join(item.get("description", "Ajuste automático") for item in transformations)
                    result["justification"] = (str(result.get("justification") or "").strip() + f" Ajuste preventivo aplicado pela validação: {correction_summary}.").strip()
                return complete_response(candidate, validation)
            except Exception as exc:
                candidate_error = str(exc)
                if attempt >= self.MAX_SCRIPT_REPAIR_ATTEMPTS:
                    break

                repair_context = {
                    "framework": resolved_framework,
                    "language": resolved_language,
                    "refactored_script_with_errors": candidate,
                    "preflight_diagnostic": candidate_error,
                    "confirmed_script_fallback": confirmed_script,
                    "repair_attempt": attempt + 1,
                    "required_action": (
                        "Fix the refactored script so it passes the framework's syntax/preflight check. "
                        "Preserve the original business flow and assertions. Return the same JSON contract and put only executable source code in refactored_script. "
                        "Do not add Markdown fences or prose to the source field."
                    ),
                }
                repair_prompt = _append_context_block(final_prompt, {"refactoring_preflight_repair": repair_context})
                try:
                    repaired = _json_safe(self.llm.generate_json(api_key, model, repair_prompt, temperature=0.0))
                except Exception as repair_exc:
                    candidate_error += f"; correção automática não concluída: {repair_exc}"
                    break
                if isinstance(repaired, dict):
                    normalized_repair = _normalize_refactoring_fields(repaired)
                    candidate = normalized_repair.get("refactored_script") or candidate
                    result.update({key: value for key, value in normalized_repair.items() if value})

        # The script that was executed to produce the pipeline result is the
        # safest fallback, provided it still passes the framework preflight.
        try:
            fallback, fallback_transformations = strategy.validate_script_candidate(
                resolved_framework,
                confirmed_script,
                resolved_language,
            )
        except Exception as fallback_exc:
            validation = {
                "status": "failed",
                "message": f"A versão refatorada e a versão confirmada falharam na pré-validação. Refatoração: {candidate_error} Versão confirmada: {fallback_exc}",
                "transformations": [],
            }
            return complete_response(candidate, validation, candidate_error)

        validation = {
            "status": "fallback",
            "message": f"A versão refatorada falhou na pré-validação e não foi aceita. Mantive a versão confirmada, que passou pela validação. Diagnóstico da refatoração: {candidate_error}",
            "transformations": fallback_transformations,
        }
        result["possible_errors"] = (
            str(result.get("possible_errors") or "").strip()
            + f" A proposta de refatoração foi rejeitada pela pré-validação: {candidate_error}."
        ).strip()
        result["implemented_improvements"] = (
            "A versão refatorada proposta não passou na pré-validação. Para evitar entregar um script quebrado, foi mantida a versão confirmada que passou pela validação."
        )
        return complete_response(fallback, validation, candidate_error)


__all__ = ["RefactoringStage"]
