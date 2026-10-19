"""Robot Framework execution strategy."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from .base import BaseExecutionStrategy


class RobotFrameworkPythonExecutionStrategy(BaseExecutionStrategy):
    framework_name = "robotframework"
    supported_languages = ("python",)
    default_language = "python"

    def determine_filename(self, framework: str, script: str, language: str | None = None) -> str:
        return f"test_{uuid.uuid4().hex}.robot"

    def normalize_script(self, script: str, language: str | None = None) -> str:
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
        return self._ensure_headless_chrome(normalized)

    @staticmethod
    def _ensure_headless_chrome(script: str) -> str:
        if "--headless" in script.lower():
            return script

        lines: list[str] = []
        open_browser = re.compile(r"^(?P<indent>[ \t]*)Open Browser(?P<sep>[ \t]{2,})(?P<args>.*)$", re.IGNORECASE)
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
                    lines.append(
                        f"{match.group('indent')}Call Method    {options_variable}    add_argument    --headless=new"
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
            "GENIA_CHROME_OPTIONS.add_argument('--headless=new')\n",
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
