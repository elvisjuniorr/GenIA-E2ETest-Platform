"""Shared execution helpers for framework/language strategy classes."""

from __future__ import annotations

import glob
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


class BaseExecutionStrategy(ABC):
    framework_name: str = ""
    supported_languages: tuple[str, ...] = ()
    default_language: str = "python"
    timeout_seconds: int = 600

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

            def end_test(data, result):
                if getattr(result, "status", "") != "FAIL":
                    return
                try:
                    from robot.libraries.BuiltIn import BuiltIn
                    built_in = BuiltIn()
                    try:
                        built_in.run_keyword("Capture Page Screenshot")
                    except Exception:
                        built_in.run_keyword("Capture Screenshot")
                except Exception:
                    pass
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

    def _safe_process(self, command: list[str], cwd: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=self._build_env(),
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
    ) -> ExecutionResult:
        execution_log_lines.append(f"[ERROR] {message}")
        return ExecutionResult(
            stdout="",
            stderr=message,
            status="error",
            duration=time.time() - start,
            execution_log_lines=execution_log_lines,
            stacktrace=traceback.format_exc(),
            framework=framework,
            runtime=framework,
        )

    def resolve_script_path(self, temp_dir: str, filename: str) -> str:
        return os.path.join(temp_dir, filename)

    def determine_filename(self, framework: str, script: str, language: str | None = None) -> str:
        return f"test_{uuid.uuid4().hex}.py"

    def normalize_script(self, script: str, language: str | None = None) -> str:
        return script

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
    ) -> ExecutionResult:
        start = time.time()
        execution_log_lines: list[str] = []
        temp_dir = tempfile.mkdtemp(prefix="genia-exec-")

        try:
            resolved_language = (language or self.default_language or "").lower()
            filename = self.determine_filename(framework, script, resolved_language)
            normalized_script = self.normalize_script(script, resolved_language)
            file_path = self.resolve_script_path(temp_dir, filename)
            file_path = self._write_file(os.path.dirname(file_path), os.path.basename(file_path), normalized_script)
            self.prepare_workspace(temp_dir, file_path, normalized_script, resolved_language)
            execution_log_lines.append(f"[SETUP] Workspace: {temp_dir}")
            execution_log_lines.append(f"[SETUP] Script file: {file_path}")
            self._emit(log_callback, "info", f"Script preparado em {file_path}")

            command = self.build_command(file_path, resolved_language, temp_dir)
            execution_log_lines.append(f"[EXECUTION] Command: {' '.join(command)}")
            self._emit(log_callback, "info", f"Executando {' '.join(command)}")

            process = self._safe_process(command, cwd=temp_dir)
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
            )
        except subprocess.TimeoutExpired:
            self._emit(log_callback, "error", "Execução excedeu o tempo limite")
            return self._error_result(
                framework=framework,
                start=start,
                execution_log_lines=execution_log_lines,
                message="Execution timeout",
            )
        except Exception as exc:
            self._emit(log_callback, "error", str(exc))
            return self._error_result(
                framework=framework,
                start=start,
                execution_log_lines=execution_log_lines,
                message=str(exc),
            )


class GenericPythonExecutionStrategy(BaseExecutionStrategy):
    framework_name = "python"
    supported_languages = ("python",)
    default_language = "python"

    def build_command(self, file_path: str, language: str | None = None, temp_dir: str | None = None) -> list[str]:
        return [self._python_command(), file_path]
