"""Shared helpers for the GenIA pipeline."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, List

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig
except ImportError:  # pragma: no cover - optional runtime dependency
    AsyncWebCrawler = None
    BrowserConfig = None

from backend.domain.models import ExtractedElement, Module, TestCase
from backend.infrastructure.llm_factory import LLMFactory


logger = logging.getLogger(__name__)


def _model_dump(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else value


def _model_dump_list(items: List[Any]) -> List[Any]:
    return [_model_dump(item) for item in items]


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, indent=2)


def _render_prompt(template: str, **values: Any) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{key}}}", _safe_json_dumps(value) if not isinstance(value, str) else value)
    return rendered


def _append_context_block(prompt: str, context: Any) -> str:
    prompt = prompt.rstrip()
    return f"{prompt}\n\nCONTEXT PAYLOAD:\n{_safe_json_dumps(context)}"


def _truncate_text(value: Any, limit: int = 1200) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}... [truncated]"


def _compact_html_snapshot(value: Any, limit: int = 12000) -> str:
    if not isinstance(value, str):
        return ""
    compacted = re.sub(r"\s+", " ", value).strip()
    if len(compacted) <= limit:
        return compacted
    half = max(1, limit // 2)
    head = compacted[:half].rstrip()
    tail = compacted[-half:].lstrip()
    return f"{head} ... [truncated] ... {tail}"


def _compact_log_entries(entries: Any, limit: int = 80, text_limit: int = 300) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    compacted = []
    for item in entries[-limit:]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "timestamp": item.get("timestamp"),
                "level": item.get("level"),
                "stage": item.get("stage"),
                "message": _truncate_text(item.get("message"), text_limit),
                "type": item.get("type"),
            }
        )
    return compacted


def _compact_sequence(items: Any, limit: int = 25) -> list[Any]:
    if not isinstance(items, list):
        return []
    return [_json_safe(item) for item in items[-limit:]]


def _compact_history_entries(entries: Any, limit: int = 25) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    compacted = []
    for item in entries[-limit:]:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        payload_keys = list(payload.keys())[:12] if isinstance(payload, dict) else []
        compacted.append(
            {
                "timestamp": item.get("timestamp"),
                "stage": item.get("stage"),
                "event": item.get("event") or item.get("event_type"),
                "payload_keys": payload_keys,
            }
        )
    return compacted


def _compact_timeline_entries(entries: Any, limit: int = 30) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    compacted = []
    for item in entries[-limit:]:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        payload_keys = list(payload.keys())[:12] if isinstance(payload, dict) else []
        compacted.append(
            {
                "timestamp": item.get("timestamp"),
                "stage": item.get("stage"),
                "event_type": item.get("event_type"),
                "payload_keys": payload_keys,
            }
        )
    return compacted


def _compact_execution_result(execution_result: Any) -> dict[str, Any]:
    data = _model_dump(execution_result)
    if not isinstance(data, dict):
        return {"value": _truncate_text(str(data), 1500)}
    return {
        "status": data.get("status"),
        "framework": data.get("framework"),
        "language": data.get("language"),
        "runtime": data.get("runtime"),
        "duration_seconds": data.get("duration_seconds"),
        "exit_code": data.get("exit_code"),
        "stdout": _truncate_text(data.get("stdout"), 1800),
        "stderr": _truncate_text(data.get("stderr"), 1800),
        "stacktrace": _truncate_text(data.get("stacktrace"), 1800),
        "logs": _compact_sequence(data.get("logs") or data.get("execution_log_lines") or [], 20),
        "screenshots": _compact_sequence(data.get("screenshots") or [], 10),
        "traces": _compact_sequence(data.get("traces") or [], 10),
        "evidence": _compact_sequence(data.get("evidence") or [], 10),
        "test_results": _json_safe(data.get("test_results") or {}),
    }


def _compact_execution_artifacts(execution_result: Any) -> dict[str, Any]:
    data = _model_dump(execution_result)
    if not isinstance(data, dict):
        return {"screenshots": [], "primary_screenshot": None, "image_evidence": []}

    screenshots = [path for path in (data.get("screenshots") or []) if isinstance(path, str) and path.strip()]
    evidence = [path for path in (data.get("evidence") or []) if isinstance(path, str) and path.strip()]
    image_evidence = [
        path
        for path in evidence
        if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    combined_screenshots = screenshots or image_evidence

    return {
        "screenshots": _compact_sequence(combined_screenshots, 5),
        "primary_screenshot": combined_screenshots[0] if combined_screenshots else None,
        "image_evidence": _compact_sequence(image_evidence, 5),
        "screenshot_count": len(combined_screenshots),
        "evidence_count": len(evidence),
    }


def _compact_module_snapshot(module: Any, html_limit: int = 12000) -> dict[str, Any]:
    data = _model_dump(module)
    if not isinstance(data, dict):
        return {"value": _truncate_text(str(data), 1500)}
    source_html = data.get("source_html") or ""
    return {
        "url": data.get("url"),
        "purpose": data.get("purpose"),
        "execution_steps": _compact_sequence(data.get("execution_steps") or [], 50),
        "extracted_data": _compact_sequence(data.get("extracted_data") or [], 50),
        "source_html": _compact_html_snapshot(source_html, html_limit) if source_html else "",
    }


def _require_crawl4ai() -> None:
    if AsyncWebCrawler is None:
        raise RuntimeError(
            "crawl4ai is not installed in this environment. "
            "Install the backend dependencies to use extraction and refinement."
        )
    if BrowserConfig is not None:
        try:
            BrowserConfig.set_defaults(
                browser_mode="dedicated",
                cache_cdp_connection=True,
                cdp_close_delay=0,
                create_isolated_context=True,
            )
        except Exception:
            pass


def _create_browser_config(headless: bool) -> Any:
    return BrowserConfig(
        browser_type="chromium",
        browser_mode="dedicated",
        headless=headless,
        verbose=True,
        text_mode=True,
        light_mode=True,
        cache_cdp_connection=True,
        cdp_close_delay=0,
        create_isolated_context=True,
    )


def _is_crawl4ai_browser_lifecycle_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser closed",
        )
    )


def _iso_now() -> str:
    return datetime.now().isoformat()


def _normalize_manual_changes(manual_changes: Any) -> dict[str, Any]:
    if not manual_changes:
        return {}
    if isinstance(manual_changes, dict):
        return manual_changes
    if isinstance(manual_changes, list):
        return {"manual_inputs": manual_changes}
    return {"value": manual_changes}


def _build_timeline_payload(session: dict[str, Any], stage: str, event_type: str, payload: Any) -> dict[str, Any]:
    return {
        "timestamp": _iso_now(),
        "stage": stage,
        "event": event_type,
        "payload": _json_safe(payload),
    }


def _map_extracted_data_to_steps(module: Module):
    extracted_items = module.extracted_data or []
    normalized_items = []

    for item in extracted_items:
        if isinstance(item, dict):
            normalized_items.append(ExtractedElement(**item))
        elif isinstance(item, ExtractedElement):
            normalized_items.append(item)

    for step in module.execution_steps:
        matched_data = [item.model_copy() for item in normalized_items if item.step_name == step.step]
        if matched_data:
            step.extracted_data = matched_data

    return module


def _extract_html_snapshot(result: Any) -> str | None:
    for attr in ("html", "raw_html", "page_source", "cleaned_html", "markdown"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _normalize_extracted_elements(raw: Any) -> List[ExtractedElement]:
    if not raw:
        return []

    if isinstance(raw, dict):
        candidates = raw.get("items") or raw.get("elements") or raw.get("extracted_elements") or raw.get("data") or []
    else:
        candidates = raw

    if not isinstance(candidates, list):
        candidates = [candidates]

    normalized: list[ExtractedElement] = []
    for item in candidates:
        if isinstance(item, ExtractedElement):
            normalized.append(item)
        elif isinstance(item, dict):
            normalized.append(ExtractedElement(**item))
    return normalized


def _sanitize_module_tree(test_case: TestCase | dict[str, Any]) -> dict[str, Any]:
    payload = _model_dump(test_case)
    modules = payload.get("modules", []) if isinstance(payload, dict) else []
    for module in modules:
        if isinstance(module, dict) and "extracted_data" in module:
            module["extracted_data"] = []
    return payload


def _build_execution_report_html(final_report: Dict[str, Any]) -> str:
    sections = []
    for key, value in final_report.items():
        sections.append(
            f"<section><h2>{html.escape(str(key))}</h2><pre>{html.escape(json.dumps(_json_safe(value), ensure_ascii=False, indent=2))}</pre></section>"
        )
    return "<html><body>" + "".join(sections) + "</body></html>"


def _build_execution_report_markdown(final_report: Dict[str, Any]) -> str:
    lines = ["# Finalization Report", ""]
    for key, value in final_report.items():
        lines.append(f"## {key}")
        lines.append("```json")
        lines.append(json.dumps(_json_safe(value), ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


async def validate_llm_connection(provider: str, model: str, api_key: str) -> bool:
    return LLMFactory.validate_connection(provider, model, api_key)

