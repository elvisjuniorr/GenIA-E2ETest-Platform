"""Refinement stage."""

from __future__ import annotations

from typing import Any, List

from backend.domain.models import ExtractedElement, Module

from .base import PipelineStageRunner
from ..shared import _compact_html_snapshot, _compact_module_snapshot, _model_dump_list, _normalize_extracted_elements, _render_prompt


class RefinementStage(PipelineStageRunner):
    async def run(
        self,
        current_api_key: str,
        current_model: str,
        current_provider: str,
        current_temperature: float,
        module: Module,
        headless: bool = True,
        temperature: float | None = None,
        prompt_override: str | None = None,
        llm_override: dict[str, Any] | None = None,
    ) -> List[ExtractedElement]:
        provider, model, api_key, resolved_temperature = self.resolve_stage_llm(
            current_provider,
            current_model,
            current_api_key,
            current_temperature,
            "refinement",
            {"refinement": llm_override} if llm_override else None,
        )
        return await self.execute(
            None,
            module,
            api_key,
            model,
            provider,
            temperature=temperature if temperature is not None else resolved_temperature,
            headless=headless,
            prompt_override=prompt_override,
        )

    async def execute(
        self,
        crawler: Any,
        module: Module,
        api_key: str,
        model: str,
        provider: str,
        temperature: float = 0.0,
        headless: bool = True,
        prompt_override: str | None = None,
    ) -> List[ExtractedElement]:
        del crawler, headless, provider
        prompt = prompt_override or self.prompt_manager.load_prompt("refinement")
        structured_module = _compact_module_snapshot(module)
        extracted_elements = _model_dump_list(module.extracted_data or [])
        source_html = _compact_html_snapshot(module.source_html or "")
        final_prompt = _render_prompt(
            prompt,
            structured_json=structured_module,
            refined_module=structured_module,
            extracted_elements=extracted_elements,
            source_html=source_html,
            refinement_context={
                "module": structured_module,
                "extracted_elements": extracted_elements,
                "source_html": source_html,
            },
        )
        raw = self.llm.generate_json(api_key, model, final_prompt, temperature=temperature)
        refined_elements = _normalize_extracted_elements(raw)
        original_elements = list(module.extracted_data or [])
        if original_elements and not refined_elements:
            self.log(
                "Refinement returned no elements; preserving the extracted elements for this module.",
                level="warning",
                stage="refinement",
            )
            return original_elements
        return refined_elements


__all__ = ["RefinementStage"]
