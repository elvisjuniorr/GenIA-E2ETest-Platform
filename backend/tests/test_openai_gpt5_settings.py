from backend.llms import openai as openai_module


def test_gpt5_mini_and_nano_use_low_reasoning_without_temperature(monkeypatch):
    requests = []

    class FakeCompletions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": "ok"})()})()]})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai_module, "OpenAI", FakeOpenAI)
    provider = openai_module.OpenAIProvider()

    for model in ("gpt-5-mini", "gpt-5-nano"):
        assert provider.generate_text("test-key", model, "Return ok", temperature=0.2) == "ok"

    assert [request["model"] for request in requests] == ["gpt-5-mini", "gpt-5-nano"]
    assert all(request["reasoning_effort"] == "low" for request in requests)
    assert all("temperature" not in request for request in requests)


def test_generate_json_requests_json_mode(monkeypatch):
    requests = []

    class FakeCompletions:
        def create(self, **kwargs):
            requests.append(kwargs)
            message = type("Message", (), {"content": '{"homologation_status":"PASSED"}'})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai_module, "OpenAI", FakeOpenAI)
    provider = openai_module.OpenAIProvider()

    result = provider.generate_json("test-key", "gpt-5-mini", "Return JSON")

    assert result == {"homologation_status": "PASSED"}
    assert requests[0]["response_format"] == {"type": "json_object"}
