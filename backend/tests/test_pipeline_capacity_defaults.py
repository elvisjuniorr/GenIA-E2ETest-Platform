from backend.http.app_factory import create_app


def test_pipeline_capacity_defaults_to_ten_workers_and_two_heavy_stages(monkeypatch):
    for variable in ("PIPELINE_MAX_CONCURRENT", "PIPELINE_MAX_WAITING", "HEAVY_STAGE_CONCURRENCY"):
        monkeypatch.delenv(variable, raising=False)

    app, _, _ = create_app()

    assert app.config["PIPELINE_MAX_CONCURRENT"] == 10
    assert app.config["PIPELINE_MAX_WAITING"] == 10
    assert app.config["HEAVY_STAGE_CONCURRENCY"] == 2
