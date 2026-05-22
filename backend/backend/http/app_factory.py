"""Flask application factory for the REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from queue import Empty
import traceback
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Dict

try:
    from flask import Flask, Response, jsonify, request, send_file, stream_with_context
    from flask_cors import CORS
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    from flask_socketio import SocketIO
except ImportError:  # pragma: no cover - optional runtime dependency
    Flask = None
    jsonify = None
    request = None
    CORS = None
    Limiter = None
    get_remote_address = None
    SocketIO = None

from backend.application.pipeline import GenIAOrchestrator, validate_llm_connection
from backend.config import get_settings
from backend.domain.frameworks import get_available_frameworks, get_framework_languages
from backend.domain.models import ExecutionStep, Module, TestCase


logger = logging.getLogger("genia.api")
settings = get_settings()


def trace(message: str) -> None:
    print(f"[GenIA Backend] {message}", flush=True)
    logger.info(message)


def model_to_dict(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else value


def validate_request_json(*required_fields):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not request.is_json:
                trace(f"Invalid request content type for {request.method} {request.path}")
                return jsonify({"error": "Request must be JSON"}), 400

            data = request.get_json(force=True)
            trace(
                f"Incoming JSON request {request.method} {request.path} "
                f"keys={sorted(list(data.keys()))}"
            )

            for field in required_fields:
                if field not in data:
                    trace(f"Missing required field {field} on {request.method} {request.path}")
                    return jsonify({"error": f"Missing required field: {field}"}), 400

            return f(*args, **kwargs)

        return decorated_function

    return decorator


def error_handler(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            trace(f"Handling request {request.method} {request.path}")
            return f(*args, **kwargs)
        except ValueError as e:
            traceback.print_exc()
            return jsonify({"error": str(e), "type": "validation_error"}), 400
        except FileNotFoundError as e:
            traceback.print_exc()
            return jsonify({"error": str(e), "type": "file_error"}), 404
        except Exception as e:
            traceback.print_exc()
            trace(f"Unhandled error on {request.method} {request.path}: {e}")
            return jsonify({"error": str(e), "type": "internal_error", "traceback": traceback.format_exc()}), 500

    return decorated_function


def build_module(module_data: Dict[str, Any], *, include_extracted: bool = False, extracted_data=None) -> Module:
    execution_steps = [ExecutionStep(**step) for step in module_data.get("execution_steps", [])]
    payload = {
        "url": module_data["url"],
        "purpose": module_data.get("purpose", ""),
        "execution_steps": execution_steps,
    }
    if include_extracted:
        payload["extracted_data"] = extracted_data
    return Module(**payload)


def build_test_case(refined_json: Dict[str, Any]) -> TestCase:
    modules = [build_module(mod_data) for mod_data in refined_json.get("modules", [])]
    return TestCase(testCase=refined_json.get("testCase", "Generated Test"), modules=modules)


def create_app() -> tuple[Flask, SocketIO, GenIAOrchestrator]:
    if Flask is None:
        raise RuntimeError(
            "Flask dependencies are not installed in this environment. "
            "Install the backend requirements to start the API."
        )

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_size_mb * 1024 * 1024

    CORS(app, resources={r"/api/*": {"origins": "*"}})
    socketio = SocketIO(app, cors_allowed_origins="*")
    limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"], storage_uri="memory://")
    orchestrator = GenIAOrchestrator(prompts_dir=settings.prompts_dir)

    @app.before_request
    def log_request_start():
        request.request_id = str(uuid.uuid4())[:8]
        trace(f"[{request.request_id}] --> {request.method} {request.path} from {request.remote_addr}")

    @app.after_request
    def apply_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        request_id = getattr(request, "request_id", "--------")
        trace(f"[{request_id}] <-- {request.method} {request.path} status={response.status_code}")
        response.headers["X-Request-Id"] = request_id
        return response

    @app.route("/api/health", methods=["GET"])
    def health_check():
        return jsonify({"status": "ok", "service": "GenIA E2E Test Platform", "version": "1.0.0"}), 200

    @app.route("/api/info", methods=["GET"])
    def platform_info():
        return jsonify(
            {
                "frameworks": get_available_frameworks(),
                "providers": ["openai", "anthropic", "gemini", "cohere"],
                "max_upload_size_mb": settings.max_upload_size_mb,
                "supported_features": [
                    "test_structuring",
                    "element_extraction",
                    "selector_refinement",
                    "script_generation",
                    "variable_validation",
                    "manual_validation_pause",
                    "real_time_logs",
                    "multi_framework_execution",
                ],
            }
        ), 200

    @app.route("/api/llm/validate", methods=["POST"])
    @limiter.limit("10 per minute")
    @validate_request_json("provider", "model", "api_key")
    @error_handler
    def validate_llm():
        data = request.get_json(force=True)
        is_valid = asyncio.run(validate_llm_connection(data["provider"], data["model"], data["api_key"]))
        return jsonify({"valid": is_valid, "provider": data["provider"], "model": data["model"]}), 200

    @app.route("/api/llm/models", methods=["GET"])
    @error_handler
    def get_llm_models():
        models = {
            "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
            "anthropic": ["claude-3-5-sonnet-latest", "claude-3-opus-latest"],
            "gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
            "cohere": ["command-r", "command-r-plus"],
        }
        return jsonify(models), 200

    @app.route("/api/frameworks", methods=["GET"])
    @error_handler
    def get_frameworks():
        frameworks = {fw: get_framework_languages(fw) for fw in get_available_frameworks()}
        return jsonify(frameworks), 200

    @app.route("/api/pipeline/initialize", methods=["POST"])
    @validate_request_json("provider", "model", "api_key")
    @error_handler
    def initialize_pipeline():
        data = request.get_json(force=True)
        orchestrator.set_provider(data["provider"], data["api_key"], data["model"])
        return jsonify({"status": "initialized", "provider": data["provider"], "model": data["model"]}), 200

    @app.route("/api/pipeline/prompts", methods=["GET"])
    @error_handler
    def pipeline_prompts():
        framework = request.args.get("framework")
        prompts = orchestrator.prompt_manager.load_prompt_bundle(framework)
        return jsonify({"status": "success", "prompts": prompts}), 200

    @app.route("/api/pipeline/restructure", methods=["POST"])
    @validate_request_json("test_case", "urls", "project", "mode")
    @error_handler
    def restructure():
        data = request.get_json(force=True)
        structured = asyncio.run(orchestrator.run_structuring(data["test_case"]))
        return jsonify({"status": "success", "data": model_to_dict(structured), "logs": orchestrator.execution_logs}), 200

    @app.route("/api/pipeline/extract", methods=["POST"])
    @validate_request_json("structured_json", "urls")
    @error_handler
    def extract():
        data = request.get_json(force=True)
        module = build_module(data["structured_json"])
        extracted = asyncio.run(orchestrator.run_extraction(module, headless=data.get("headless", True)))
        return jsonify({"status": "success", "data": [model_to_dict(e) for e in extracted], "logs": orchestrator.execution_logs}), 200

    @app.route("/api/pipeline/refine", methods=["POST"])
    @validate_request_json("extracted_json", "structured_json", "urls")
    @error_handler
    def refine():
        data = request.get_json(force=True)
        module = build_module(data["structured_json"], include_extracted=True, extracted_data=data.get("extracted_json", []))
        refined = asyncio.run(orchestrator.run_refinement(module, headless=data.get("headless", True)))
        return jsonify({"status": "success", "data": [model_to_dict(e) for e in refined], "logs": orchestrator.execution_logs}), 200

    @app.route("/api/pipeline/generate-script", methods=["POST"])
    @validate_request_json("refined_json", "framework", "language", "project")
    @error_handler
    def generate_script():
        data = request.get_json(force=True)
        test_case = build_test_case(data["refined_json"])
        script = asyncio.run(orchestrator.run_generation(test_case, data["framework"], data["language"]))
        return jsonify({"status": "success", "data": script, "logs": orchestrator.execution_logs}), 200

    @app.route("/api/pipeline/validate", methods=["POST"])
    @validate_request_json("script", "refined_json", "framework", "language")
    @error_handler
    def validate():
        data = request.get_json(force=True)
        test_case = build_test_case(data["refined_json"])
        variables = asyncio.run(orchestrator.run_validation(data["script"], test_case))
        return jsonify({"status": "success", "data": variables, "logs": orchestrator.execution_logs}), 200

    @app.route("/api/pipeline/full-run", methods=["POST"])
    @validate_request_json("test_case", "framework", "language", "provider", "model", "api_key")
    @error_handler
    def full_pipeline_run():
        data = request.get_json(force=True)
        orchestrator.set_provider(data["provider"], data["api_key"], data["model"])
        results = asyncio.run(
            orchestrator.run_full_pipeline_1(
                test_case=data["test_case"],
                framework=data["framework"],
                language=data["language"],
                test_name=data.get("test_name", "untitled"),
                pipeline_id=data.get("pipeline_id"),
                prompt_overrides=data.get("prompt_overrides"),
                llm_overrides=data.get("llm_overrides"),
            )
        )
        return jsonify(results), 200 if results.get("status") != "error" else 500

    @app.route("/api/pipeline/run-until-validation", methods=["POST"])
    @validate_request_json("test_case", "framework", "language", "provider", "model", "api_key")
    @error_handler
    def run_until_validation():
        data = request.get_json(force=True)
        orchestrator.set_provider(data["provider"], data["api_key"], data["model"])
        result = asyncio.run(
            orchestrator.run_full_pipeline_1(
                test_case=data["test_case"],
                framework=data["framework"],
                language=data["language"],
                test_name=data.get("test_name", "test"),
                headless=data.get("headless", True),
                attempt=data.get("attempt", 1),
                pipeline_id=data.get("pipeline_id"),
                prompt_overrides=data.get("prompt_overrides"),
                llm_overrides=data.get("llm_overrides"),
            )
        )
        return jsonify(result)

    @app.route("/api/pipeline/continue-after-validation", methods=["POST"])
    @validate_request_json("pipeline_id")
    @error_handler
    def continue_after_validation():
        data = request.get_json(force=True)
        result = asyncio.run(
            orchestrator.run_full_pipeline_2(
                pipeline_id=data["pipeline_id"],
                manual_changes=data.get("manual_changes"),
                continue_without_changes=data.get("continue_without_changes", True),
                resume_from_stage=data.get("resume_from_stage"),
                script_override=data.get("script_override"),
                prompt_overrides=data.get("prompt_overrides"),
                llm_overrides=data.get("llm_overrides"),
            )
        )
        return jsonify(result)

    @app.route("/api/pipeline/logs/stream/<pipeline_id>", methods=["GET"])
    def stream_pipeline_logs(pipeline_id: str):
        session = orchestrator.pipeline_sessions.get(pipeline_id)
        if not session:
            session = orchestrator._create_session(pipeline_id)

        @stream_with_context
        def generate():
            yield "retry: 1000\n\n"
            queue = session["queue"]
            while True:
                try:
                    event = queue.get(timeout=15)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue

                if event is None:
                    yield "event: done\ndata: {}\n\n"
                    break

                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/api/pipeline/artifact", methods=["GET"])
    def pipeline_artifact():
        file_path = request.args.get("path", "")
        if not file_path:
            return jsonify({"error": "Missing path"}), 400

        candidate = Path(file_path).expanduser().resolve()
        allowed_roots = [Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve()]

        if not any(str(candidate).startswith(str(root)) for root in allowed_roots):
            return jsonify({"error": "Forbidden artifact path"}), 403
        if not candidate.exists() or not candidate.is_file():
            return jsonify({"error": "Artifact not found"}), 404

        return send_file(candidate)

    @app.route("/api/pipeline/status/<pipeline_id>", methods=["GET"])
    def pipeline_status(pipeline_id: str):
        session = orchestrator.pipeline_sessions.get(pipeline_id)
        if not session:
            return jsonify({"status": "missing", "pipeline_id": pipeline_id}), 404
        return jsonify(
            {
                "status": session.get("state"),
                "pipeline_id": pipeline_id,
                "paused_at": session.get("paused_at"),
                "history": session.get("history", []),
                "timeline": session.get("timeline", []),
            }
        ), 200

    @app.route("/api/logs", methods=["GET"])
    def get_logs():
        return jsonify({"ok": True, "logs": orchestrator.execution_logs, "count": len(orchestrator.execution_logs)})

    @app.route("/api/logs/clear", methods=["POST"])
    @error_handler
    def clear_logs():
        orchestrator.execution_logs = []
        return jsonify({"status": "cleared"}), 200

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({"error": "Endpoint not found"}), 404

    @app.errorhandler(500)
    def internal_error(error):
        traceback.print_exc()
        return jsonify({"error": str(error), "traceback": traceback.format_exc()}), 500

    return app, socketio, orchestrator
