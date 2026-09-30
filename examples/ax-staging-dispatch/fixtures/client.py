#!/usr/bin/env python3
"""Framed RPC client and synthetic OIDC protocol client for owned containers.

The OIDC fixture has no login UI and auto-authorizes fixed synthetic identities.
This client cannot sign an Opaque approval. Inputs/outputs may contain session
credentials and must stay in owner-private runtime files.
"""
import json
import os
from pathlib import Path
import socket
import struct
import sys
import urllib.parse
import urllib.request


def rpc(message):
    def exact(connection, count):
        data = bytearray()
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise ConnectionError("broker closed transport")
            data.extend(chunk)
        return data
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(180)
        connection.connect("/run/opaque/opaqued.sock")
        handshake = {"handshake": "v1", "daemon_token": Path("/run/opaque/daemon.token").read_text().strip()}
        if message.get("use_session"):
            handshake["session_token"] = json.loads(Path("/tmp/scope-session.json").read_text())["session_token"]
        for frame in (handshake, {"id": 1, "method": message["method"], "params": message.get("params", {})}):
            raw = json.dumps(frame).encode()
            connection.sendall(struct.pack(">I", len(raw)) + raw)
        count = struct.unpack(">I", exact(connection, 4))[0]
        if count > 1024 * 1024:
            raise ValueError("oversized broker frame")
        return json.loads(exact(connection, count))


class FixtureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "http" or parsed.netloc != "127.0.0.1:18916" or parsed.path != "/callback":
            raise ValueError("redirect outside synthetic broker callback")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def login(message):
    parsed = urllib.parse.urlsplit(message["auth_url"])
    if parsed.scheme != "http" or parsed.netloc != "127.0.0.1:18912" or parsed.path != "/authorize":
        raise ValueError("authorization endpoint outside owned synthetic fixture")
    if message["subject"] not in {"requester", "reviewer"}:
        raise ValueError("unknown synthetic identity")
    url = message["auth_url"] + "&" + urllib.parse.urlencode({"fixture_subject": message["subject"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), FixtureRedirect())
    with opener.open(url, timeout=10) as response:
        if response.status != 200:
            raise ValueError("broker callback failed")
    return {"synthetic_identity_protocol": "completed", "human_identity_proven": False}


if __name__ == "__main__":
    message = json.load(sys.stdin)
    if sys.argv[1] == "rpc":
        try:
            print(json.dumps(rpc(message)))
        except ConnectionError:
            print(json.dumps({"transport_error": "broker_closed_without_response"}))
    elif sys.argv[1] == "login":
        print(json.dumps(login(message)))
    elif sys.argv[1] == "session":
        if message.get("mode") != "delegated" or not message.get("session_token", "").startswith("opqd1."):
            raise ValueError("expected a delegated broker session")
        descriptor = os.open("/tmp/scope-session.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(message, output)
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"stored": True, "custody": "private agent tmpfs"}))
    else:
        raise ValueError("unsupported fixture client command")
