#!/usr/bin/env python3
"""Save a client-neutral handoff and request a fresh VS Code agent tab."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time


def home() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_HANDOFF_HOME") or
                Path.home() / ".coding-orchestrator").expanduser().resolve()


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def private_text(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)


def write_json(path: Path, data: dict) -> None:
    private_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    private_text(temporary, json.dumps(data, ensure_ascii=False))
    os.replace(temporary, path)


def open_uri(uri: str) -> None:
    if sys.platform == "win32":
        code = shutil.which("code")
        if code:
            subprocess.run([code, "--open-url", uri], check=True, capture_output=True)
        else:
            os.startfile(uri)
    else:
        command = ["open", uri] if sys.platform == "darwin" else ["xdg-open", uri]
        subprocess.run(command, check=True, capture_output=True)


def launch(client: str, handoff: Path, resume_token: str = "", timeout: float = 12.0) -> str:
    handoff = handoff.expanduser().resolve(strict=True)
    if not handoff.is_file():
        raise ValueError(f"handoff is not a regular file: {handoff}")
    if client not in ("claude", "codex"):
        raise ValueError(f"unknown client: {client}")
    if client == "claude" and not resume_token.startswith("relay:"):
        raise ValueError("Claude handoffs require a relay:<id> resume token")
    request_id = secrets.token_hex(16)
    prompt = (f"{resume_token} continue from the saved handoff." if client == "claude"
              else f"Continue from the attached handoff file {handoff}. Verify the listed state before acting.")
    root = home()
    write_json(root / "launches" / f"{request_id}.json", {
        "client": client, "handoff": str(handoff), "prompt": prompt,
        "created_at": time.time(),
    })
    uri = f"vscode://coding-orchestrator.handoff-bridge/open?id={request_id}"
    try:
        open_uri(uri)
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Editor launch failed ({exc}). Open a new {client} tab and send: {prompt}"
    ack = root / "acks" / f"{request_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = json.loads(ack.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.1)
            continue
        if result.get("status") == "opened":
            if client == "codex":
                return "Codex tab launch acknowledged; handoff and continuation prompt copied. Paste and send it."
            return "Claude tab launch acknowledged with the continuation prompt pre-filled. Press Enter there."
        return f"Editor could not open the {client} tab: {result.get('error', 'unknown error')}. Open one and send: {prompt}"
    return f"Editor launch was requested but not confirmed. Open a new {client} tab and send: {prompt}"


def save_codex(title: str, body: str) -> Path:
    if len(body.strip()) < 40:
        raise ValueError("handoff body is empty or too short")
    directory = home() / "handoffs"
    private_directory(directory)
    path = directory / f"{secrets.token_hex(8)}.md"
    private_text(path, f"# Handoff: {title}\n- cwd: {Path.cwd()}\n\n{body.strip()}\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    opener = sub.add_parser("open")
    opener.add_argument("--client", choices=("codex", "claude"), required=True)
    opener.add_argument("--handoff", type=Path, required=True)
    opener.add_argument("--resume-token", default="")
    writer = sub.add_parser("handoff")
    writer.add_argument("--client", choices=("codex",), required=True)
    writer.add_argument("--title", default="continue")
    writer.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "open":
            message = launch(args.client, args.handoff, args.resume_token)
            print(message)
            return 0 if "tab launch acknowledged" in message else 2
        else:
            path = save_codex(args.title, sys.stdin.read())
            print(f"handoff saved: {path}")
            if not args.no_open:
                message = launch("codex", path)
                print(message)
                return 0 if "tab launch acknowledged" in message else 2
    except (OSError, ValueError) as exc:
        parser.exit(1, f"rollover-open: {exc}\n")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
