from __future__ import annotations

import unittest

from backend.infrastructure.TestExecutor import TestExecutor
from backend.infrastructure.frameworks_languages import ExecutionStrategyRegistry
from backend.infrastructure.frameworks_languages.robotframework import RobotFrameworkPythonExecutionStrategy


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

    def test_existing_chrome_options_keep_headless_argument(self) -> None:
        script = "*** Test Cases ***\nSmoke\n    Open Browser    https://example.test    chrome    options=${chrome_options}"

        normalized = RobotFrameworkPythonExecutionStrategy().normalize_script(script, "python")

        self.assertIn("Call Method    ${chrome_options}    add_argument    --headless=new", normalized)
        self.assertIn("options=${chrome_options}", normalized)


if __name__ == "__main__":
    unittest.main()
