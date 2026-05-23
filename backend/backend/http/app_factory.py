"""Flask application factory for the REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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


def _load_allowed_origins() -> list[str]:
    raw = os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5500,https://genia-e2etest-platform.onrender.com,https://genia-e2etest-ai-driven-platform.onrender.com",
    )
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["*"]


def _is_allowed_origin(origin: str | None) -> bool:
    if not origin:
        return False
    if origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:"):
        return True
    if origin == "https://genia-e2etest-platform.onrender.com":
        return True
    if origin == "https://genia-e2etest-ai-driven-platform.onrender.com":
        return True
    return bool(re.match(r"^https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.onrender\.com$", origin))


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

    # ✅ CORREÇÃO 1: Carregar origins permitidas e usar na configuração CORS
    allowed_origins = _load_allowed_origins()
    trace(f"Allowed CORS origins: {allowed_origins}")

    # ✅ CORREÇÃO 2: Configuração CORS robusta
    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": allowed_origins,
                "methods": ["GET", "POST", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization"],
                "supports_credentials": False,
                "max_age": 3600,
                "send_wildcard": False,
            }
        },
        vary_header=True,
    )
    
    socketio = SocketIO(app, cors_allowed_origins=allowed_origins)
    limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"], storage_uri="memory://")
    orchestrator = GenIAOrchestrator(prompts_dir=settings.prompts_dir)

    @app.before_request
    def log_request_start():
        request.request_id = str(uuid.uuid4())[:8]
        origin = request.headers.get("Origin", "no-origin")
        trace(f"[{request.request_id}] --> {request.method} {request.path} from {request.remote_addr} (origin: {origin})")

    # ✅ CORREÇÃO 3: Simplificar apply_headers - deixar CORS para flask-cors
    @app.after_request
    def apply_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
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
                    "data_extraction",
                    "test_refinement",
                    "code_generation",
                    "validation",
                    "execution",
                ],
            }
        ), 200

    @app.route("/api/frameworks", methods=["GET"])
    @error_handler
    def get_frameworks():
        framework = request.args.get("framework", "").strip()
        if not framework:
            return jsonify({"frameworks": get_available_frameworks()}), 200
        languages = get_framework_languages(framework)
        return jsonify({"framework": framework, "languages": languages}), 200

    @app.route("/api/test-case/structure", methods=["POST"])
    @validate_request_json("test_case", "provider", "model", "api_key")
    @error_handler
    def structure_test_case():
        data = request.get_json(force=True)
        orchestrator.set_provider(data["provider"], data["api_key"], data["model"], data.get("temperature"))
        structured = asyncio.run(orchestrator.run_structuring(data["test_case"]))
        return jsonify({"status": "success", "data": model_to_dict(structured), "logs": orchestrator.execution_logs}), 200

    @app.route("/api/pipeline/restructure", methods=["POST"])
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
        orchestrator.set_provider(data["provider"], data["api_key"], data["model"], data.get("temperature"))
        results = asyncio.run(
            orchestrator.run_full_pipeline_1(
                test_case=data["test_case"],
                framework=data["framework"],
                language=data["language"],
                test_name=data.get("test_name", "untitled"),
                pipeline_id=data.get("pipeline_id"),
                prompt_overrides=data.get("prompt_overrides"),
                llm_overrides=data.get("llm_overrides"),
                input_mode=data.get("input_mode", "test_case"),
                user_story_content=data.get("user_story_content"),
                user_story_filename=data.get("user_story_filename"),
                manual_urls=data.get("manual_urls") or [],
            )
        )
        return jsonify(results), 200 if results.get("status") != "error" else 500

    @app.route("/api/pipeline/run-until-validation", methods=["POST"])
    @validate_request_json("test_case", "framework", "language", "provider", "model", "api_key")
    @error_handler
    def run_until_validation():
        data = request.get_json(force=True)
        orchestrator.set_provider(data["provider"], data["api_key"], data["model"], data.get("temperature"))
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
                input_mode=data.get("input_mode", "test_case"),
                user_story_content=data.get("user_story_content"),
                user_story_filename=data.get("user_story_filename"),
                manual_urls=data.get("manual_urls") or [],
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

    # ✅ CORREÇÃO 4: Adicionar CORS headers mesmo em erros 404 e 500
    @app.errorhandler(404)
    def not_found(error):
        trace(f"404 Not Found: {request.method} {request.path}")
        response = jsonify({"error": "Endpoint not found", "path": request.path})
        request_origin = request.headers.get("Origin")
        
        # Adicionar CORS headers mesmo em 404
        if request_origin and _is_allowed_origin(request_origin):
            response.headers["Access-Control-Allow-Origin"] = request_origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        elif "*" in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        
        return response, 404

    @app.errorhandler(500)
    def internal_error(error):
        trace(f"500 Internal Server Error: {str(error)}")
        traceback.print_exc()
        response = jsonify({"error": str(error), "traceback": traceback.format_exc()})
        request_origin = request.headers.get("Origin")
        
        # Adicionar CORS headers mesmo em 500
        if request_origin and _is_allowed_origin(request_origin):
            response.headers["Access-Control-Allow-Origin"] = request_origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        elif "*" in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        
        return response, 500

    return app, socketio, orchestrator