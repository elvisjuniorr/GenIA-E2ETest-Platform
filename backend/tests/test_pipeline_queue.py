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
