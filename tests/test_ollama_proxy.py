from __future__ import annotations

import json

from veyro.ollama_proxy import _ollama_request, _openai_response
from veyro.opencode_autonomy import _proxy_environment


def test_openai_tool_history_is_converted_for_native_ollama():
    payload = {
        "model": "qwen3-coder:30b",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": "Read it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp/file"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
        ],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }

    converted = _ollama_request(payload)

    assert converted["stream"] is False
    assert converted["options"]["num_predict"] == 128
    assert converted["messages"][1]["tool_calls"][0]["function"]["arguments"] == {
        "path": "/tmp/file"
    }
    assert converted["messages"][2]["tool_name"] == "read_file"


def test_native_ollama_tool_call_becomes_openai_tool_call():
    payload = {"model": "qwen3-coder:30b"}
    result = {
        "model": "qwen3-coder:30b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "native-id",
                    "function": {"name": "write", "arguments": {"path": "app.py"}},
                }
            ],
        },
        "done_reason": "stop",
        "prompt_eval_count": 10,
        "eval_count": 5,
    }

    response = _openai_response(payload, result)

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [
        {
            "index": 0,
            "id": "native-id",
            "type": "function",
            "function": {"name": "write", "arguments": '{"path":"app.py"}'},
        }
    ]
    assert response["usage"]["total_tokens"] == 15


def test_explicit_seed_is_preserved():
    converted = _ollama_request({"model": "qwen3-coder:30b", "seed": 42})

    assert converted["options"]["seed"] == 42


def test_native_context_limit_is_forwarded_to_ollama():
    converted = _ollama_request(
        {"model": "qwen3-coder:30b"}, context_size=16384
    )

    assert converted["options"]["num_ctx"] == 16384


def test_opencode_proxy_uses_the_advertised_model_context(monkeypatch):
    captured = {}
    server = object()

    def fake_start(expected_model, *, context_size):
        captured.update(
            {"expected_model": expected_model, "context_size": context_size}
        )
        return server, "http://127.0.0.1:12345/v1"

    monkeypatch.setenv("VEYRO_OLLAMA_NATIVE_PROXY", "1")
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps(
            {
                "provider": {
                    "veyro-local": {
                        "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                        "models": {
                            "qwen3-coder:30b": {
                                "limit": {"context": 16384, "output": 1024}
                            }
                        },
                    }
                }
            }
        ),
    )
    monkeypatch.setattr("veyro.ollama_proxy.start", fake_start)

    actual_server, environment = _proxy_environment()

    assert actual_server is server
    assert captured == {
        "expected_model": "qwen3-coder:30b",
        "context_size": 16384,
    }
    config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    assert config["provider"]["veyro-local"]["options"]["baseURL"] == (
        "http://127.0.0.1:12345/v1"
    )
