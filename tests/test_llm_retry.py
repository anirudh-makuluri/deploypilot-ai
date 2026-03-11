import json

import pytest
from pydantic import BaseModel, ValidationError

from graph.llm_retry import RetryConfig, RetryExhaustedError, invoke_with_retry


class _Payload(BaseModel):
    value: int


def test_invoke_with_retry_succeeds_after_retry_with_validator_failure(monkeypatch):
    calls = {"count": 0}

    class _Resp:
        def __init__(self, content: str):
            self.content = content

    def _invoke(_prompt: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return _Resp("{bad json")
        return _Resp(json.dumps({"value": 7}))

    def _validate(resp):
        data = json.loads(resp.content)
        return _Payload(**data)

    result, attempts, fallback_used = invoke_with_retry(
        invoke_fn=_invoke,
        prompt="main",
        validator=_validate,
        config=RetryConfig(max_attempts=3, backoff_base_seconds=0.001, backoff_max_seconds=0.001),
        node_name="unit",
    )

    assert result.value == 7
    assert attempts == 2
    assert fallback_used is False


def test_invoke_with_retry_uses_fallback_prompt_after_threshold():
    prompts = []

    class _Resp:
        def __init__(self, content: str):
            self.content = content

    def _invoke(prompt: str):
        prompts.append(prompt)
        if len(prompts) < 3:
            raise ValidationError.from_exception_data("x", [])
        return _Resp('{"value": 5}')

    def _validate(resp):
        return _Payload(**json.loads(resp.content))

    result, attempts, fallback_used = invoke_with_retry(
        invoke_fn=_invoke,
        prompt="primary",
        validator=_validate,
        fallback_prompt="fallback",
        config=RetryConfig(
            max_attempts=3,
            backoff_base_seconds=0.001,
            backoff_max_seconds=0.001,
            fallback_after_attempt=2,
        ),
        node_name="unit",
    )

    assert result.value == 5
    assert attempts == 3
    assert fallback_used is True
    assert prompts[0] == "primary"
    assert prompts[1] == "primary"
    assert prompts[2].startswith("primary")
    assert "fallback" in prompts[2]


def test_invoke_with_retry_raises_after_exhaustion():
    def _invoke(_prompt: str):
        raise RuntimeError("transient boom")

    with pytest.raises(RetryExhaustedError):
        invoke_with_retry(
            invoke_fn=_invoke,
            prompt="x",
            config=RetryConfig(max_attempts=2, backoff_base_seconds=0.001, backoff_max_seconds=0.001),
            node_name="unit",
        )
