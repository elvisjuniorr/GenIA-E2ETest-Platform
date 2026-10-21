from __future__ import annotations

import unittest
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

from backend.infrastructure.TestExecutor import TestExecutor
from backend.infrastructure.frameworks_languages import ExecutionStrategyRegistry
from backend.infrastructure.frameworks_languages.robotframework import RobotFrameworkPythonExecutionStrategy
from backend.domain.models import ExecutionResult
from backend.pipelines.stages.finalization import FinalizationStage
from backend.pipelines.stages.homologation import HomologationStage
from backend.pipelines.stages.refactoring import RefactoringStage


class ExecutionStrategyRegistryTest(unittest.TestCase):
    def test_resolves_expected_strategies_for_each_supported_combination(self) -> None:
        registry = ExecutionStrategyRegistry()
        cases = {
            ("robotframework", "python"): "RobotFrameworkPythonExecutionStrategy",
            ("playwright", "javascript"): "PlaywrightJavaScriptExecutionStrategy",
            ("playwright", "typescript"): "PlaywrightTypeScriptExecutionStrategy",
            ("cypress", "javascript"): "CypressJavaScriptExecutionStrategy",
            ("cypress", "typescript"): "CypressTypeScriptExecutionStrategy",
            ("pytest", "python"): "PytestPythonExecutionStrategy",
            ("junit", "java"): "JUnitJavaExecutionStrategy",
            ("selenium", "python"): "SeleniumPythonExecutionStrategy",
            ("selenium", "javascript"): "SeleniumJavaScriptExecutionStrategy",
            ("selenium", "typescript"): "SeleniumTypeScriptExecutionStrategy",
            ("selenium", "java"): "SeleniumJavaExecutionStrategy",
        }

        for (framework, language), expected_class_name in cases.items():
            with self.subTest(framework=framework, language=language):
                strategy = registry.resolve(framework, language)
                self.assertEqual(strategy.__class__.__name__, expected_class_name)

    def test_unknown_framework_falls_back_to_generic_python(self) -> None:
        registry = ExecutionStrategyRegistry()
        strategy = registry.resolve("unknown", "python")
        self.assertEqual(strategy.__class__.__name__, "GenericPythonExecutionStrategy")


class TestExecutorDispatchTest(unittest.TestCase):
    def test_execute_delegates_to_resolved_strategy(self) -> None:
        class DummyStrategy:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str | None]] = []

            def execute(
                self,
                framework: str,
                script: str,
                language: str | None = None,
                log_callback=None,
                visualize_execution: bool = False,
            ):
                self.calls.append((framework, script, language))
                return {"framework": framework, "script": script, "language": language}

        class DummyRegistry:
            def __init__(self, strategy) -> None:
                self.strategy = strategy

            def resolve(self, framework: str, language: str | None = None, script: str | None = None):
                return self.strategy

        dummy_strategy = DummyStrategy()
        executor = TestExecutor(registry=DummyRegistry(dummy_strategy))
        result = executor.execute("playwright", "console.log('ok')", language="javascript")

        self.assertEqual(dummy_strategy.calls, [("playwright", "console.log('ok')", "javascript")])
        self.assertEqual(result["framework"], "playwright")


