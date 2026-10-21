"""Shared execution helpers for framework/language strategy classes."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import uuid
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from textwrap import dedent
from typing import Callable, Optional

from backend.domain.models import ExecutionResult

LogCallback = Callable[[str, str], None]


class ExecutionPreflightError(Exception):
    """Framework-neutral failure raised before a test runner starts the test."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "", phase: str = "script_validation") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.phase = phase


class BaseExecutionStrategy(ABC):
    framework_name: str = ""
    supported_languages: tuple[str, ...] = ()
    default_language: str = "python"
    timeout_seconds: int = 600
    supports_candidate_validation: bool = False

    def _python_command(self) -> str:
        return sys.executable or "python"

    def _is_unix_like(self) -> bool:
        return os.name != "nt"

    def _emit(self, callback: Optional[LogCallback], level: str, message: str) -> None:
        if callback:
            callback(level, message)

    def _write_file(self, directory: str, filename: str, content: str) -> str:
        file_path = os.path.join(directory, filename)
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text(content, encoding="utf-8")
        return file_path

    def _collect_artifacts(self, directory: str) -> list[str]:
        collected: list[str] = []
        artifact_extensions = {".png", ".jpg", ".jpeg", ".webp", ".webm", ".mp4", ".zip", ".trace", ".html", ".xml", ".json", ".log"}

        for root, _, files in os.walk(directory):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                if Path(file_path).suffix.lower() in artifact_extensions:
                    collected.append(file_path)
        return collected

    def _stream_process(
        self,
        process: subprocess.Popen[str],
        execution_log_lines: list[str],
        callback: Optional[LogCallback] = None,
    ) -> tuple[str, str]:
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def drain(stream, prefix: str, store: list[str], level: str) -> None:
            if not stream:
                return
            for line in iter(stream.readline, ""):
                clean = line.rstrip("\n")
                marker = "__GENIA_VISUAL__"
                marker_index = clean.find(marker)
                if marker_index >= 0:
                    before = clean[:marker_index].strip()
                    if before:
                        store.append(before)
                        execution_log_lines.append(f"[{prefix}] {before}")
                        self._emit(callback, level, before)

                    encoded_frame = clean[marker_index + len(marker):].lstrip()
                    try:
                        frame, consumed = json.JSONDecoder().raw_decode(encoded_frame)
                    except (TypeError, ValueError):
                        frame = None
                        consumed = len(encoded_frame)
                    if isinstance(frame, dict):
                        self._emit(
                            callback,
                            "visual",
                            json.dumps(frame, ensure_ascii=True, separators=(",", ":")),
                        )
                    after = encoded_frame[consumed:].strip()
                    if after:
                        store.append(after)
                        execution_log_lines.append(f"[{prefix}] {after}")
                        self._emit(callback, level, after)
                    continue

                store.append(clean)
                execution_log_lines.append(f"[{prefix}] {clean}")
                self._emit(callback, level, clean)

        threads: list[threading.Thread] = []
        if process.stdout:
            stdout_thread = threading.Thread(target=drain, args=(process.stdout, "STDOUT", stdout_lines, "info"), daemon=True)
            threads.append(stdout_thread)
            stdout_thread.start()
        if process.stderr:
            stderr_thread = threading.Thread(target=drain, args=(process.stderr, "STDERR", stderr_lines, "error"), daemon=True)
            threads.append(stderr_thread)
            stderr_thread.start()

        process.wait(timeout=self.timeout_seconds)

        for thread in threads:
            thread.join(timeout=1)

        return "\n".join(stdout_lines), "\n".join(stderr_lines)

    def _discover_chrome_binary(self) -> str | None:
        candidates: list[str] = []
        # Container images may provide a Chrome wrapper that supplies the
        # flags required by headless/container server execution. Prefer it over
        # the underlying Playwright browser binary so SeleniumLibrary uses the
        # same stable launch configuration.
        candidates.extend(("/usr/local/bin/google-chrome", "/usr/bin/google-chrome"))
        candidates.extend(glob.glob("/ms-playwright/chromium-*/chrome-linux64/chrome"))
        candidates.extend(glob.glob("/ms-playwright/chromium-*/chrome-linux/chrome"))
        candidates.extend(glob.glob("/usr/bin/chromium"))
        candidates.extend(glob.glob("/usr/bin/chromium-browser"))
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _write_robot_listener(self, directory: str) -> str:
        listener_source = dedent(
            """
            ROBOT_LISTENER_API_VERSION = 3

            import base64
            import json
            import os
            import sys
            import time

            _visual_frame_count = 0
            _last_visual_frame_at = 0.0

            def _capture_execution_screenshot(data):
                try:
                    from robot.libraries.BuiltIn import BuiltIn
                    library = BuiltIn().get_library_instance("SeleniumLibrary")
                    test_name = "".join(
                        char if char.isalnum() or char in "-_" else "_"
                        for char in str(getattr(data, "name", "test"))
                    )[:60]
                    screenshot_path = os.path.join(
                        os.path.dirname(__file__),
                        f"execution-{test_name or 'test'}.png",
                    )
                    library.capture_page_screenshot(screenshot_path)
                except Exception:
                    pass

            def start_keyword(data, result):
                keyword_name = str(getattr(data, "name", "")).strip().lower()
                if keyword_name in {"close browser", "close all browsers"}:
                    _capture_execution_screenshot(data)

            def end_test(data, result):
                _capture_execution_screenshot(data)
                _close_all_browsers()

            def end_suite(data, result):
                _close_all_browsers()

            def _close_all_browsers():
                try:
                    from robot.libraries.BuiltIn import BuiltIn
                    library = BuiltIn().get_library_instance("SeleniumLibrary")
                    library.close_all_browsers()
                except Exception:
                    pass

            def end_keyword(data, result):
                global _visual_frame_count, _last_visual_frame_at
                if os.environ.get("GENIA_VISUALIZE_EXECUTION") != "1" or _visual_frame_count >= 100:
                    return
                now = time.monotonic()
                if now - _last_visual_frame_at < 0.3:
                    return
                try:
                    from robot.libraries.BuiltIn import BuiltIn
                    library = BuiltIn().get_library_instance("SeleniumLibrary")
                    screenshot_path = os.path.join(os.path.dirname(__file__), "execution-live.png")
                    library.capture_page_screenshot(screenshot_path)
                    with open(screenshot_path, "rb") as screenshot:
                        image = base64.b64encode(screenshot.read()).decode("ascii")
                    event = {
                        "image": image,
                        "mime": "image/png",
                        "label": str(getattr(data, "name", "Interagindo com a página"))[:100],
                    }
                    line = "__GENIA_VISUAL__" + json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\\n"
                    output = getattr(sys, "__stdout__", None) or sys.stdout
                    output.write(line)
                    output.flush()
                    _visual_frame_count += 1
                    _last_visual_frame_at = now
                except Exception:
                    return
            """
        ).strip()
        listener_path = os.path.join(directory, "genia_failure_listener.py")
        Path(listener_path).write_text(listener_source, encoding="utf-8")
        return listener_path

    def _normalize_robot_script(self, script: str) -> str:
        normalized = script.replace("\r\n", "\n")
        normalized = re.sub(
            r"(?m)^(\s*\$\{chrome_bin\}\s+)\$\{CHROME_BIN\}\s*$",
            r"\1%{CHROME_BIN}",
            normalized,
        )
        normalized = re.sub(
            r"(?m)^(\s*Run Keyword If\s+'\\$\\{chrome_bin\\}' != ''\s+Set Suite Variable\s+)\$\{chrome_options\.binary_location\}\s+\$\{chrome_bin\}\s*$",
            r"\1Evaluate    setattr($chrome_options, 'binary_location', $chrome_bin)",
            normalized,
        )
        normalized = re.sub(
            r"(?m)^(\s*Set Suite Variable\s+)\$\{chrome_options\.binary_location\}\s+\$\{chrome_bin\}\s*$",
            r"\1Evaluate    setattr($chrome_options, 'binary_location', $chrome_bin)",
            normalized,
        )
        return normalized

    def _default_runtime(self, command: list[str]) -> str | None:
        return " ".join(command[:2]) if command else None

    def _pytest_command(self, file_path: str) -> list[str]:
        pytest_executable = shutil.which("pytest")
        if pytest_executable:
            return [pytest_executable, file_path, "-q"]
        return [self._python_command(), "-m", "pytest", file_path, "-q"]

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("DISPLAY", env.get("DISPLAY", ":99"))
        chrome_binary = self._discover_chrome_binary()
        if chrome_binary:
            env.setdefault("CHROME_BIN", chrome_binary)
        return env

    def _safe_process(self, command: list[str], cwd: str, *, visualize_execution: bool = False) -> subprocess.Popen[str]:
        env = self._build_env()
        env["GENIA_VISUALIZE_EXECUTION"] = "1" if visualize_execution else "0"
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )

    def _result(
        self,
        *,
        framework: str,
        language: str | None,
        start: float,
        return_code: int,
        stdout_text: str,
        stderr_text: str,
        execution_log_lines: list[str],
        artifacts: list[str],
        command: list[str],
        execution_metadata: dict | None = None,
    ) -> ExecutionResult:
        screenshots = [path for path in artifacts if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
        traces = [path for path in artifacts if Path(path).suffix.lower() == ".trace"]
        evidence = [path for path in artifacts if Path(path).suffix.lower() in {".html", ".xml", ".json", ".log", ".zip", ".mp4", ".webm"}]
        is_passed = return_code == 0

        execution_log_lines.append(f"[RESULT] Process exit code: {return_code}")
        execution_log_lines.append(f"[RESULT] Status: {'PASSED' if is_passed else 'FAILED'}")

        return ExecutionResult(
            stdout=stdout_text,
            stderr=stderr_text,
            status="passed" if is_passed else "failed",
            duration=time.time() - start,
            screenshots=screenshots,
            traces=traces,
            logs=evidence,
            evidence=evidence,
            execution_log_lines=execution_log_lines,
            stacktrace=stderr_text if not is_passed else None,
            test_results={
                "exit_code": return_code,
                "artifacts_count": len(artifacts),
                "duration_seconds": round(time.time() - start, 2),
                "framework": framework,
                "language": language,
                **(execution_metadata or {}),
            },
            framework=framework,
            runtime=self._default_runtime(command),
        )

    def _error_result(
        self,
        *,
        framework: str,
        start: float,
        execution_log_lines: list[str],
        message: str,
        stdout_text: str = "",
        stderr_text: str | None = None,
        execution_metadata: dict | None = None,
    ) -> ExecutionResult:
        execution_log_lines.append(f"[ERROR] {message}")
        return ExecutionResult(
            stdout=stdout_text,
            stderr=stderr_text if stderr_text is not None else message,
            status="error",
            duration=time.time() - start,
            execution_log_lines=execution_log_lines,
            stacktrace=traceback.format_exc(),
            framework=framework,
            runtime=framework,
            test_results=execution_metadata,
        )

    def resolve_script_path(self, temp_dir: str, filename: str) -> str:
        return os.path.join(temp_dir, filename)

    def determine_filename(self, framework: str, script: str, language: str | None = None) -> str:
        return f"test_{uuid.uuid4().hex}.py"

    def normalize_script(self, script: str, language: str | None = None) -> str:
        return script

    def prepare_script(self, script: str, language: str | None = None) -> tuple[str, list[dict]]:
        """Optional framework-specific normalization with traceable changes."""
        return script, []

    def validate_prepared_script(self, file_path: str, temp_dir: str, language: str | None = None) -> None:
        """Optional framework-specific preflight; must not launch the test."""
        return None

    def validate_script_candidate(
        self,
        framework: str,
        script: str,
        language: str | None = None,
    ) -> tuple[str, list[dict]]:
        """Prepare and preflight a candidate without executing its test steps."""
        if not self.supports_candidate_validation:
            raise NotImplementedError(f"Candidate validation is not implemented for {framework}.")
        resolved_language = (language or self.default_language or "").lower()
        normalized = self.normalize_script(script, resolved_language)
        normalization_applied = normalized != script
        prepared, transformations = self.prepare_script(normalized, resolved_language)
        if normalization_applied:
            transformations.insert(0, {
                "type": "framework_normalization",
                "description": "Framework-specific script normalization was applied before validation.",
            })
        filename = self.determine_filename(framework, prepared, resolved_language)
        with tempfile.TemporaryDirectory(prefix="genia-script-check-") as temp_dir:
            file_path = self.resolve_script_path(temp_dir, filename)
            file_path = self._write_file(os.path.dirname(file_path), os.path.basename(file_path), prepared)
            self.prepare_workspace(temp_dir, file_path, prepared, resolved_language)
            self.validate_prepared_script(file_path, temp_dir, resolved_language)
        return prepared, transformations

    def prepare_workspace(self, temp_dir: str, file_path: str, script: str, language: str | None = None) -> None:
        return None

    @abstractmethod
    def build_command(self, file_path: str, language: str | None = None, temp_dir: str | None = None) -> list[str]:
        raise NotImplementedError

    def execute(
        self,
        framework: str,
        script: str,
        language: str | None = None,
        log_callback: Optional[LogCallback] = None,
        visualize_execution: bool = False,
    ) -> ExecutionResult:
        start = time.time()
        execution_log_lines: list[str] = []
        temp_dir = tempfile.mkdtemp(prefix="genia-exec-")
        source_script_sha256 = hashlib.sha256(script.encode("utf-8")).hexdigest()
        normalized_script = script
        transformations: list[dict] = []
        execution_metadata = {
            "execution_phase": "test_execution",
            "source_script_sha256": source_script_sha256,
            "executed_script_sha256": source_script_sha256,
            "script_transformations": transformations,
        }

        try:
            resolved_language = (language or self.default_language or "").lower()
            filename = self.determine_filename(framework, script, resolved_language)
            normalized_script = self.normalize_script(script, resolved_language)
            normalization_applied = normalized_script != script
            normalized_script, transformations = self.prepare_script(normalized_script, resolved_language)
            if normalization_applied:
                transformations.insert(0, {
                    "type": "framework_normalization",
                    "description": "Framework-specific script normalization was applied before execution.",
                })
            execution_metadata.update({
                "source_script_sha256": source_script_sha256,
                "executed_script_sha256": hashlib.sha256(normalized_script.encode("utf-8")).hexdigest(),
                "script_transformations": transformations,
            })
            file_path = self.resolve_script_path(temp_dir, filename)
            file_path = self._write_file(os.path.dirname(file_path), os.path.basename(file_path), normalized_script)
            self.prepare_workspace(temp_dir, file_path, normalized_script, resolved_language)
            execution_log_lines.append(f"[SETUP] Workspace: {temp_dir}")
            execution_log_lines.append(f"[SETUP] Script file: {file_path}")
            self._emit(log_callback, "info", f"Script preparado em {file_path}")

            self.validate_prepared_script(file_path, temp_dir, resolved_language)

            command = self.build_command(file_path, resolved_language, temp_dir)
            execution_log_lines.append(f"[EXECUTION] Command: {' '.join(command)}")
            self._emit(log_callback, "info", f"Executando {' '.join(command)}")

            process = self._safe_process(command, cwd=temp_dir, visualize_execution=visualize_execution)
            stdout_text, stderr_text = self._stream_process(process, execution_log_lines, log_callback)
            return_code = process.wait(timeout=self.timeout_seconds)
            artifacts = self._collect_artifacts(temp_dir)

            self._emit(
                log_callback,
                "success" if return_code == 0 else "error",
                f"Execução finalizada com status {'PASSED' if return_code == 0 else 'FAILED'}",
            )
            return self._result(
                framework=framework,
                language=language,
                start=start,
                return_code=return_code,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                execution_log_lines=execution_log_lines,
                artifacts=artifacts,
                command=command,
                execution_metadata=execution_metadata,
            )
        except ExecutionPreflightError as exc:
            execution_log_lines.append(f"[PREFLIGHT] {exc}")
            if exc.stdout:
                execution_log_lines.extend(f"[PREFLIGHT STDOUT] {line}" for line in exc.stdout.splitlines())
            if exc.stderr:
                execution_log_lines.extend(f"[PREFLIGHT STDERR] {line}" for line in exc.stderr.splitlines())
            self._emit(log_callback, "error", str(exc))
            return self._error_result(
                framework=framework,
                start=start,
                execution_log_lines=execution_log_lines,
                message=str(exc),
                stdout_text=exc.stdout,
                stderr_text=exc.stderr or str(exc),
                execution_metadata={
                    "execution_phase": exc.phase,
                    "source_script_sha256": source_script_sha256,
                    "executed_script_sha256": hashlib.sha256(normalized_script.encode("utf-8")).hexdigest(),
                    "script_transformations": transformations,
                },
            )
        except subprocess.TimeoutExpired:
            self._emit(log_callback, "error", "Execução excedeu o tempo limite")
            return self._error_result(
                framework=framework,
                start=start,
                execution_log_lines=execution_log_lines,
                message="Execution timeout",
                execution_metadata=execution_metadata,
            )
        except Exception as exc:
            self._emit(log_callback, "error", str(exc))
            return self._error_result(
                framework=framework,
                start=start,
                execution_log_lines=execution_log_lines,
                message=str(exc),
                execution_metadata=execution_metadata,
            )


class GenericPythonExecutionStrategy(BaseExecutionStrategy):
    framework_name = "python"
    supported_languages = ("python",)
    default_language = "python"

    def build_command(self, file_path: str, language: str | None = None, temp_dir: str | None = None) -> list[str]:
        return [self._python_command(), file_path]
