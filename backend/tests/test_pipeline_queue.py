import threading
import time

from backend.http.app_factory import create_app


def test_pipeline_queue_serializes_jobs_and_keeps_llm_config_isolated(monkeypatch):
    monkeypatch.setenv("PIPELINE_MAX_CONCURRENT", "1")
    monkeypatch.setenv("PIPELINE_MAX_WAITING", "2")
    app, _, orchestrator = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    first_started = threading.Event()
    release_jobs = threading.Event()
    observed_configs = []

    async def fake_pipeline(**kwargs):
        first_started.set()
        assert release_jobs.wait(timeout=5)
        observed_configs.append((orchestrator.current_model, orchestrator.current_api_key))
        return {"status": "waiting_validation", "pipeline_id": kwargs["pipeline_id"]}

    monkeypatch.setattr(orchestrator, "run_full_pipeline_1", fake_pipeline)

    def request(pipeline_id, model):
        return client.post(
            "/api/pipeline/run-until-validation",
            json={
                "test_case": "Test case",
                "framework": "robotframework",
                "language": "python",
                "provider": "openai",
                "model": model,
                "api_key": f"test-key-{pipeline_id}",
                "pipeline_id": pipeline_id,
            },
        )

    first = request("pipeline-a", "gpt-5-mini")
    assert first.status_code == 202
    assert first_started.wait(timeout=2)
    second = request("pipeline-b", "gpt-5-nano")
    assert second.status_code == 202
    third = request("pipeline-c", "gpt-5-mini")
    assert third.status_code == 202
    rejected = request("pipeline-d", "gpt-5-nano")
    assert rejected.status_code == 429

    release_jobs.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        states = [client.get(f"/api/pipeline/status/{pipeline_id}").json["status"] for pipeline_id in ("pipeline-a", "pipeline-b", "pipeline-c")]
        if states == ["WAITING_VALIDATION"] * 3:
            break
        time.sleep(0.02)

    assert states == ["WAITING_VALIDATION"] * 3
    assert observed_configs == [
        ("gpt-5-mini", "test-key-pipeline-a"),
        ("gpt-5-nano", "test-key-pipeline-b"),
        ("gpt-5-mini", "test-key-pipeline-c"),
    ]


def test_completed_pipeline_can_resume_from_confirmation_with_client_llm_config_fallback(monkeypatch):
    app, _, orchestrator = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    async def phase_one(**kwargs):
        return {"status": "waiting_validation", "pipeline_id": kwargs["pipeline_id"]}

    async def phase_two(**kwargs):
        assert kwargs["resume_from_stage"] == "confirmation"
        assert kwargs["script_override"] == "*** Test Cases ***\nRefactored test"
        recovered_session = orchestrator._get_session("refactored-script-pipeline")
        assert recovered_session["data"]["refined"] == {"modules": []}
        assert recovered_session["data"]["framework"] == "robotframework"
        assert orchestrator.current_provider == "openai"
        assert orchestrator.current_model == "gpt-5-nano"
        assert orchestrator.current_api_key == "test-api-key"
        return {"status": "completed", "pipeline_id": kwargs["pipeline_id"]}

    monkeypatch.setattr(orchestrator, "run_full_pipeline_1", phase_one)
    monkeypatch.setattr(orchestrator, "run_full_pipeline_2", phase_two)

    started = client.post(
        "/api/pipeline/run-until-validation",
        json={
            "test_case": "Test case",
            "framework": "robotframework",
            "language": "python",
            "provider": "openai",
            "model": "gpt-5-nano",
            "api_key": "test-api-key",
            "pipeline_id": "refactored-script-pipeline",
        },
    )
    assert started.status_code == 202

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get("/api/pipeline/status/refactored-script-pipeline").json["status"]
        if status == "WAITING_VALIDATION":
            break
        time.sleep(0.02)
    assert status == "WAITING_VALIDATION"
    # Simulate a lost process-local session after the first continuation completed.
    orchestrator.pipeline_sessions.pop("refactored-script-pipeline")

    continued = client.post(
        "/api/pipeline/continue-after-validation",
        json={
            "pipeline_id": "refactored-script-pipeline",
            "resume_from_stage": "confirmation",
            "script_override": "*** Test Cases ***\nRefactored test",
            "llm_config": {
                "provider": "openai",
                "api_key": "test-api-key",
                "model": "gpt-5-nano",
                "temperature": 0,
            },
            "session_context": {
                "initial_input": "Test case",
                "refined": {"modules": []},
                "framework": "robotframework",
                "language": "python",
                "validation": {},
                "script": "Original test",
            },
        },
    )
    assert continued.status_code == 202

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get("/api/pipeline/status/refactored-script-pipeline").json["status"]
        if status == "COMPLETED":
            break
        time.sleep(0.02)
    assert status == "COMPLETED"
    assert "llm_config" not in orchestrator._get_session("refactored-script-pipeline")["data"]
