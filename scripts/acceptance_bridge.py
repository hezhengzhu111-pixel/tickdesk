"""Test-only TCP bridge from the emulator's reverse port to the isolated relay."""
import argparse
import asyncio


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def handle(reader, writer, target_host, target_port):
    try:
        remote_reader, remote_writer = await asyncio.open_connection(target_host, target_port)
    except OSError:
        writer.close()
        return
    await asyncio.gather(
        pipe(reader, remote_writer), pipe(remote_reader, writer)
    )


async def main(host, port, target_host, target_port):
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, target_host, target_port), host, port
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.listen_host, args.listen_port, args.target_host, args.target_port))
