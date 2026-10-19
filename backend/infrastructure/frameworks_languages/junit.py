"""JUnit execution strategy."""

from __future__ import annotations

import re
import os
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from backend.domain.models import ExecutionResult

from .base import BaseExecutionStrategy
from .java_native import (
    build_java_classpath,
    build_junit_runner_source,
    discover_chromedriver_executable,
    discover_java_executable,
    discover_javac_executable,
    extract_java_package_name,
    extract_java_primary_class_name,
    validate_java_dependencies,
)
from .runtime import strip_code_fences

logger = logging.getLogger(__name__)


class JUnitJavaExecutionStrategy(BaseExecutionStrategy):
    framework_name = "junit"
    supported_languages = ("java",)
    default_language = "java"

    def determine_filename(self, framework: str, script: str, language: str | None = None) -> str:
        class_name = extract_java_primary_class_name(script) or "GeneratedTest"
        return f"{class_name}.java"

    def normalize_script(self, script: str, language: str | None = None) -> str:
        normalized = strip_code_fences(script).strip()
        if "new WebDriverWait(" in normalized and "java.time.Duration" not in normalized:
            normalized = re.sub(
                r"new\s+WebDriverWait\s*\(\s*([^,]+)\s*,\s*(\d+)\s*\)",
                r"new WebDriverWait(\1, Duration.ofSeconds(\2))",
                normalized,
            )
            if "import java.time.Duration;" not in normalized:
                package_match = re.search(r"^\s*package\s+[^;]+;\s*", normalized, flags=re.M)
                if package_match:
                    insertion_point = package_match.end()
                    normalized = normalized[:insertion_point] + "\nimport java.time.Duration;" + normalized[insertion_point:]
                else:
                    import_match = re.search(r"^\s*import\s+.+?;\s*", normalized, flags=re.M)
                    if import_match:
                        insertion_point = import_match.end()
                        normalized = normalized[:insertion_point] + "\nimport java.time.Duration;" + normalized[insertion_point:]
                    else:
                        normalized = "import java.time.Duration;\n" + normalized
        return normalized + "\n"

    def build_command(self, file_path: str, language: str | None = None, temp_dir: str | None = None) -> list[str]:
        java_executable = discover_java_executable() or "java"
        classpath = build_java_classpath(temp_dir or "")
        class_name = Path(file_path).stem
        return [java_executable, "-cp", classpath, "GeniaJUnitRunner", class_name]

    def _run_process(
        self,
        command: list[str],
        cwd: str,
        execution_log_lines: list[str],
        log_callback=None,
        visualize_execution: bool = False,
    ) -> tuple[int, str, str]:
        process = self._safe_process(command, cwd=cwd, visualize_execution=visualize_execution)
        stdout_text, stderr_text = self._stream_process(process, execution_log_lines, log_callback)
        return_code = process.wait(timeout=self.timeout_seconds)
        return return_code, stdout_text, stderr_text

    def _run_quiet_process(self, command: list[str], cwd: str) -> tuple[int, str, str]:
        process = self._safe_process(command, cwd=cwd)
        stdout_text, stderr_text = process.communicate(timeout=self.timeout_seconds)
        return process.returncode or 0, stdout_text or "", stderr_text or ""

    def execute(
        self,
        framework: str,
        script: str,
        language: str | None = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        visualize_execution: bool = False,
    ) -> ExecutionResult:
        start = time.time()
        execution_log_lines: list[str] = []
        temp_dir = tempfile.mkdtemp(prefix="genia-exec-")

        try:
            resolved_language = (language or self.default_language or "").lower()
            if resolved_language != "java":
                return self._error_result(
                    framework=framework,
                    start=start,
                    execution_log_lines=execution_log_lines,
                    message=f"Unsupported language for JUnit execution: {language}",
                )

            normalized_script = self.normalize_script(script, resolved_language)
            filename = self.determine_filename(framework, normalized_script, resolved_language)
            file_path = self.resolve_script_path(temp_dir, filename)
            file_path = self._write_file(os.path.dirname(file_path), os.path.basename(file_path), normalized_script)
            self.prepare_workspace(temp_dir, file_path, normalized_script, resolved_language)

            package_name = extract_java_package_name(normalized_script)
            class_name = extract_java_primary_class_name(normalized_script) or Path(file_path).stem
            qualified_class_name = f"{package_name}.{class_name}" if package_name else class_name

            runner_source = build_junit_runner_source()
            runner_path = self._write_file(temp_dir, "GeniaJUnitRunner.java", runner_source)
            output_dir = Path(temp_dir) / "out"
            output_dir.mkdir(parents=True, exist_ok=True)

            javac_executable = discover_javac_executable() or "javac"
            java_executable = discover_java_executable() or "java"
            chromedriver = discover_chromedriver_executable()
            classpath = build_java_classpath(str(output_dir))
            dependencies_ok, missing_dependencies = validate_java_dependencies()
            classpath_entries = classpath.split(os.pathsep) if classpath else []

            execution_log_lines.append(f"[SETUP] Classpath entries detected: {len(classpath_entries)}")
            execution_log_lines.append(f"[SETUP] Java dependencies validation: {'OK' if dependencies_ok else 'MISSING ' + ', '.join(missing_dependencies)}")
            compile_command = [
                javac_executable,
                "-encoding",
                "UTF-8",
                "-cp",
                classpath,
                "-d",
                str(output_dir),
                file_path,
                runner_path,
            ]

            execution_log_lines.append(f"[SETUP] Workspace: {temp_dir}")
            execution_log_lines.append(f"[SETUP] Script file: {file_path}")
            execution_log_lines.append(f"[SETUP] Runner file: {runner_path}")
            execution_log_lines.append(f"[SETUP] Output dir: {output_dir}")
            execution_log_lines.append(f"[EXECUTION] Compile step prepared using {javac_executable}")
            self._emit(log_callback, "info", f"Script preparado em {file_path}")
            if not dependencies_ok:
                message = f"Missing required Java dependencies: {', '.join(missing_dependencies)}"
                self._emit(log_callback, "error", message)
                return self._error_result(
                    framework=framework,
                    start=start,
                    execution_log_lines=execution_log_lines,
                    message=message,
                )
            self._emit(log_callback, "info", "Compilando Java localmente")

            compile_rc, compile_stdout, compile_stderr = self._run_quiet_process(compile_command, temp_dir)
            if compile_rc != 0:
                logger.warning("JUnit Java compilation failed for %s: %s", file_path, compile_stderr)
                execution_log_lines.append("[ERROR] Java compilation failed. Full compiler output kept on backend only.")
                return self._result(
                    framework=framework,
                    language=resolved_language,
                    start=start,
                    return_code=compile_rc,
                    stdout_text="",
                    stderr_text="Java compilation failed. Check backend logs for details.",
                    execution_log_lines=execution_log_lines,
                    artifacts=self._collect_artifacts(temp_dir),
                    command=compile_command,
                )

            runtime_cp = build_java_classpath(str(output_dir))
            run_command = [java_executable]
            if chromedriver:
                run_command.append(f"-Dwebdriver.chrome.driver={chromedriver}")
            run_command.extend(["-cp", runtime_cp, "GeniaJUnitRunner", qualified_class_name])

            execution_log_lines.append(f"[EXECUTION] Run step prepared using {java_executable}")
            self._emit(log_callback, "info", "Executando Java localmente")

            run_rc, run_stdout, run_stderr = self._run_process(
                run_command,
                temp_dir,
                execution_log_lines,
                log_callback,
                visualize_execution,
            )
            artifacts = self._collect_artifacts(temp_dir)

            combined_stdout = "\n".join(part for part in [compile_stdout, run_stdout] if part)
            combined_stderr = "\n".join(part for part in [compile_stderr, run_stderr] if part)

            self._emit(
                log_callback,
                "success" if run_rc == 0 else "error",
                f"Execução finalizada com status {'PASSED' if run_rc == 0 else 'FAILED'}",
            )
            return self._result(
                framework=framework,
                language=resolved_language,
                start=start,
                return_code=run_rc,
                stdout_text=combined_stdout,
                stderr_text=combined_stderr,
                execution_log_lines=execution_log_lines,
                artifacts=artifacts,
                command=run_command,
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


__all__ = ["JUnitJavaExecutionStrategy"]
