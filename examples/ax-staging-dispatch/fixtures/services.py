#!/usr/bin/env python3
"""Disposable synthetic OIDC identity fixture for the broker pod.

It auto-authorizes two fixed test accounts and proves no human identity. Opaque's
native reviewer is a separate process on the host. No approval key, approval
endpoint or GitHub credential exists here. Keep state outside Git.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.parse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ROOT = Path("/fixture")
ISSUER = "http://127.0.0.1:18912"
CLIENT = "opaque-live-scope-fixture"
CALLBACK = "http://127.0.0.1:18916/callback"
LOCK = threading.Lock()
CODES = {}


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def initialize():
    global SIGNER
    key_path = ROOT / "oidc-key.pem"
    if not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        private(key_path, key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    SIGNER = serialization.load_pem_private_key(key_path.read_bytes(), password=None)


def jwt(claims):
    head = encoded(json.dumps({"alg": "ES256", "kid": "synthetic-oidc", "typ": "JWT"}).encode())
    body = encoded(json.dumps(claims).encode())
    signing = f"{head}.{body}".encode()
    r, s = decode_dss_signature(SIGNER.sign(signing, ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{encoded(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class Identity(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 16384:
            raise ValueError("request body out of bounds")
        return self.rfile.read(length)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/.well-known/openid-configuration":
            self.reply(200, {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                            "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"})
        elif url.path == "/jwks":
            point = SIGNER.public_key().public_numbers()
            self.reply(200, {"keys": [{"kty": "EC", "kid": "synthetic-oidc", "alg": "ES256", "use": "sig",
                "crv": "P-256", "x": encoded(point.x.to_bytes(32, "big")), "y": encoded(point.y.to_bytes(32, "big"))}]})
        elif url.path == "/authorize":
            query = urllib.parse.parse_qs(url.query, strict_parsing=True)
            if any(len(values) != 1 for values in query.values()):
                self.reply(400, {"error": "duplicate_parameter"})
                return
            query = {key: values[0] for key, values in query.items()}
            subject = query.get("fixture_subject")
            if (subject not in {"reviewer", "requester"} or query.get("redirect_uri") != CALLBACK
                    or query.get("client_id") != CLIENT or query.get("response_type") != "code"
                    or query.get("code_challenge_method") != "S256" or not query.get("state")
                    or not query.get("nonce") or not query.get("code_challenge")):
                self.reply(400, {"error": "invalid_synthetic_authorization"})
                return
            code = secrets.token_urlsafe(32)
            with LOCK:
                CODES[code] = {**query, "expires": time.time() + 60}
            self.send_response(302)
            self.send_header("Location", CALLBACK + "?" + urllib.parse.urlencode({"state": query["state"], "code": code}))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.reply(404, {"error": "unknown_endpoint"})

    def do_POST(self):
        if self.path != "/token":
            self.reply(404, {"error": "unknown_endpoint"})
            return
        data = urllib.parse.parse_qs(self.body().decode(), strict_parsing=True)
        if any(len(value) != 1 for value in data.values()):
            self.reply(400, {"error": "invalid_grant"})
            return
        data = {key: value[0] for key, value in data.items()}
        with LOCK:
            code = CODES.pop(data.get("code", ""), None)
        if (code is None or code["expires"] < time.time() or data.get("client_id") != CLIENT
                or data.get("redirect_uri") != CALLBACK or data.get("grant_type") != "authorization_code"
                or encoded(hashlib.sha256(data.get("code_verifier", "").encode()).digest()) != code["code_challenge"]):
            self.reply(400, {"error": "invalid_grant"})
            return
        now = int(time.time())
        subject = code["fixture_subject"]
        self.reply(200, {"token_type": "Bearer", "expires_in": 300, "access_token": "synthetic-unused",
            "id_token": jwt({"iss": ISSUER, "aud": CLIENT, "sub": subject, "nonce": code["nonce"],
                "iat": now, "exp": now + 300, "email": subject + "@example.invalid", "email_verified": True,
                "name": "Synthetic " + subject})})


def main():
    initialize()
    ThreadingHTTPServer(("127.0.0.1", 18912), Identity).serve_forever()


if __name__ == "__main__":
    main()
