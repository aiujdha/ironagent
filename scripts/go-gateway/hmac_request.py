#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.request
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a signed Go Gateway request.")
    parser.add_argument("--url", required=True, help="Gateway base URL, for example https://gateway.example.com")
    parser.add_argument("--method", default="GET", help="HTTP method")
    parser.add_argument("--path", required=True, help="Request path including query string")
    parser.add_argument("--api-key", required=True, help="X-API-Key value")
    parser.add_argument("--client-id", required=True, help="X-Client-ID value")
    parser.add_argument("--secret", required=True, help="HMAC secret")
    parser.add_argument("--json", default="", help="JSON request body")
    args = parser.parse_args()

    method = args.method.upper()
    body = args.json.encode("utf-8") if args.json else b""
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    payload = ".".join([args.client_id, timestamp, nonce, method, args.path, body_hash])
    signature = hmac.new(args.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    request = urllib.request.Request(
        args.url.rstrip("/") + args.path,
        data=body if body else None,
        method=method,
        headers={
            "X-API-Key": args.api_key,
            "X-Client-ID": args.client_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
            "Accept": "application/json",
        },
    )
    if body:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            output = response.read().decode("utf-8")
            sys.stdout.write(output)
            if output and not output.endswith("\n"):
                sys.stdout.write("\n")
            return 0
    except urllib.error.HTTPError as exc:
        sys.stderr.write(exc.read().decode("utf-8"))
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
