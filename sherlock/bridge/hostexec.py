"""Job-local helper: runs `apptainer` / `gcloud` on the Sherlock host for the runner container.

The harness venv needs glibc >= 2.28 (numpy/pandas/scipy/pyarrow/tiktoken wheels) and
therefore runs in a python:3.12 container; Apptainer cannot be nested there, so the
runner's `apptainer` and `gcloud` are thin clients (client.py) that forward their argv,
stdin, stdout, stderr and exit status over a private Unix socket to this helper.

- Only `apptainer` and `gcloud` can be run, with a fixed clean environment (no client env).
- The socket lives in a 0700 directory of the job; the agent sandbox never sees it.
- If a client disconnects (killed, timed out), its host process group is terminated.
- Started and stopped by run_job.sbatch; it never outlives the job.
Usage: hostexec.py <socket-path>
"""
import asyncio
import json
import os
import shutil
import signal
import struct
import sys

ALLOWED = {name: shutil.which(name) for name in ("apptainer", "gcloud")}
ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "APPTAINER_CACHEDIR", "APPTAINER_TMPDIR",
            "TMPDIR", "CLOUDSDK_CONFIG", "CLOUDSDK_CORE_PROJECT", "XDG_RUNTIME_DIR")
CLEAN_ENV = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


async def read_frame(reader):
    head = await reader.readexactly(5)
    kind, size = head[:1], struct.unpack(">I", head[1:])[0]
    return kind, await reader.readexactly(size)


def frame(kind, payload):
    return kind + struct.pack(">I", len(payload)) + payload


async def kill_group(proc):
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def handle(reader, writer):
    proc = None
    try:
        kind, payload = await read_frame(reader)
        request = json.loads(payload)
        argv = request["argv"]
        exe = ALLOWED.get(argv[0]) if kind == b"A" and argv else None
        if not exe:
            writer.write(frame(b"R", f"hostexec: {argv[:1]} not allowed\n".encode()) + frame(b"X", struct.pack(">i", 126)))
            await writer.drain()
            return
        cwd = request.get("cwd")
        proc = await asyncio.create_subprocess_exec(
            exe, *argv[1:], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd if cwd and os.path.isdir(cwd) else None,
            env=CLEAN_ENV, start_new_session=True,
        )

        async def pump(stream, tag):
            while chunk := await stream.read(65536):
                writer.write(frame(tag, chunk))
                await writer.drain()

        async def from_client():
            stdin_open = True
            while True:
                try:
                    tag, data = await read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    return "gone"
                if tag == b"I" and stdin_open:
                    proc.stdin.write(data)
                    try:
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        stdin_open = False
                elif tag == b"E" and stdin_open:
                    stdin_open = False
                    proc.stdin.close()

        client = asyncio.ensure_future(from_client())
        outputs = asyncio.ensure_future(asyncio.gather(pump(proc.stdout, b"O"), pump(proc.stderr, b"R")))
        done, _ = await asyncio.wait({client, outputs}, return_when=asyncio.FIRST_COMPLETED)
        if client in done:  # client disconnected before the command finished
            outputs.cancel()
            await kill_group(proc)
            return
        code = await proc.wait()
        client.cancel()
        writer.write(frame(b"X", struct.pack(">i", code if code >= 0 else 128 - code)))
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        if proc is not None:
            await kill_group(proc)
    finally:
        writer.close()


async def main(path):
    os.umask(0o077)
    server = await asyncio.start_unix_server(handle, path=path)
    os.chmod(path, 0o600)
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
    async with server:
        await stop


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
