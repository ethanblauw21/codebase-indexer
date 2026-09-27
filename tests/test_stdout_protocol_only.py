"""B-039: a stdio server's stdout carries protocol messages and nothing else.

A real server process is driven with raw JSON-RPC. Its one tool prints, and runs a
child process that prints. Every stdout line must parse as JSON-RPC, and both
prints must reach stderr instead. Before the fix, tool prints, watchdog reindex
logs and child output all went down the protocol pipe.
"""
import json
import os
import subprocess
import sys
import threading

SRC = os.path.join(os.path.dirname(__file__), "..", "src")

SERVER = r"""
import subprocess, sys
sys.path.insert(0, sys.argv[1])
import anyio
import MCPServer as M

@M.mcp.tool()
def noisy() -> str:
    print("noise from the tool")
    subprocess.run([sys.executable, "-c", "print('noise from a child')"])
    return "quiet result"

M._utf8_stdio()
M._detach_stdin()
anyio.run(M._serve_stdio, M._claim_stdout())
"""


def _send(proc, msg):
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def test_only_protocol_messages_on_stdout(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, str(script), os.path.abspath(SRC)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    stderr_chunks: list[bytes] = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()

    lines: list[bytes] = []
    got_result = threading.Event()

    def read_stdout():
        for line in proc.stdout:
            lines.append(line)
            if b'"id":2' in line.replace(b" ", b""):
                got_result.set()
                return

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "noisy", "arguments": {}}})
        assert got_result.wait(120), f"no tools/call result; stdout so far: {lines!r}"
    finally:
        proc.stdin.close()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        drain.join(10)

    messages = [json.loads(line) for line in lines if line.strip()]   # raises on any non-JSON line
    assert all(m.get("jsonrpc") == "2.0" for m in messages)
    result = next(m for m in messages if m.get("id") == 2)
    assert result["result"]["content"][0]["text"] == "quiet result"

    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    assert "noise from the tool" in stderr
    assert "noise from a child" in stderr
