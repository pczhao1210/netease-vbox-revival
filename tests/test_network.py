import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from unittest.mock import AsyncMock, patch

import httpcore

from spk_platform.network import GuardedBackend, GuardedTransport, NetworkPolicy


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_timeout_is_inside_connection_deadline(self):
        policy = NetworkPolicy()
        policy.resolve = AsyncMock(side_effect=TimeoutError("fixture"))
        with self.assertRaises(httpcore.ConnectTimeout):
            await GuardedBackend(policy).connect_tcp("example.org", 443, timeout=1)

    async def test_connect_failure_falls_back_without_bypassing_address_checks(self):
        policy = NetworkPolicy()
        policy.resolve = AsyncMock(return_value=["1.1.1.1", "8.8.8.8"])
        backend = GuardedBackend(policy)
        stream = object()
        with patch(
            "httpcore._backends.anyio.AnyIOBackend.connect_tcp",
            new=AsyncMock(side_effect=[httpcore.ConnectError("fixture"), stream]),
        ) as connect:
            self.assertIs(await backend.connect_tcp("example.org", 443, timeout=1), stream)
            self.assertEqual(connect.await_count, 2)

    def test_address_policy(self):
        policy = NetworkPolicy(["gateway.lan"])
        for host, address in (
            ("example.org", "127.0.0.1"),
            ("gateway.lan", "169.254.169.254"),
            ("example.org", "192.168.1.1"),
            ("gateway.lan", "::1"),
            ("gateway.lan", "100.100.100.200"),
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                policy.address(host, address)
        policy.address("gateway.lan", "192.168.1.5")
        policy.address("example.org", "8.8.8.8")
        with self.assertRaises(ValueError):
            policy.url("https://user:password@example.org")

    async def test_real_transport_and_loopback_rejection(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%s/" % server.server_port
            async with httpx.AsyncClient(
                transport=GuardedTransport(NetworkPolicy(allow_loopback=True))
            ) as client:
                self.assertEqual((await client.get(url)).text, "ok")
            async with httpx.AsyncClient(transport=GuardedTransport(NetworkPolicy())) as client:
                with self.assertRaises(ValueError):
                    await client.get(url)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
