#!/usr/bin/env python3
"""Offline native CLI stand-in for real subprocess and WebSocket contract tests."""
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from urllib.parse import urlsplit

args = sys.argv[1:]
folder = Path(os.environ["VOICE_FIXTURE_DIR"])


def save(name, value):
    (folder / name).write_text(json.dumps(value), encoding="utf-8")


def exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError
        data += part
    return data


def send(sock, payload, opcode=1, final=True):
    mask = b"abcd"
    length = len(payload)
    head = bytes([(128 if final else 0) | opcode])
    if length < 126:
        head += bytes([128 | length])
    elif length < 65536:
        head += b"\xfe" + struct.pack("!H", length)
    else:
        head += b"\xff" + struct.pack("!Q", length)
    sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))


def receive(sock):
    first, second = exact(sock, 2)
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(sock, 8))[0]
    return first & 15, exact(sock, length)


if args == ["--help"]:
    print("--no-daemon --remote --remote-auth-token-env" if not (folder / "old").exists() else "old codex")
    raise SystemExit
if args == ["app-server", "--help"]:
    print("--stdio")
    raise SystemExit
if "generate-json-schema" in args:
    path = Path(args[args.index("--out") + 1]) / "v2"
    path.mkdir()
    schema = json.loads(Path(__file__).with_name("codex-realtime-start-0.162.1.json").read_text())
    if (folder / "unsupported-schema").exists():
        schema["definitions"]["ConversationTextRole"]["enum"].remove("developer")
    (path / "ThreadRealtimeStartParams.json").write_text(json.dumps(schema))
    raise SystemExit
if "app-server" in args:
    save("backend.json", {"args": args, "pid": os.getpid(), "session":os.getenv("ASHA_HUB_SESSION_ID"),
                          "token_present":any(k.startswith("ASHA_VOICE_TOKEN_") for k in os.environ)})
    held_reads = []
    with (folder / "wire.jsonl").open("ab", buffering=0) as capture:
        for wire in sys.stdin.buffer:
            capture.write(wire)
            message = json.loads(wire)
            if message.get("method") == "fixture/crash":
                raise SystemExit(42)
            if message.get("method") == "thread/read":
                target = message["params"]["threadId"]
                if target.startswith("hold-"):
                    held_reads.append(message)
                    continue
                if target == "missing":
                    result = {"id":message["id"], "error":{"code":-32000, "message":"fixture thread absent"}}
                else:
                    thread = {"id":target, "cwd":str(folder / "other") if target == "foreign" else str(folder)}
                    result = {"id":message["id"], "result":{"thread":{} if target == "malformed" else thread}}
                sys.stdout.buffer.write(json.dumps(result).encode() + b"\n")
            elif message.get("method") == "fixture/release":
                approval = {"id":held_reads[0]["id"],"method":"item/commandExecution/requestApproval",
                            "params":{"command":"fixture"}}
                sys.stdout.buffer.write(json.dumps(approval).encode() + b"\n")
                sys.stdout.buffer.write(b'{ "method":"fixture/progress", "params":{"unchanged":true} }\n')
                sys.stdout.buffer.write(b'{ "id":"asha-voice-scope-unrelated", "result":{"unchanged":true} }\n')
                for held in reversed(held_reads):
                    target = held["params"]["threadId"]
                    result = {"id":held["id"],"result":{"thread":{"id":target,
                        "cwd":str(folder / "other") if target == "hold-foreign" else str(folder)}}}
                    sys.stdout.buffer.write(json.dumps(result).encode() + b"\n")
                held_reads.clear()
                sys.stdout.buffer.write(json.dumps({"id":message["id"],"result":{}}).encode() + b"\n")
            elif message.get("method") == "fixture/approval":
                sys.stdout.buffer.write(b'{ "id": "native-approval", "method": "item/commandExecution/requestApproval", "params": {"command":"fixture"} }\n')
            elif message.get("method") == "fixture/hang":
                child = subprocess.Popen([sys.executable,"-c","import time; time.sleep(300)"])
                save("child.json", child.pid)
                child.wait()
            elif "id" in message:
                result = {"id": message["id"], "result": {"echo": message.get("params"), "answer":message.get("result")}}
                sys.stdout.buffer.write(json.dumps(result).encode() + b"\n")
            sys.stdout.buffer.flush()
    raise SystemExit
save("frontend.json", {"args":args,"pid":os.getpid()})
if "--remote" not in args:
    raise SystemExit(0)
url = urlsplit(args[args.index("--remote") + 1])
token = os.environ[args[args.index("--remote-auth-token-env") + 1]]
sock = socket.create_connection((url.hostname, url.port), timeout=8)
request = (f"GET / HTTP/1.1\r\nHost: {url.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {base64.b64encode(b'0123456789abcdef').decode()}\r\n"
           f"Authorization: Bearer {token}\r\n\r\n")
sock.sendall(request.encode())
response = b""
while not response.endswith(b"\r\n\r\n"):
    response += exact(sock, 1)
assert b"101 Switching" in response, response
results = []
try:
    for frame in json.loads((folder / "scenario.json").read_text()):
        wire = frame.encode()
        if (folder / "fragment").exists():
            send(sock, wire[:len(wire)//2], final=False)
            send(sock, b"keepalive", opcode=9)
            assert receive(sock) == (10, b"keepalive")
            send(sock, wire[len(wire)//2:], opcode=0)
        else:
            send(sock, wire)
        opcode, data = receive(sock)
        results.append(data.decode())
        save("results.json", results)
except EOFError:
    pass
finally:
    sock.close()
raise SystemExit(int((folder / "exit").read_text()) if (folder / "exit").exists() else 0)