class RobotHeadlessNormalizationTest(unittest.TestCase):
    def test_chrome_open_browser_is_forced_headless(self) -> None:
        script = "*** Test Cases ***\nSmoke\n    Open Browser    https://example.test    chrome"

        normalized = RobotFrameworkPythonExecutionStrategy().normalize_script(script, "python")

        self.assertIn("options=${GENIA_CHROME_OPTIONS}", normalized)

    def test_existing_chrome_options_receive_headless_as_a_positional_python_argument(self) -> None:
        script = "*** Test Cases ***\nSmoke\n    Open Browser    https://example.test    chrome    options=${chrome_options}"

        normalized = RobotFrameworkPythonExecutionStrategy().normalize_script(script, "python")

        self.assertIn(r"Call Method    ${chrome_options}    add_argument    --headless\=new", normalized)
        self.assertIn("options=${chrome_options}", normalized)

    def test_generated_chrome_options_are_not_mutated_twice(self) -> None:
        script = "*** Test Cases ***\nSmoke\n    Open Browser    https://example.test    chrome    options=${GENIA_CHROME_OPTIONS}"

        normalized = RobotFrameworkPythonExecutionStrategy().normalize_script(script, "python")

        self.assertIn("options=${GENIA_CHROME_OPTIONS}", normalized)
        self.assertNotIn("Call Method", normalized)
        self.assertNotIn("Evaluate", normalized)

    def test_legacy_headless_call_is_migrated_away_from_robot_named_arguments(self) -> None:
        script = (
            "*** Test Cases ***\nSmoke\n"
            "    Call Method    ${GENIA_CHROME_OPTIONS}    add_argument    --headless=new\n"
            "    Open Browser    https://example.test    chrome    options=${GENIA_CHROME_OPTIONS}"
        )

        normalized = RobotFrameworkPythonExecutionStrategy().normalize_script(script, "python")

        self.assertNotIn("Call Method    ${GENIA_CHROME_OPTIONS}    add_argument", normalized)

    def test_robot_listener_closes_browsers_when_suite_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            listener_path = RobotFrameworkPythonExecutionStrategy()._write_robot_listener(temp_dir)
            listener_source = Path(listener_path).read_text(encoding="utf-8")

        self.assertIn("def end_suite(data, result):", listener_source)
        self.assertIn("def end_test(data, result):", listener_source)
        self.assertIn("_close_all_browsers()", listener_source)
        self.assertIn("library.close_all_browsers()", listener_source)

    def test_robot_variable_single_space_is_repaired_and_recorded(self) -> None:
        strategy = RobotFrameworkPythonExecutionStrategy()
        script = "*** Variables ***\n${INVALID_PASSWORD} invalid_password\n\n*** Test Cases ***\nSmoke\n    Log    ${INVALID_PASSWORD}"

        prepared, changes = strategy.prepare_script(script, "python")

        self.assertIn("${INVALID_PASSWORD}    invalid_password", prepared)
        self.assertEqual(changes[0]["line"], 2)
        self.assertEqual(changes[0]["type"], "robot_variable_separator")

    def test_robot_preflight_rejects_variable_names_with_spaces(self) -> None:
        from backend.infrastructure.frameworks_languages.base import ExecutionPreflightError

        script = "*** Variables ***\n${PASSWORD INPUT}    secret\n"
        with self.assertRaisesRegex(ExecutionPreflightError, "linha 2"):
            RobotFrameworkPythonExecutionStrategy().prepare_script(script, "python")

    @patch("backend.infrastructure.frameworks_languages.robotframework.subprocess.run")
    def test_robot_dryrun_failure_returns_before_browser_execution(self, run):
        run.return_value = CompletedProcess([], 1, stdout="", stderr="Variable not found")
        strategy = RobotFrameworkPythonExecutionStrategy()
        script = "*** Test Cases ***\nSmoke\n    Log    ${MISSING}"

        with patch.object(strategy, "_safe_process") as start_browser_runner:
            result = strategy.execute("robotframework", script, "python")

        start_browser_runner.assert_not_called()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.test_results["execution_phase"], "script_validation")
        self.assertIn("Variable not found", result.stderr)


