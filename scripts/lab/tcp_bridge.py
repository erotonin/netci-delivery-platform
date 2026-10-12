"""Forward one TCP address to another. Lab only.

Prometheus and Alertmanager listen on 127.0.0.1 on purpose. The Kubernetes install's API
and worker query Prometheus for release verification (ADR-046), and pods reach the host
only through the Docker bridge address. This listens there -- 172.17.0.1, not 0.0.0.0 --
so the cluster can reach Prometheus and nothing outside the host can.

    python tcp_bridge.py 172.17.0.1:19090 127.0.0.1:9090
"""

import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def main(listen: str, target: str) -> None:
    lhost, lport = listen.rsplit(":", 1)
    thost, tport = target.rsplit(":", 1)

    async def handle(client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(thost, int(tport))
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer),
                             return_exceptions=True)

    server = await asyncio.start_server(handle, lhost, int(lport))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
