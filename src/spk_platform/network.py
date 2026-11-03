import asyncio
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend


class NetworkPolicy:
    def __init__(self, private_hosts=(), allow_loopback=False):
        self.private_hosts = {host.lower() for host in private_hosts}
        self.allow_loopback = allow_loopback

    def url(self, value):
        parsed = urlsplit(str(value))
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("Unsafe remote URL")
        if parsed.scheme != "https" and parsed.hostname.lower() not in self.private_hosts:
            music_host = parsed.hostname.lower().endswith((".music.126.net", ".music.163.com"))
            if not self.allow_loopback and not music_host:
                raise ValueError("Remote services require HTTPS")
        return parsed

    def address(self, host, value):
        address = ipaddress.ip_address(value.split("%")[0])
        if address.is_loopback and self.allow_loopback:
            return
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or str(address) in ("100.100.100.200", "168.63.129.16")
        ):
            raise ValueError("Remote address is blocked")
        if not address.is_global and host.lower() not in self.private_hosts:
            raise ValueError("Private network host requires explicit approval")

    async def resolve(self, host, port):
        records = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise ValueError("Endpoint has no usable address")
        for address in addresses:
            self.address(host, address)
        return addresses


class GuardedBackend(AnyIOBackend):
    def __init__(self, policy):
        self.policy = policy

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            async with asyncio.timeout(timeout):
                addresses = await self.policy.resolve(host, port)
                last_error = None
                for address in addresses:
                    try:
                        return await super().connect_tcp(
                            address, port, timeout, local_address, socket_options
                        )
                    except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                        last_error = error
                raise httpcore.ConnectError(
                    "Unable to connect to approved endpoint"
                ) from last_error
        except TimeoutError as error:
            raise httpcore.ConnectTimeout("Endpoint resolution/connection timed out") from error


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response, limit):
        self.response = response
        self.limit = limit

    async def __aiter__(self):
        received = 0
        async for chunk in self.response.aiter_stream():
            received += len(chunk)
            if received > self.limit:
                raise ValueError("Remote response exceeds hard transport limit")
            yield chunk

    async def aclose(self):
        await self.response.aclose()


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy, response_limit=32 * 1048576):
        self.policy = policy
        self.response_limit = response_limit
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=GuardedBackend(policy),
            max_connections=8,
            max_keepalive_connections=2,
            retries=0,
        )

    async def handle_async_request(self, request):
        self.policy.url(request.url)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self.pool.handle_async_request(core_request)
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=ResponseStream(response, self.response_limit),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


def remote_client(private_hosts=(), timeout=30, transport=None, response_limit=32 * 1048576):
    policy = NetworkPolicy(private_hosts)
    return httpx.AsyncClient(
        transport=transport or GuardedTransport(policy, response_limit),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


async def bounded_json(response, limit=131072):
    response.raise_for_status()
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > limit:
            raise ValueError("Remote response exceeds configured limit")
    import json

    return json.loads(content)
