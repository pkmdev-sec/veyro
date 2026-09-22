from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MAX_REQUEST_BYTES = 64 * 1024


def _arguments(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _ollama_messages(messages: list[dict]) -> list[dict]:
    names: dict[str, str] = {}
    converted = []
    for message in messages:
        item = {key: message[key] for key in ("role", "content") if key in message}
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            item["tool_calls"] = []
            for call in calls:
                function = call.get("function", {})
                name = function.get("name")
                identifier = call.get("id")
                if isinstance(identifier, str) and isinstance(name, str):
                    names[identifier] = name
                item["tool_calls"].append(
                    {
                        "function": {
                            "name": name,
                            "arguments": _arguments(function.get("arguments", {})),
                        }
                    }
                )
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id in names:
            item["tool_name"] = names[call_id]
        converted.append(item)
    return converted


def _ollama_request(payload: dict, *, context_size: int | None = None) -> dict:
    options = {}
    option_names = {
        "temperature": "temperature",
        "top_p": "top_p",
        "frequency_penalty": "frequency_penalty",
        "presence_penalty": "presence_penalty",
        "seed": "seed",
        "max_tokens": "num_predict",
    }
    for source, target in option_names.items():
        value = payload.get(source)
        if value is not None:
            options[target] = value
    if context_size is not None:
        options["num_ctx"] = context_size
    request = {
        "model": payload["model"],
        "messages": _ollama_messages(payload.get("messages", [])),
        "stream": False,
        "options": options,
    }
    if isinstance(payload.get("tools"), list):
        request["tools"] = payload["tools"]
    return request


def _openai_message(message: dict) -> dict:
    converted = {"role": "assistant", "content": message.get("content") or ""}
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        converted["tool_calls"] = []
        for index, call in enumerate(calls):
            function = call.get("function", {})
            converted["tool_calls"].append(
                {
                    "index": index,
                    "id": call.get("id") or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": function.get("name"),
                        "arguments": json.dumps(
                            function.get("arguments", {}), separators=(",", ":")
                        ),
                    },
                }
            )
    return converted


def _openai_response(payload: dict, result: dict) -> dict:
    message = _openai_message(result.get("message", {}))
    finish = "tool_calls" if message.get("tool_calls") else result.get("done_reason", "stop")
    return {
        "id": "chatcmpl-veyro-local",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model", payload["model"]),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": result.get("prompt_eval_count", 0),
            "completion_tokens": result.get("eval_count", 0),
            "total_tokens": result.get("prompt_eval_count", 0) + result.get("eval_count", 0),
        },
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "VeyroOllamaProxy/1"

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_REQUEST_BYTES:
                raise ValueError("serialized model request exceeds 64 KiB context budget")
            payload = json.loads(self.rfile.read(length))
            expected_model = getattr(self.server, "expected_model", None)
            if expected_model is not None and payload.get("model") != expected_model:
                raise ValueError("request model does not match the pinned local model")
            request = Request(
                OLLAMA_CHAT_URL,
                data=json.dumps(
                    _ollama_request(
                        payload,
                        context_size=getattr(self.server, "context_size", None),
                    )
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=120) as response:
                result = json.load(response)
            output = _openai_response(payload, result)
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                chunk = {
                    **output,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": output["choices"][0]["message"],
                            "finish_reason": output["choices"][0]["finish_reason"],
                        }
                    ],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
                return
            body = json.dumps(output).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (KeyError, TypeError, ValueError, HTTPError, URLError, TimeoutError) as error:
            body = json.dumps(
                {"error": {"message": str(error), "type": "local_proxy_error"}}
            ).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def start(
    expected_model: str | None = None, *, context_size: int | None = None
) -> tuple[ThreadingHTTPServer, str]:
    if context_size is not None and (
        type(context_size) is not int or not 512 <= context_size <= 32768
    ):
        raise ValueError("context_size must be an integer from 512 to 32768")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.expected_model = expected_model  # type: ignore[attr-defined]
    server.context_size = context_size  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1"
