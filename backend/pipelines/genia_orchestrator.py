"""Application orchestration for the GenIA E2E pipeline."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import queue
import threading
import traceback
import uuid
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

try:
    from crawl4ai import AsyncWebCrawler
except ImportError:  # pragma: no cover - optional runtime dependency
    AsyncWebCrawler = None

from backend.application.prompts import PromptManager
from backend.domain.models import (
    ExecutionResult,
    ExtractedElement,
    Module,
    PipelineStage,
    TestCase,
)
from backend.infrastructure.llm_factory import LLMFactory
from backend.pipelines.stages.confirmation import ConfirmationStage as ConfirmationStageImpl
from backend.pipelines.stages.execution import ExecutionStage as ExecutionStageImpl
from backend.pipelines.stages.extraction import ExtractionStage as ExtractionStageImpl
from backend.pipelines.stages.finalization import FinalizationStage as FinalizationStageImpl
from backend.pipelines.stages.generation import GenerationStage as GenerationStageImpl
from backend.pipelines.stages.homologation import HomologationStage as HomologationStageImpl
from backend.pipelines.stages.refactoring import RefactoringStage as RefactoringStageImpl
from backend.pipelines.stages.refinement import RefinementStage as RefinementStageImpl
from backend.pipelines.stages.structuring import StructuringStage as StructuringStageImpl
from backend.pipelines.stages.validation import ValidationStage as ValidationStageImpl
from backend.pipelines.shared import (
    _append_context_block,
    _build_execution_report_html,
    _build_execution_report_markdown,
    _build_timeline_payload,
    _compact_execution_artifacts,
    _compact_execution_result,
    _compact_history_entries,
    _compact_log_entries,
    _compact_module_snapshot,
    _compact_sequence,
    _compact_timeline_entries,
    _create_browser_config,
    _extract_html_snapshot,
    _is_crawl4ai_browser_lifecycle_error,
    _iso_now,
    _json_safe,
    _map_extracted_data_to_steps,
    _model_dump,
    _model_dump_list,
    _normalize_extracted_elements,
    _normalize_manual_changes,
    _render_prompt,
    _require_crawl4ai,
    _safe_json_dumps,
    _sanitize_module_tree,
    _truncate_text,
)


logger = logging.getLogger(__name__)


class GenIAOrchestrator:
    def __init__(self, prompts_dir: str = "prompts", heavy_stage_concurrency: int = 2):
        self.prompt_manager = PromptManager(prompts_dir)
        self._global_execution_logs: list[dict[str, Any]] = []
        self._execution_logs_context = contextvars.ContextVar(f"genia_execution_logs_{id(self)}", default=None)
        self.pipeline_sessions: dict[str, dict[str, Any]] = {}
        self._llm_context = threading.local()
        self._session_lock = threading.Lock()
        # Pipeline stages use per-session state and per-thread LLM context.
        # A no-op context preserves the existing method structure without
        # serializing otherwise independent pipelines.
        self._pipeline_lock = nullcontext()
        self._heavy_stage_capacity = max(1, min(2, int(heavy_stage_concurrency)))
        self._heavy_stage_semaphore = threading.BoundedSemaphore(self._heavy_stage_capacity)
        self._heavy_stage_wait_lock = threading.Lock()
        self._heavy_stage_waiters = 0

    @property
    def execution_logs(self) -> list[dict[str, Any]]:
        logs = self._execution_logs_context.get()
        return logs if logs is not None else self._global_execution_logs

    @execution_logs.setter
    def execution_logs(self, logs: list[dict[str, Any]]) -> None:
        if self._execution_logs_context.get() is None:
            self._global_execution_logs = logs
        else:
            self._execution_logs_context.set(logs)

    @property
    def llm_provider(self):
        return getattr(self._llm_context, "provider", None)

    @llm_provider.setter
    def llm_provider(self, value):
        self._llm_context.provider = value

    @property
    def current_provider(self) -> str:
        return getattr(self._llm_context, "current_provider", "")

    @current_provider.setter
    def current_provider(self, value: str) -> None:
        self._llm_context.current_provider = value

    @property
    def current_model(self) -> str:
        return getattr(self._llm_context, "current_model", "")

    @current_model.setter
    def current_model(self, value: str) -> None:
        self._llm_context.current_model = value

    @property
    def current_api_key(self) -> str:
        return getattr(self._llm_context, "current_api_key", "")

    @current_api_key.setter
    def current_api_key(self, value: str) -> None:
        self._llm_context.current_api_key = value

    @property
    def current_temperature(self) -> float:
        return getattr(self._llm_context, "current_temperature", 0.0)

    @current_temperature.setter
    def current_temperature(self, value: float) -> None:
        self._llm_context.current_temperature = value

    def set_provider(self, provider: str, api_key: str, model: str, temperature: float | None = None) -> None:
        self.llm_provider = LLMFactory.get_provider(provider)
        self.current_provider = provider
        self.current_model = model
        self.current_api_key = api_key
        self.current_temperature = float(temperature if temperature is not None else self.current_temperature or 0.0)

    def _create_session(self, pipeline_id: str, **data: Any) -> dict[str, Any]:
        with self._session_lock:
            existing = self.pipeline_sessions.get(pipeline_id)
            if existing:
                existing["data"].update(data)
                existing["state"] = "INITIAL"
                existing["paused_at"] = None
                existing["updated_at"] = _iso_now()
                session = existing
            else:
                session = {
                    "id": pipeline_id,
                    "state": "INITIAL",
                    "data": data,
                    "history": [],
                    "timeline": [],
                    "paused_at": None,
                    "created_at": _iso_now(),
                    "updated_at": _iso_now(),
                    "queue": queue.Queue(),
                }
                self.pipeline_sessions[pipeline_id] = session
        return session

    def _get_session(self, pipeline_id: str) -> dict[str, Any] | None:
        with self._session_lock:
            return self.pipeline_sessions.get(pipeline_id)

    def _set_session_state(self, pipeline_id: str, state: str) -> None:
        session = self._get_session(pipeline_id)
        if not session:
            return
        session["state"] = state
        session["updated_at"] = _iso_now()

    def _acquire_heavy_stage_slot(self, pipeline_id: str) -> None:
        if not self._heavy_stage_semaphore.acquire(blocking=False):
            with self._heavy_stage_wait_lock:
                self._heavy_stage_waiters += 1
                position = self._heavy_stage_waiters
            session = self._get_session(pipeline_id)
            if session:
                session["wait_reason"] = "heavy_stage"
                session["queue_position"] = position
            self._set_session_state(pipeline_id, "WAITING_HEAVY_STAGE")
            self.push_event(
                "STAGE_QUEUED",
                "extraction_refinement",
                {
                    "message": "Aguardando vaga para extração/refinamento.",
                    "queue_position": position,
                    "capacity": self._heavy_stage_capacity,
                },
                pipeline_id,
            )
            self._heavy_stage_semaphore.acquire()
            with self._heavy_stage_wait_lock:
                self._heavy_stage_waiters -= 1
        session = self._get_session(pipeline_id)
        if session:
            session["wait_reason"] = None
            session["queue_position"] = 0
        self._set_session_state(pipeline_id, PipelineStage.EXTRACTION.value)
        self.push_event(
            "STAGE_SLOT_ACQUIRED",
            "extraction_refinement",
            {"message": "Vaga de extração/refinamento liberada.", "capacity": self._heavy_stage_capacity},
            pipeline_id,
        )

    def log(self, message: str, level: str = "info", stage: str = "") -> None:
        timestamp = _iso_now()
        self.execution_logs.append(
            {
                "timestamp": timestamp,
                "level": level,
                "message": message,
                "stage": stage,
                "type": level,
            }
        )
        stage_prefix = f"[{stage}] " if stage else ""
        logger.log(getattr(logging, level.upper(), logging.INFO), f"{stage_prefix}{message}")
        print(f"[GenIA Backend] {stage_prefix}{message}", flush=True)

    def push_event(self, event_type: str, channel: str, payload: Any, pipeline_id: str | None = None):
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("label") or payload.get("stage") or payload.get("channel") or event_type
        else:
            message = str(payload)

        event = {
            "id": str(uuid.uuid4()),
            "timestamp": _iso_now(),
            "type": event_type,
            "channel": channel,
            "payload": _json_safe(payload),
            "message": message,
        }
        is_visual_frame = event_type == "EXECUTION_VISUAL_FRAME"
        if not is_visual_frame:
            self.execution_logs.append(
                {
                    "timestamp": event["timestamp"],
                    "level": self._get_level_from_event_type(event_type),
                    "message": message,
                    "stage": channel,
                    "type": self._get_level_from_event_type(event_type),
                    "event_type": event_type,
                }
            )
            print(f"[EVENT:{event_type}][{channel}] {message}", flush=True)

        if pipeline_id:
            session = self._get_session(pipeline_id)
            if session:
                if not is_visual_frame:
                    session["history"].append(_build_timeline_payload(session, channel, event_type, payload))
                    session["timeline"].append(
                        {
                            "timestamp": event["timestamp"],
                            "stage": channel,
                            "event_type": event_type,
                            "payload": _json_safe(payload),
                        }
                    )
                    session["updated_at"] = _iso_now()
                session["queue"].put(event)

        return event

    def _get_level_from_event_type(self, event_type: str) -> str:
        if "ERROR" in event_type or "FAILED" in event_type:
            return "error"
        if "SUCCESS" in event_type or "DONE" in event_type or "PASSED" in event_type:
            return "success"
        if "START" in event_type or "PAUSED" in event_type:
            return "info"
        return "info"

    async def run_full_pipeline_1(
        self,
        test_case: str,
        framework: str,
        language: str,
        test_name: str | None = None,
        headless: bool = True,
        attempt: int = 1,
        pipeline_id: str | None = None,
        prompt_overrides: Dict[str, str] | None = None,
        llm_overrides: Dict[str, Any] | None = None,
        input_mode: str = "test_case",
        user_story_content: str | None = None,
        user_story_filename: str | None = None,
        manual_urls: list[str] | None = None,
    ) -> Dict[str, Any]:
        self._execution_logs_context.set([])
        with self._pipeline_lock:
            self.execution_logs = []
            start_time = datetime.now()
            pipeline_id = pipeline_id or str(uuid.uuid4())
            prompt_overrides = prompt_overrides or {}
            llm_overrides = llm_overrides or {}
            manual_urls = manual_urls or []

            heavy_stage_slot_acquired = False
            try:
                _require_crawl4ai()
                session = self._create_session(
                    pipeline_id,
                    initial_input=test_case,
                    input_mode=input_mode,
                    user_story_content=user_story_content,
                    user_story_filename=user_story_filename,
                    manual_urls=manual_urls,
                    framework=framework,
                    language=language,
                    test_name=test_name,
                    headless=headless,
                    attempt=attempt,
                    prompt_overrides=prompt_overrides,
                    llm_overrides=llm_overrides,
                )
                session["execution_logs"] = self.execution_logs

                self.push_event(
                    "PIPELINE_START",
                    "logs",
                    {
                        "pipeline_id": pipeline_id,
                        "test_name": test_name,
                        "framework": framework,
                        "language": language,
                        "attempt": attempt,
                        "timestamp": start_time.isoformat(),
                    },
                    pipeline_id,
                )

                self._set_session_state(pipeline_id, PipelineStage.STRUCTURING.value)
                self.push_event("STAGE_START", "structuring", {"stage": PipelineStage.STRUCTURING.value}, pipeline_id)
                structured = await StructuringStageImpl(self.llm_provider, self.prompt_manager).run(
                    self.current_api_key,
                    self.current_model,
                    self.current_provider,
                    self.current_temperature,
                    test_case,
                    input_mode=input_mode,
                    user_story_content=user_story_content,
                    user_story_filename=user_story_filename,
                    manual_urls=manual_urls,
                    prompt_override=prompt_overrides.get("structuring"),
                    llm_override=llm_overrides.get("structuring"),
                )
                # The graph root should use the explicit UI field, not a title
                # inferred from (or accidentally copied from) the test body.
                if isinstance(test_name, str) and test_name.strip():
                    structured.testCase = test_name.strip()
                self.push_event(
                    "STAGE_DONE",
                    "structuring",
                    {
                        "test_case": structured.testCase,
                        "modules": len(structured.modules),
                        "structured": structured.model_dump(),
                    },
                    pipeline_id,
                )

                browser_config = _create_browser_config(headless)
                extracted_test = structured.model_copy(deep=True)
                refined_test = structured.model_copy(deep=True)
                crawler_retry_errors: list[str] = []

                self._acquire_heavy_stage_slot(pipeline_id)
                heavy_stage_slot_acquired = True
                for crawler_attempt in range(2):
                    crawler = AsyncWebCrawler(config=browser_config)
                    try:
                        await crawler.start()
                        for idx, module in enumerate(structured.modules, 1):
                            module_start = datetime.now()
                            self._set_session_state(pipeline_id, PipelineStage.EXTRACTION.value)
                            self.push_event(
                                "MODULE_START",
                                "extraction",
                                {"index": idx, "total": len(structured.modules), "url": module.url},
                                pipeline_id,
                            )

                            extracted_elements = await ExtractionStageImpl(self.llm_provider, self.prompt_manager).run(
                                self.current_api_key,
                                self.current_model,
                                self.current_provider,
                                self.current_temperature,
                                module,
                                headless=headless,
                                temperature=None,
                                prompt_override=prompt_overrides.get("extraction"),
                                llm_override=llm_overrides.get("extraction"),
                                crawler=crawler,
                            )
                            extracted_test.modules[idx - 1].source_html = getattr(module, "source_html", None)
                            extracted_test.modules[idx - 1].extracted_data = extracted_elements
                            extracted_test.modules[idx - 1] = _map_extracted_data_to_steps(extracted_test.modules[idx - 1])
                            self.push_event(
                                "EXTRACTION_DONE",
                                "extraction",
                                {
                                    "index": idx,
                                    "total": len(structured.modules),
                                    "module": module.url,
                                    "elements_count": len(extracted_elements),
                                    "elements": _model_dump_list(extracted_elements),
                                    "extracted_snapshot": _sanitize_module_tree(extracted_test),
                                    "duration_ms": (datetime.now() - module_start).total_seconds() * 1000,
                                },
                                pipeline_id,
                            )

                            self._set_session_state(pipeline_id, PipelineStage.REFINEMENT.value)
                            refined_test.modules[idx - 1].source_html = getattr(extracted_test.modules[idx - 1], "source_html", None)
                            refined_test.modules[idx - 1].extracted_data = extracted_elements
                            refined_elements = await RefinementStageImpl(self.llm_provider, self.prompt_manager).run(
                                self.current_api_key,
                                self.current_model,
                                self.current_provider,
                                self.current_temperature,
                                extracted_test.modules[idx - 1],
                                headless=headless,
                                temperature=None,
                                prompt_override=prompt_overrides.get("refinement"),
                                llm_override=llm_overrides.get("refinement"),
                            )
                            refined_test.modules[idx - 1].extracted_data = refined_elements
                            refined_test.modules[idx - 1] = _map_extracted_data_to_steps(refined_test.modules[idx - 1])
                            self.push_event(
                                "REFINEMENT_DONE",
                                "refinement",
                                {
                                    "index": idx,
                                    "total": len(structured.modules),
                                    "module": module.url,
                                    "elements_count": len(refined_elements),
                                    "elements": _model_dump_list(refined_elements),
                                    "refined_snapshot": _sanitize_module_tree(refined_test),
                                },
                                pipeline_id,
                            )
                        break
                    except Exception as exc:
                        crawler_retry_errors.append(str(exc))
                        exc_text = str(exc).lower()
                        if "browsertype.launch" in exc_text:
                            self.log(
                                "Crawl4AI browser launch failed. Verify the Render runtime, browser binaries, and Docker image.",
                                level="error",
                                stage="extraction",
                            )
                        if crawler_attempt == 0 and _is_crawl4ai_browser_lifecycle_error(exc):
                            self.log(
                                "Crawler lifecycle error detected. Recreating browser and retrying extraction once.",
                                level="warning",
                                stage="extraction",
                            )
                            continue
                        raise
                    finally:
                        try:
                            await crawler.close()
                        except Exception:
                            pass

                self._heavy_stage_semaphore.release()
                heavy_stage_slot_acquired = False

                if crawler_retry_errors:
                    self.push_event("DEBUG", "logs", {"crawler_retry_errors": crawler_retry_errors[-2:]}, pipeline_id)

                self._set_session_state(pipeline_id, PipelineStage.GENERATION.value)
                self.push_event("STAGE_START", "generation", {"stage": PipelineStage.GENERATION.value}, pipeline_id)
                script = await GenerationStageImpl(self.llm_provider, self.prompt_manager).run(
                    self.current_api_key,
                    self.current_model,
                    self.current_provider,
                    self.current_temperature,
                    refined_test,
                    framework,
                    language,
                    temperature=None,
                    prompt_override=prompt_overrides.get("generation"),
                    llm_override=llm_overrides.get("generation"),
                )
                self.push_event(
                    "STAGE_DONE",
                    "generation",
                    {"script_length": len(script), "script_preview": script[:500], "script": script},
                    pipeline_id,
                )

                self._set_session_state(pipeline_id, PipelineStage.VALIDATION.value)
                self.push_event("STAGE_START", "validation", {"stage": PipelineStage.VALIDATION.value}, pipeline_id)
                validation = await ValidationStageImpl(self.llm_provider, self.prompt_manager).run(
                    self.current_api_key,
                    self.current_model,
                    self.current_provider,
                    self.current_temperature,
                    script,
                    refined_test,
                    temperature=None,
                    prompt_override=prompt_overrides.get("validation"),
                    llm_override=llm_overrides.get("validation"),
                )
                self.push_event("STAGE_DONE", "validation", {"validation": validation, "generated_script": script}, pipeline_id)

                session["data"].update(
                    {
                        "structured": structured.model_dump(),
                        "extracted": _sanitize_module_tree(extracted_test),
                        "refined": _sanitize_module_tree(refined_test),
                        "script": script,
                        "validation": validation,
                        "test_case": test_case,
                        "input_mode": input_mode,
                        "user_story_content": user_story_content,
                        "user_story_filename": user_story_filename,
                        "manual_urls": manual_urls,
                        "framework": framework,
                        "language": language,
                        "test_name": test_name,
                        "headless": headless,
                        "attempt": attempt,
                        "pipeline_id": pipeline_id,
                        "prompt_overrides": prompt_overrides,
                        "llm_overrides": llm_overrides,
                    }
                )
                session["state"] = "WAITING_VALIDATION"
                session["paused_at"] = PipelineStage.VALIDATION.value
                session["updated_at"] = _iso_now()
                self.push_event(
                    "PIPELINE_PAUSED",
                    "logs",
                    {"pipeline_id": pipeline_id, "paused_at": PipelineStage.VALIDATION.value},
                    pipeline_id,
                )
                session["queue"].put(None)

                return {
                    "status": "waiting_validation",
                    "pipeline_id": pipeline_id,
                    "structured": structured.model_dump(),
                    "extracted": _sanitize_module_tree(extracted_test),
                    "refined": _sanitize_module_tree(refined_test),
                    "script": script,
                    "validation": validation,
                    "variables": validation.get("detected_inputs", []),
                    "editable_fields": validation.get("editable_fields", []),
                    "logs": self.execution_logs,
                    "llm_overrides": llm_overrides,
                }
            except Exception as exc:
                if heavy_stage_slot_acquired:
                    self._heavy_stage_semaphore.release()
                self.push_event("PIPELINE_ERROR", "logs", {"error": str(exc), "traceback": traceback.format_exc()}, pipeline_id)
                session = self._get_session(pipeline_id)
                if session:
                    session["state"] = "ERROR"
                    session["data"]["error"] = str(exc)
                    session["paused_at"] = "ERROR"
                    session["queue"].put(None)
                return {"status": "error", "error": str(exc), "pipeline_id": pipeline_id, "logs": self.execution_logs}

    async def run_full_pipeline_2(
        self,
        pipeline_id: str,
        manual_changes: Dict[str, Any] | None = None,
        continue_without_changes: bool = True,
        resume_from_stage: str | None = None,
        script_override: str | None = None,
        prompt_overrides: Dict[str, str] | None = None,
        llm_overrides: Dict[str, Any] | None = None,
        visualize_execution: bool = False,
    ) -> Dict[str, Any]:
        with self._pipeline_lock:
            session = self._get_session(pipeline_id)
            if not session:
                self.push_event("PIPELINE_ERROR", "logs", {"error": "Pipeline session not found", "pipeline_id": pipeline_id})
                raise ValueError("Pipeline session not found")

            self._execution_logs_context.set(session.get("execution_logs", []))

            try:
                self.push_event("PIPELINE_RESUME", "logs", {"pipeline_id": pipeline_id}, pipeline_id)
                manual_changes = _normalize_manual_changes(manual_changes)
                prompt_overrides = prompt_overrides or session["data"].get("prompt_overrides", {}) or {}
                llm_overrides = llm_overrides or session["data"].get("llm_overrides", {}) or {}
                refined = session["data"].get("refined")
                original_script = script_override or session["data"].get("script", "")
                framework = session["data"].get("framework")
                language = session["data"].get("language")
                validation = session["data"].get("validation", {})
                visualize_execution = bool(visualize_execution)

                confirmed_script = original_script
                confirmation_used_llm = False

                if resume_from_stage == "confirmation" and manual_changes and not continue_without_changes:
                    self._set_session_state(pipeline_id, PipelineStage.CONFIRMATION.value)
                    self.push_event("STAGE_START", "confirmation", manual_changes, pipeline_id)
                    confirmed_script = await ConfirmationStageImpl(self.llm_provider, self.prompt_manager).run(
                        self.current_provider,
                        self.current_model,
                        self.current_api_key,
                        self.current_temperature,
                        original_script,
                        manual_changes,
                        temperature=None,
                        prompt_override=prompt_overrides.get("confirmation"),
                        llm_override=llm_overrides.get("confirmation"),
                    )
                    confirmation_used_llm = True
                    self.push_event(
                        "STAGE_DONE",
                        "confirmation",
                        {
                            "script_updated": True,
                            "confirmed_script": confirmed_script,
                            "script_preview": confirmed_script[:500],
                        },
                        pipeline_id,
                    )
                else:
                    self.push_event(
                        "STAGE_DONE",
                        "confirmation",
                        {
                            "script_updated": False,
                            "script_reused": True,
                            "confirmed_script": confirmed_script,
                            "script_preview": confirmed_script[:500],
                        },
                        pipeline_id,
                    )

                session["data"]["manual_changes"] = manual_changes
                session["data"]["confirmed_script"] = confirmed_script
                session["data"]["confirmation_used_llm"] = confirmation_used_llm

                self._set_session_state(pipeline_id, PipelineStage.EXECUTION.value)
                self.push_event("STAGE_START", "execution", {"framework": framework, "language": language}, pipeline_id)

                def emit_execution_feedback(level: str, message: str) -> None:
                    if level == "visual":
                        if not visualize_execution:
                            return
                        try:
                            frame = json.loads(message)
                        except (TypeError, ValueError):
                            return
                        self.push_event("EXECUTION_VISUAL_FRAME", "execution", frame, pipeline_id)
                        return
                    self.push_event(
                        "EXECUTION_LOG",
                        "execution",
                        {"level": level, "message": message},
                        pipeline_id,
                    )

                execution_result = await ExecutionStageImpl(self.llm_provider, self.prompt_manager).run(
                    confirmed_script,
                    framework,
                    language,
                    log_callback=emit_execution_feedback,
                    visualize_execution=visualize_execution,
                )
                execution_artifacts = _compact_execution_artifacts(execution_result)
                execution_payload = _model_dump(execution_result)
                if not isinstance(execution_payload, dict):
                    execution_payload = {"value": _truncate_text(str(execution_payload), 1500)}
                self.push_event(
                    "STAGE_DONE",
                    "execution",
                    {
                        **execution_payload,
                        "execution_artifacts": execution_artifacts,
                        "primary_screenshot": execution_artifacts.get("primary_screenshot"),
                    },
                    pipeline_id,
                )

                self._set_session_state(pipeline_id, PipelineStage.HOMOLOGATION.value)
                self.push_event("STAGE_START", "homologation", {}, pipeline_id)
                homologation = await HomologationStageImpl(self.llm_provider, self.prompt_manager).run(
                    self.current_provider,
                    self.current_model,
                    self.current_api_key,
                    self.current_temperature,
                    execution_result,
                    refined,
                    validation,
                    manual_changes,
                    confirmed_script,
                    temperature=None,
                    prompt_override=prompt_overrides.get("homologation"),
                    llm_override=llm_overrides.get("homologation"),
                )
                self.push_event("STAGE_DONE", "homologation", homologation, pipeline_id)

                finalization_input = {
                    "pipeline_id": pipeline_id,
                    "inputs": {
                        "initial_input": session["data"].get("initial_input"),
                        "test_case": session["data"].get("test_case"),
                        "framework": framework,
                        "language": language,
                        "test_name": session["data"].get("test_name"),
                        "manual_changes": manual_changes,
                        "validation": {
                            "detected_inputs": _compact_sequence(validation.get("detected_inputs", []), 15),
                            "editable_fields": _compact_sequence(validation.get("editable_fields", []), 15),
                        },
                    },
                    "history": _compact_history_entries(session["history"], 25),
                    "timeline": _compact_timeline_entries(session["timeline"], 30),
                    "structured": _sanitize_module_tree(session["data"].get("structured") or {}),
                    "extracted": _sanitize_module_tree(session["data"].get("extracted") or {}),
                    "refined": _sanitize_module_tree(refined or {}),
                    "script": {
                        "original": _truncate_text(original_script, 5000),
                        "confirmed": _truncate_text(confirmed_script, 5000),
                    },
                    "execution": _compact_execution_result(execution_result),
                    "execution_artifacts": execution_artifacts,
                    "homologation": {
                        key: _truncate_text(value, 2500)
                        for key, value in (homologation or {}).items()
                    }
                    if isinstance(homologation, dict)
                    else _truncate_text(str(homologation), 2500),
                    "logs": _compact_log_entries(self.execution_logs, 80, 300),
                    "prompts_used": [
                        "structuring",
                        "extraction",
                        "refinement",
                        "generation",
                        "validation",
                        "confirmation",
                        "execution",
                        "homologation",
                        "finalization",
                        "refactoring",
                    ],
                }

                self._set_session_state(pipeline_id, PipelineStage.FINALIZATION.value)
                self.push_event("STAGE_START", "finalization", {}, pipeline_id)
                final_report = await FinalizationStageImpl(self.llm_provider, self.prompt_manager).run(
                    self.current_provider,
                    self.current_model,
                    self.current_api_key,
                    self.current_temperature,
                    finalization_input,
                    temperature=None,
                    prompt_override=prompt_overrides.get("finalization"),
                    llm_override=llm_overrides.get("finalization"),
                )
                self.push_event("STAGE_DONE", "finalization", final_report, pipeline_id)

                self._set_session_state(pipeline_id, PipelineStage.REFACTORING.value)
                self.push_event("STAGE_START", "refactoring", {}, pipeline_id)
                refactoring = await RefactoringStageImpl(self.llm_provider, self.prompt_manager).run(
                    self.current_provider,
                    self.current_model,
                    self.current_api_key,
                    self.current_temperature,
                    confirmed_script,
                    execution_result,
                    homologation,
                    final_report,
                    temperature=None,
                    prompt_override=prompt_overrides.get("refactoring"),
                    llm_override=llm_overrides.get("refactoring"),
                    framework=framework,
                    language=language,
                )

                if not isinstance(refactoring, dict):
                    refactoring = {"refactored_script": str(refactoring)}

                refactored_script = refactoring.get("refactored_script") or refactoring.get("script") or ""
                if not refactored_script:
                    refactored_script = confirmed_script

                refactoring.setdefault("original_script", original_script)
                refactoring.setdefault("refactored_script", refactored_script)
                refactoring.setdefault("diff", "")
                refactoring.setdefault("justification", "")

                self.push_event("STAGE_DONE", "refactoring", refactoring, pipeline_id)

                exportables = {
                    "json": final_report,
                    "html": _build_execution_report_html(final_report),
                    "markdown": _build_execution_report_markdown(final_report),
                }

                session["data"].update(
                    {
                        "manual_changes": manual_changes,
                        "confirmed_script": confirmed_script,
                        "execution_result": _model_dump(execution_result),
                        "homologation": homologation,
                        "final_report": final_report,
                        "refactoring": refactoring,
                        "refactored_script": refactored_script,
                        "exportables": exportables,
                        "state": "COMPLETED",
                        "prompt_overrides": prompt_overrides,
                        "llm_overrides": llm_overrides,
                    }
                )
                session["state"] = "COMPLETED"
                session["updated_at"] = _iso_now()
                self.push_event("PIPELINE_COMPLETED", "logs", {"pipeline_id": pipeline_id}, pipeline_id)
                session["queue"].put(None)

                return {
                    "status": "completed",
                    "pipeline_id": pipeline_id,
                    "execution": _model_dump(execution_result),
                    "homologation": homologation,
                    "final_report": final_report,
                    "exportables": exportables,
                    "refactoring": refactoring,
                    "refactored_script": refactored_script,
                    "confirmed_script": confirmed_script,
                    "manual_changes": manual_changes,
                    "confirmation_used_llm": confirmation_used_llm,
                    "resume_from_stage": resume_from_stage or "confirmation",
                    "logs": self.execution_logs,
                    "llm_overrides": llm_overrides,
                }
            except Exception as exc:
                self.push_event("PIPELINE_ERROR", "logs", {"error": str(exc), "traceback": traceback.format_exc()}, pipeline_id)
                session = self._get_session(pipeline_id)
                if session:
                    session["state"] = "ERROR"
                    session["data"]["error"] = str(exc)
                    session["paused_at"] = "ERROR"
                    session["queue"].put(None)
                return {"status": "error", "error": str(exc), "pipeline_id": pipeline_id, "logs": self.execution_logs}


async def validate_llm_connection(provider: str, model: str, api_key: str) -> bool:
    return LLMFactory.validate_connection(provider, model, api_key)


def map_extracted_data_to_steps(module: Module):
    return _map_extracted_data_to_steps(module)