class ExecutionReportIntegrityTest(unittest.TestCase):
    def test_homologation_does_not_claim_ui_execution_after_robot_preflight_failure(self) -> None:
        execution = ExecutionResult(
            status="error",
            duration=0.2,
            stderr="Robot Framework: separador inválido na linha 4.",
            test_results={"execution_phase": "script_validation"},
        )
        report = {"homologation_status": "PASSED", "execution_analysis": "The browser ran."}

        result = HomologationStage._enforce_execution_facts(report, execution, "script")

        self.assertEqual(result["homologation_status"], "FAILED")
        self.assertIn("Nenhuma interação", result["execution_analysis"])
        self.assertIn("não chegou à interação", result["broken_elements"])

    def test_homologation_rejects_script_provenance_mismatch(self) -> None:
        execution = ExecutionResult(
            status="passed",
            duration=1,
            test_results={"source_script_sha256": "different"},
        )

        result = HomologationStage._enforce_execution_facts({"homologation_status": "PASSED"}, execution, "script")

        self.assertEqual(result["homologation_status"], "FAILED")
        self.assertIn("hash", result["probable_cause"])

    def test_finalization_uses_executor_failure_as_authoritative(self) -> None:
        report = {
            "pipeline_status": "PASSED",
            "final_result": {"test_passed": "true", "automation_reliability": "HIGH"},
        }

        result = FinalizationStage._enforce_execution_outcome(
            report,
            {"execution": {"status": "error", "stderr": "syntax error", "test_results": {"execution_phase": "script_validation"}}},
        )

        self.assertEqual(result["pipeline_status"], "FAILED")
        self.assertEqual(result["final_result"]["test_passed"], "false")
        self.assertEqual(result["final_result"]["business_validation_status"], "NOT_VALIDATED")


class RefactoringScriptValidationTest(unittest.TestCase):
    class FakePromptManager:
        def load_prompt(self, name):
            return "Refactor {framework}/{language}: {confirmed_script}"

    class FakeLLM:
        def __init__(self, responses):
            self.responses = list(responses)
            self.prompts = []

        def generate_json(self, api_key, model, prompt, temperature=0.0):
            self.prompts.append(prompt)
            return self.responses.pop(0)

    @staticmethod
    def _report(script):
        return {
            "refactored_script": script,
            "diff": "Refactor",
            "justification": "Keep the flow",
            "possible_errors": "None",
            "implemented_improvements": "None",
            "suggested_follow_up": "None",
        }

    def test_invalid_robot_refactor_is_repaired_before_it_is_returned(self):
        valid_script = "*** Test Cases ***\nSmoke\n    Log    valid"
        invalid_script = "*** Variables ***\n${PASSWORD INPUT}    secret\n*** Test Cases ***\nSmoke\n    Log    ${PASSWORD INPUT}"
        llm = self.FakeLLM([self._report(invalid_script), self._report(valid_script)])
        stage = RefactoringStage(llm, self.FakePromptManager())
        execution = ExecutionResult(
            status="passed", duration=1, framework="robotframework",
            test_results={"language": "python"},
        )

        result = asyncio.run(stage.execute(
            valid_script,
            execution,
            {},
            {"script": {"confirmed": valid_script}},
            "key",
            "gpt-test",
            "openai",
            framework="robotframework",
            language="python",
        ))

        self.assertEqual(result["script_validation"]["status"], "passed")
        self.assertEqual(result["refactored_script"], valid_script)
        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("nome de variável inválido", llm.prompts[1])

    def test_unrepairable_robot_refactor_falls_back_to_valid_confirmed_script(self):
        valid_script = "*** Test Cases ***\nSmoke\n    Log    valid"
        invalid_script = "*** Variables ***\n${BAD NAME}    secret\n*** Test Cases ***\nSmoke\n    Log    ${BAD NAME}"
        llm = self.FakeLLM([self._report(invalid_script)] * 3)
        stage = RefactoringStage(llm, self.FakePromptManager())
        execution = ExecutionResult(
            status="passed", duration=1, framework="robotframework",
            test_results={"language": "python"},
        )

        result = asyncio.run(stage.execute(
            valid_script,
            execution,
            {},
            {"script": {"confirmed": valid_script}},
            "key",
            "gpt-test",
            "openai",
            framework="robotframework",
            language="python",
        ))

        self.assertEqual(result["script_validation"]["status"], "fallback")
        self.assertEqual(result["refactored_script"], valid_script)
        self.assertIn("não foi aceita", result["script_validation"]["message"])


if __name__ == "__main__":
    unittest.main()
