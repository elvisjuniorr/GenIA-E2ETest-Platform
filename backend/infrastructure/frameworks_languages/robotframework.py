"""Robot Framework execution strategy."""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .base import BaseExecutionStrategy, ExecutionPreflightError


class RobotFrameworkPythonExecutionStrategy(BaseExecutionStrategy):
    framework_name = "robotframework"
    supported_languages = ("python",)
    default_language = "python"
    supports_candidate_validation = True

    def determine_filename(self, framework: str, script: str, language: str | None = None) -> str:
        return f"test_{uuid.uuid4().hex}.robot"

    def normalize_script(self, script: str, language: str | None = None) -> str:
        normalized = script.replace("\r\n", "\n").strip()
        lines = normalized.split("\n")
        if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            normalized = "\n".join(lines[1:-1]).strip("\n")
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
        return self._ensure_headless_chrome(normalized)

    def prepare_script(self, script: str, language: str | None = None) -> tuple[str, list[dict]]:
        """Repair only unambiguous Robot variable-cell separators."""
        del language
        lines = script.split("\n")
        in_variables = False
        transformations: list[dict] = []
        variable_row = re.compile(r"^(?P<indent>[ \t]*)(?P<sigil>\$|@|&)\{(?P<name>[^}]+)\}(?P<tail>.*)$")

        for index, line in enumerate(lines):
            heading = re.match(r"^\s*\*\*\*\s*(?P<section>[^*]+?)\s*\*\*\*", line)
            if heading:
                in_variables = heading.group("section").strip().casefold() == "variables"
                continue
            if not in_variables or not line.strip() or line.lstrip().startswith(("#", "|", "...")):
                continue

            match = variable_row.match(line)
            if not match:
                continue
            name = match.group("name")
            if not name.strip() or re.search(r"\s", name):
                raise ExecutionPreflightError(
                    f"Robot Framework: nome de variável inválido na linha {index + 1}; use um nome sem espaços, por exemplo ${{PASSWORD_INPUT}}."
                )

            tail = match.group("tail")
            if not tail.strip():
                raise ExecutionPreflightError(
                    f"Robot Framework: variável sem valor na linha {index + 1} ({match.group('sigil')}{{{name}}})."
                )

            # Robot's explicit assignment form is `${NAME} =    value`.
            explicit_equals = re.match(r"^[ \t]+=(?P<after>[ \t]*)(?P<value>.*)$", tail)
            if explicit_equals:
                after = explicit_equals.group("after")
                if explicit_equals.group("value") and ("\t" in after or len(after) >= 2):
                    continue
                raise ExecutionPreflightError(
                    f"Robot Framework: separador inválido após '=' na variável da linha {index + 1}; use pelo menos dois espaços ou uma tabulação antes do valor."
                )

            separator = re.match(r"^[ \t]+", tail)
            if not separator:
                raise ExecutionPreflightError(
                    f"Robot Framework: falta separador entre a variável e o valor na linha {index + 1}."
                )
            separator_text = separator.group(0)
            value = tail[len(separator_text):]
            if not value:
                raise ExecutionPreflightError(
                    f"Robot Framework: variável sem valor na linha {index + 1} ({match.group('sigil')}{{{name}}})."
                )
            if separator_text == " ":
                lines[index] = f"{match.group('indent')}{match.group('sigil')}{{{name}}}    {value}"
                transformations.append({
                    "line": index + 1,
                    "type": "robot_variable_separator",
                    "description": "Single-space variable/value separator changed to four spaces.",
                })
            elif "\t" not in separator_text and len(separator_text) < 2:
                raise ExecutionPreflightError(
                    f"Robot Framework: separador inválido na variável da linha {index + 1}; use pelo menos dois espaços ou uma tabulação."
                )

        return "\n".join(lines), transformations

    def validate_prepared_script(self, file_path: str, temp_dir: str, language: str | None = None) -> None:
        """Use Robot's dry-run parser before starting a browser session."""
        del language
        variable_file = str(Path(temp_dir) / "genia_robot_variables.py")
        command = [
            self._python_command(), "-m", "robot", "--dryrun",
            "--output", "NONE", "--log", "NONE", "--report", "NONE",
            "--variablefile", variable_file, file_path,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionPreflightError(
                f"Robot Framework: não foi possível concluir a pré-validação do script: {exc}",
                phase="script_validation",
            ) from exc
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout or "Robot Framework rejeitou o script.").strip()
            raise ExecutionPreflightError(
                "Robot Framework rejeitou o script na pré-validação (--dryrun); o navegador não foi iniciado. "
                + diagnostic[-6000:],
                stdout=completed.stdout[-6000:],
                stderr=completed.stderr[-6000:],
                phase="script_validation",
            )

    @staticmethod
    def _ensure_headless_chrome(script: str) -> str:
        legacy_headless_call = re.compile(
            r"^(?P<indent>[ \t]*)Call Method(?P<sep>[ \t]{2,})(?P<options>\$\{[^}]+\})(?P<sep2>[ \t]{2,})add_argument(?P<sep3>[ \t]{2,})--headless(?:=new)?\s*$",
            re.IGNORECASE,
        )

        def replace_legacy_call(line: str) -> str:
            match = legacy_headless_call.match(line)
            if not match:
                return line
            options_variable = match.group("options")[2:-1]
            if options_variable.casefold() == "genia_chrome_options":
                return ""
            return f'{match.group("indent")}Call Method{match.group("sep")}{match.group("options")}{match.group("sep2")}add_argument{match.group("sep3")}--headless\\=new'

        script = "\n".join(replace_legacy_call(line) for line in script.split("\n"))

        # Robot screenshots capture the browser viewport; a taller viewport
        # shows more of the page in live frames and final evidence.
        open_browser = re.compile(r"^(?P<indent>[ \t]*)Open Browser(?P<sep>[ \t]{2,})(?P<args>.*)$", re.IGNORECASE)
        viewport_lines: list[str] = []
        active_browser = ""
        for line in script.split("\n"):
            match = open_browser.match(line)
            if match:
                args = match.group("args")
                cells = re.split(r"[ \t]{2,}", args.strip()) if args.strip() else []
                browser = next(
                    (cell.split("=", 1)[1] for cell in cells if cell.lower().startswith("browser=")),
                    cells[1] if len(cells) > 1 else "",
                )
                active_browser = browser.strip().strip("'\"{}").lower()
                viewport_lines.append(line)
                if active_browser == "chrome":
                    viewport_lines.append(f"{match.group('indent')}Set Window Size    1440    1200")
                continue

            if re.match(r"^[ \t]*(?:Close Browser|Close All Browsers)(?:[ \t]|$)", line, re.IGNORECASE):
                active_browser = ""
                viewport_lines.append(line)
                continue

            maximize = re.match(r"^(?P<indent>[ \t]*)Maximize Browser Window[ \t]*$", line, re.IGNORECASE)
            if maximize and active_browser == "chrome":
                viewport_lines.append(f"{maximize.group('indent')}Set Window Size    1440    1200")
                continue

            viewport_lines.append(line)

        script = "\n".join(viewport_lines)

        if "--headless" in script.lower():
            return script

        lines: list[str] = []
        option_arg = re.compile(r"(?i)(?:^|[ \t]{2,})(?:options|chrome_options)=(\$\{[^}]+\}|[^ \t]+)")

        for line in script.split("\n"):
            match = open_browser.match(line)
            if not match:
                lines.append(line)
                continue

            args = match.group("args")
            cells = re.split(r"[ \t]{2,}", args.strip()) if args.strip() else []
            browser = next(
                (cell.split("=", 1)[1] for cell in cells if cell.lower().startswith("browser=")),
                cells[1] if len(cells) > 1 else "",
            )
            if browser.strip().strip("'\"{}").lower() != "chrome":
                lines.append(line)
                continue

            configured_options = option_arg.search(args)
            if configured_options:
                options_variable = configured_options.group(1)
                line = re.sub(r"(?i)\bchrome_options=", "options=", line)
                if options_variable.startswith("${") and options_variable.lower() not in {"${none}", "${null}"}:
                    if options_variable.casefold() != "${genia_chrome_options}".casefold():
                        # Robot otherwise parses `--headless=new` as named argument `--headless`.
                        lines.append(
                            f"{match.group('indent')}Call Method    {options_variable}    add_argument    --headless\\=new"
                        )
                else:
                    line = re.sub(
                        r"(?i)(?:chrome_options|options)=\S+",
                        "options=${GENIA_CHROME_OPTIONS}",
                        line,
                    )
                lines.append(line)
                continue

            lines.append(f"{line.rstrip()}    options=${{GENIA_CHROME_OPTIONS}}")

        return "\n".join(lines)

    def prepare_workspace(self, temp_dir: str, file_path: str, script: str, language: str | None = None) -> None:
        variable_file = Path(temp_dir) / "genia_robot_variables.py"
        variable_file.write_text(
            "from selenium.webdriver import ChromeOptions\n"
            "GENIA_CHROME_OPTIONS = ChromeOptions()\n"
            "GENIA_CHROME_OPTIONS.add_argument('--headless=new')\n"
            "GENIA_CHROME_OPTIONS.add_argument('--window-size=1440,1200')\n",
            encoding="utf-8",
        )

    def build_command(self, file_path: str, language: str | None = None, temp_dir: str | None = None) -> list[str]:
        python_cmd = self._python_command()
        listener_path = self._write_robot_listener(temp_dir or ".")
        variable_file = str(Path(temp_dir or ".") / "genia_robot_variables.py")
        robot_command = [
            python_cmd,
            "-m",
            "robot",
            "--listener",
            listener_path,
            "--variablefile",
            variable_file,
            file_path,
        ]
        if self._is_unix_like() and shutil.which("xvfb-run"):
            return ["xvfb-run", "-a", *robot_command]
        return robot_command
