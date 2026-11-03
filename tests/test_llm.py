import json
import sys
import unittest
from pathlib import Path

import httpx
from jsonschema import ValidationError
from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.llm import PROBE_TOOL, LLMClient, Tool
from spk_platform.settings import LLM


def chat(message, finish="stop"):
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


def response(output):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "test",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": False,
        "tools": [],
    }


class LLMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.replies = []

        async def handler(request):
            self.requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=self.replies.pop(0))

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.sdk = AsyncOpenAI(
            api_key="fixture",
            base_url="https://test.invalid/prefix/v1/",
            http_client=self.http,
            max_retries=0,
        )
        self.calls = []

    async def asyncTearDown(self):
        await self.sdk.close()

    async def execute(self, name, arguments, identifier):
        self.calls.append((name, arguments, identifier))
        return {"value": arguments["value"]}

    def client(self, protocol):
        return LLMClient(
            LLM(endpoint="https://test.invalid/prefix/v1", model="test", protocol=protocol),
            "fixture",
            client=self.sdk,
        )

    async def test_chat_tool_roundtrip_preserves_prefix_and_call_id(self):
        self.replies = [
            chat(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "probe_echo", "arguments": '{"value":7}'},
                        }
                    ],
                },
                "tool_calls",
            ),
            chat({"role": "assistant", "content": "7"}),
        ]
        result = await self.client("chat").run("test", [PROBE_TOOL], self.execute)
        self.assertEqual(result["text"], "7")
        path, request = self.requests[1]
        self.assertEqual(path, "/prefix/v1/chat/completions")
        self.assertEqual(request["messages"][-1]["tool_call_id"], "call_1")
        self.assertEqual(len(self.calls), 1)

    async def test_responses_preserves_opaque_reasoning(self):
        self.replies = [
            response(
                [
                    {
                        "id": "reasoning1",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "opaque",
                    },
                    {
                        "id": "fc1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "probe_echo",
                        "arguments": '{"value":7}',
                    },
                ]
            ),
            response(
                [
                    {
                        "id": "msg1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "7", "annotations": []}],
                    }
                ]
            ),
        ]
        result = await self.client("responses").run("test", [PROBE_TOOL], self.execute)
        self.assertEqual(result["text"], "7")
        path, request = self.requests[1]
        self.assertEqual(path, "/prefix/v1/responses")
        self.assertFalse(request["store"])
        self.assertEqual(request["input"][1]["encrypted_content"], "opaque")
        self.assertEqual(request["input"][-1]["call_id"], "call_1")
        self.assertIn("instructions", request)

    async def test_invalid_tool_parameters_do_not_execute(self):
        self.replies = [
            chat(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "probe_echo", "arguments": '{"value":"bad"}'},
                        }
                    ],
                },
                "tool_calls",
            )
        ]
        with self.assertRaises(ValidationError):
            await self.client("chat").run("test", [PROBE_TOOL], self.execute)
        self.assertEqual(self.calls, [])

    async def test_repeated_call_id_is_not_executed_twice(self):
        tool_response = chat(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "probe_echo", "arguments": '{"value":7}'},
                    }
                ],
            },
            "tool_calls",
        )
        self.replies = [
            tool_response,
            tool_response,
            chat({"role": "assistant", "content": "done"}),
        ]
        await self.client("chat").run("test", [PROBE_TOOL], self.execute)
        self.assertEqual(len(self.calls), 1)

    def test_remote_schema_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            Tool("unsafe", "Unsafe schema", {"$ref": "http://169.254.169.254/schema"})

    async def test_models_discovery_does_not_require_selected_model(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/prefix/v1/models")
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "available-model",
                            "object": "model",
                            "created": 0,
                            "owned_by": "fixture",
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sdk = AsyncOpenAI(
                api_key="fixture",
                base_url="https://test.invalid/prefix/v1/",
                http_client=http,
                max_retries=0,
            )
            client = LLMClient(
                LLM(endpoint="https://test.invalid/prefix/v1/"), "fixture", client=sdk
            )
            self.assertEqual(await client.models(), ["available-model"])
            with self.assertRaises(ValueError):
                await client.run("not billable without a selected model")

    async def test_streaming_chat_assembles_tool_arguments_before_execution(self):
        def chunk(delta, finish=None):
            return {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        replies = [
            [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_stream",
                                "type": "function",
                                "function": {"name": "probe_echo", "arguments": '{"value":'},
                            }
                        ]
                    }
                ),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": "7}"}}]}),
                chunk({}, "tool_calls"),
            ],
            [chunk({"content": "7"}), chunk({}, "stop")],
        ]

        async def handler(request):
            self.requests.append((request.url.path, json.loads(request.content)))
            text = (
                "".join("data: %s\n\n" % json.dumps(event) for event in replies.pop(0))
                + "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sdk = AsyncOpenAI(
                api_key="fixture",
                base_url="https://test.invalid/v1",
                http_client=http,
                max_retries=0,
            )
            client = LLMClient(
                LLM(endpoint="https://test.invalid/v1", model="test"), "fixture", client=sdk
            )
            result = await client.run("test", [PROBE_TOOL], self.execute, streaming=True)
        self.assertEqual(result["text"], "7")
        self.assertEqual(self.calls, [("probe_echo", {"value": 7}, "call_stream")])

    async def test_responses_http_200_stream_error_is_not_success(self):
        async def handler(request):
            text = (
                "data: "
                + json.dumps(
                    {"type": "response.failed", "response": {"id": "failed", "status": "failed"}}
                )
                + "\n\n"
            )
            return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sdk = AsyncOpenAI(
                api_key="fixture",
                base_url="https://test.invalid/v1",
                http_client=http,
                max_retries=0,
            )
            client = LLMClient(
                LLM(endpoint="https://test.invalid/v1", model="test", protocol="responses"),
                "fixture",
                client=sdk,
            )
            with self.assertRaises(ValueError):
                await client.run("test", streaming=True)

    async def test_truncated_result_is_not_success(self):
        self.replies = [chat({"role": "assistant", "content": "partial"}, "length")]
        with self.assertRaises(ValueError):
            await self.client("chat").run("test")


if __name__ == "__main__":
    unittest.main()
