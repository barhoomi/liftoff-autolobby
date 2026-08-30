#!/usr/bin/env python3
"""Unstick the Steam client's login UI when it is frozen on "Waiting for network...".

WHY THIS EXISTS (root-caused live 2026-08-05, in this container)
---------------------------------------------------------------
The Steam client's UI is a CEF app (steamwebhelper) whose JS talks to the native client
through a generated `SteamClient.*` binding surface. Its `SystemNetworkStore` starts with
`m_bIsAwaitingInitialNetworkState = true` and only clears that flag when the native client
pushes an initial network-device list, which it does through

    SteamClient.System.Network.RegisterForDeviceChanges(...)

That method is only bound when the native client managed to create a **NetworkManager**
client. In this container there is no NetworkManager and no D-Bus *system* bus, so the
client logs (logs/client_networkmanager.txt):

    Init: failed to create a NetworkManager client

and `SteamClient.System.Network` ends up with only 5 of its methods
(RegisterForConnectionStateUpdate, RegisterForAppSummaryUpdate,
RegisterForConnectivityTestChanges, SetFakeLocalSystemState, ForceTestConnectivity) --
`RegisterForDeviceChanges` among the missing ones. The SteamUI bundle still *thinks* the
method exists (its capability table is static), calls it, and dies with

    SteamUI: ERROR: SteamApp Init - Before Login - SystemNetworkStore - ERROR TypeError:
    SteamClient.System.Network.RegisterForDeviceChanges is not a function

so the flag is never cleared. The login window renders
`if (isAwaitingInitialNetworkState) return <LoginStatus loginState=WaitingForNetwork/>`,
i.e. a Steam logo + "Waiting for network..." and NOTHING ELSE, forever -- no username
field, no password field, no Sign in button. The one-time human login is impossible.

None of this reflects real connectivity: sockets, DNS, TLS, the CM endpoints, Steam's own
`Connectivity test: result=Connected` and its client-update manifest downloads all work
from inside the container. Only the UI's device-state store is stuck.

WHAT THIS DOES
--------------
Attaches to the client's own CEF debugger (enabled by the
`.cef-enable-remote-debugging` marker file the entrypoint drops next to the client, which
makes steamwebhelper listen on 127.0.0.1:8080 -- container-internal, never published) and
sets the two observables the missing callback would have set:

    SystemNetworkStore.m_bIsAwaitingInitialNetworkState = false
    SystemNetworkStore.m_bIsConnectedToANetwork         = true

The login form then renders normally and the operator can type credentials over VNC.
Verified live 2026-08-05: black "Waiting for network..." screen -> full login form with a
freshly fetched QR code (which is itself proof the client's network path is fine).

This is deliberately a UI nudge and not a new dependency: the alternative fix is to run
NetworkManager + a D-Bus system bus inside the container purely so a store can be told
"yes, there is an interface", which is a lot of daemon for one boolean.

Stdlib only (no websocket lib in the image): implements just enough of the WebSocket
client handshake + framing to drive one CDP `Runtime.evaluate` call.

Exit codes (the entrypoint treats every one of them as non-fatal):
    0  the store is fine now -- either already fine, or this run unstuck it
    1  could not reach / drive the CEF debugger (client not up yet, port closed, ...)
    2  reached it, but the store did not accept the change
Prints one short status word on stdout: already-ok | unstuck | no-store | failed.
"""

import base64
import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request

DEBUG_HOST = os.environ.get("STEAM_CEF_DEBUG_HOST", "127.0.0.1")
DEBUG_PORT = int(os.environ.get("STEAM_CEF_DEBUG_PORT", "8080"))
TIMEOUT = float(os.environ.get("STEAM_CEF_DEBUG_TIMEOUT", "5"))

# Runs in the SharedJSContext, where SteamUI publishes the store as window.SystemNetworkStore
# (see `I.Get()` in the bundle: `window.SystemNetworkStore = I.s_Singleton`).
EXPRESSION = """
(() => {
    const s = window.SystemNetworkStore;
    if (!s) return "no-store";
    if (!s.isAwaitingInitialNetworkState) return "already-ok";
    s.m_bIsAwaitingInitialNetworkState = false;
    s.m_bIsConnectedToANetwork = true;
    return s.isAwaitingInitialNetworkState ? "failed" : "unstuck";
})()
"""


def find_shared_js_context():
    """Return the ws:// debugger URL of the SharedJSContext target, or None."""
    url = f"http://{DEBUG_HOST}:{DEBUG_PORT}/json"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        targets = json.load(resp)
    for target in targets:
        if target.get("title") == "SharedJSContext" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    return None


def ws_connect(ws_url):
    """Minimal RFC6455 client handshake. Returns a connected socket."""
    rest = ws_url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port or 80)
    path = "/" + path

    sock = socket.create_connection((host, port), timeout=TIMEOUT)
    sock.settimeout(TIMEOUT)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {hostport}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())

    # Read just the response headers; CDP never sends body bytes before the first frame,
    # but tolerate frame bytes arriving in the same read by keeping the remainder.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("CEF debugger closed the connection during handshake")
        buf += chunk
    head, _, remainder = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n", 1)[0]:
        raise ConnectionError(f"CEF debugger refused the upgrade: {head.splitlines()[:1]}")
    return sock, remainder


def ws_send_text(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = b"\x81"  # FIN + text opcode
    length = len(payload)
    if length < 126:
        header += struct.pack("!B", 0x80 | length)
    elif length < (1 << 16):
        header += struct.pack("!BH", 0x80 | 126, length)
    else:
        header += struct.pack("!BQ", 0x80 | 127, length)
    sock.sendall(header + mask + masked)


class FrameReader:
    """Reads server->client frames (never masked, per RFC6455)."""

    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buf = initial

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("CEF debugger closed the connection")
            self.buf += chunk

    def _take(self, n):
        self._need(n)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def next_text(self):
        """Return the next complete text message (reassembling continuations)."""
        message = b""
        while True:
            b0, b1 = self._take(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._take(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8))[0]
            payload = self._take(length) if length else b""
            if opcode == 0x8:  # close
                raise ConnectionError("CEF debugger sent a close frame")
            if opcode in (0x9, 0xA):  # ping/pong -- ignore, CDP does not rely on these
                continue
            message += payload
            if fin:
                return message.decode("utf-8", "replace")


def main():
    try:
        ws_url = find_shared_js_context()
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cef-debugger-unreachable ({exc})")
        return 1
    if not ws_url:
        print("no-sharedjscontext-target")
        return 1

    sock = None
    try:
        sock, remainder = ws_connect(ws_url)
        ws_send_text(sock, json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": EXPRESSION, "returnByValue": True},
        }))
        reader = FrameReader(sock, remainder)
        for _ in range(20):  # skip unrelated CDP events
            message = json.loads(reader.next_text())
            if message.get("id") == 1:
                result = message.get("result", {}).get("result", {})
                status = result.get("value", "unknown")
                print(status)
                if status in ("already-ok", "unstuck"):
                    return 0
                return 2
        print("no-response")
        return 1
    except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as exc:
        print(f"cef-eval-failed ({exc})")
        return 1
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
