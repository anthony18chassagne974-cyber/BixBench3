"""Test-only HTTP CONNECT/forward proxy on 127.0.0.1:<port> (stands in for squid in jail tests)."""
import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    method, target, _ = head.split(b"\r\n", 1)[0].split(b" ", 2)
    if method == b"CONNECT":
        host, port = target.rsplit(b":", 1)
        up_r, up_w = await asyncio.open_connection(host.decode(), int(port))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
    else:
        from urllib.parse import urlsplit
        url = urlsplit(target.decode())
        up_r, up_w = await asyncio.open_connection(url.hostname, url.port or 80)
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        up_w.write(head.replace(target, path.encode(), 1))
    await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", int(sys.argv[1]))
    async with server:
        await server.serve_forever()

asyncio.run(main())
