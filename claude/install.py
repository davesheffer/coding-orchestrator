#!/usr/bin/env python3
"""Safely install the Claude Code orchestration bundle.

Usage: install.py [--force] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path


START = b"<!-- CLAUDE-ORCHESTRATOR:START -->"
END = b"<!-- CLAUDE-ORCHESTRATOR:END -->"
ROLE_NAMES = ("scout", "runner", "builder", "critic")
LEGACY_CLAUDE_HASHES = {"2d4152d9c33d2f7b98c85a0a06e697afc2c5ebf5bac9d7faf1a6cf09e266aaf0"}
LEGACY_MANAGED_HASHES = {
    "agents/scout.md": {"9907a085d3e07a0e702e05600ee9462f0c9ea144039f9350ec083dbe025a2f8f"},
    "agents/runner.md": {"6941c6bc00d1f5888c63a84d1011572e0646ffcd6e0d3c477ab01f1161b01b25"},
    "agents/builder.md": {"3af20d8ab64a00c3f45958502601f62b8d4ea9661e2f7e32ea0375e780fc8ac1"},
    "agents/critic.md": {"c24feddae163daddc687b26d644ac51891bbfa41af526e855a918247743c435b"},
    "relay/relay.py": {"41d9b40baa8f7a017ab891aa2373cdc739f66e3296dd71e34dc9317b1f3232da"},
    "bin/pr-status": {"c42240257b2f1bb7165a322bed1dc236a39ade26f951dd1c67ece7a0281e1222"},
}
MANIFEST = ".coding-orchestrator-manifest.json"


def fail(message: str) -> None:
    raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular(path: Path) -> bytes | None:
    if path.is_symlink():
        fail(f"refusing symlink: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        fail(f"expected regular file: {path}")
    return path.read_bytes()


def parse_json(path: Path, data: bytes) -> dict:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid JSON in {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected JSON object in {path}")
    return value


def validate_role(path: Path, data: bytes, name: str) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"invalid UTF-8 in {path}: {exc}")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        fail(f"invalid frontmatter in {path}")
    front = text.split("\n---\n", 1)[0].splitlines()[1:]
    fields = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
              for line in front if ":" in line}
    required = {"name", "description", "model", "tools", "permissionMode", "maxTurns"}
    if fields.get("name") != name or required - fields.keys():
        fail(f"invalid role {path}: missing fields or wrong name")


def managed_block(data: bytes, path: Path) -> bytes:
    starts = [i for i in range(len(data)) if data.startswith(START, i)]
    ends = [i for i in range(len(data)) if data.startswith(END, i)]
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        fail(f"invalid managed markers in {path}")
    return data[starts[0]:ends[0] + len(END)]


def merge_instructions(existing: bytes | None, source: bytes, path: Path) -> bytes:
    block = managed_block(source, Path("source CLAUDE.md"))
    if existing is None or digest(existing) in LEGACY_CLAUDE_HASHES:
        return source
    starts = [i for i in range(len(existing)) if existing.startswith(START, i)]
    ends = [i for i in range(len(existing)) if existing.startswith(END, i)]
    if not starts and not ends:
        separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
        return existing + separator + block + b"\n"
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        fail(f"invalid managed markers in {path}")
    end = ends[0] + len(END)
    return existing[:starts[0]] + block + existing[end:]


def is_old_relay_hook(command: object) -> bool:
    return isinstance(command, str) and bool(re.search(
        r"relay/relay\.py['\"]?\s+(?:prompt|stop)\b", command
    ))


def merge_hooks(existing: dict, template: dict, relay: Path) -> dict:
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        fail("settings.json hooks must be an object")
    for event, wanted_groups in template["hooks"].items():
        have = hooks.setdefault(event, [])
        if not isinstance(have, list):
            fail(f"settings.json hooks.{event} must be an array")
        cleaned = []
        for group in have:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                fail(f"settings.json hooks.{event} contains an invalid group")
            kept = [h for h in group.get("hooks", [])
                    if not is_old_relay_hook(h.get("command") if isinstance(h, dict) else None)]
            if kept:
                copy = dict(group)
                copy["hooks"] = kept
                cleaned.append(copy)
        for group in wanted_groups:
            copy = json.loads(json.dumps(group))
            for hook in copy["hooks"]:
                hook["command"] = hook["command"].replace("__RELAY__", shlex.quote(str(relay)))
            cleaned.append(copy)
        hooks[event] = cleaned
    return result


def validate_dir(path: Path) -> None:
    if path.is_symlink():
        fail(f"refusing symlink destination directory: {path}")
    if path.exists() and not path.is_dir():
        fail(f"destination ancestor is not a directory: {path}")


def backup_path(path: Path) -> Path:
    candidate = path.with_name(path.name + ".bak")
    number = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(path.name + f".bak.{number}")
        number += 1
    return candidate


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else mode
    fd, temporary = tempfile.mkstemp(prefix=".orchestrator-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, old_mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def create_backup(path: Path, data: bytes) -> Path:
    backup = backup_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".orchestrator-backup-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.link(temporary, backup)
        return backup
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    dest = Path(os.environ.get("CLAUDE_HOME") or Path.home() / ".claude").expanduser().absolute()
    source_claude = (root / "CLAUDE.md").read_bytes()
    managed_block(source_claude, root / "CLAUDE.md")
    hook_template = parse_json(root / "hooks.json", (root / "hooks.json").read_bytes())

    managed_sources: dict[Path, bytes] = {}
    for name in ROLE_NAMES:
        source = root / "agents" / f"{name}.md"
        data = read_regular(source)
        assert data is not None
        validate_role(source, data, name)
        managed_sources[dest / "agents" / f"{name}.md"] = data
    managed_sources[dest / "relay" / "relay.py"] = (root / "relay" / "relay.py").read_bytes()
    managed_sources[dest / "bin" / "pr-status"] = (root / "bin" / "pr-status").read_bytes()

    directories = (dest, dest / "agents", dest / "relay", dest / "relay" / "handoffs",
                   dest / "relay" / "state", dest / "bin")
    for directory in directories:
        validate_dir(directory)

    manifest_path = dest / MANIFEST
    manifest_data = read_regular(manifest_path)
    manifest = parse_json(manifest_path, manifest_data) if manifest_data is not None else {}
    previous_hashes = manifest.get("files", {})
    if not isinstance(previous_hashes, dict):
        fail(f"invalid manifest: {manifest_path}")

    existing = {path: read_regular(path) for path in managed_sources}
    claude_path = dest / "CLAUDE.md"
    settings_path = dest / "settings.json"
    config_path = dest / "relay" / "config.json"
    claude_existing = read_regular(claude_path)
    settings_existing = read_regular(settings_path)
    config_existing = read_regular(config_path)
    if config_existing is not None:
        parse_json(config_path, config_existing)
    settings = parse_json(settings_path, settings_existing) if settings_existing is not None else {}

    desired = dict(managed_sources)
    desired[claude_path] = merge_instructions(claude_existing, source_claude, claude_path)
    desired[settings_path] = (json.dumps(
        merge_hooks(settings, hook_template, dest / "relay" / "relay.py"), indent=2
    ) + "\n").encode()
    if config_existing is None:
        desired[config_path] = (root / "relay" / "config.json").read_bytes()

    conflicts = []
    for path, data in managed_sources.items():
        prior = existing[path]
        rel = str(path.relative_to(dest))
        prior_hash = digest(prior) if prior is not None else None
        recognized = previous_hashes.get(rel) == prior_hash or prior_hash in LEGACY_MANAGED_HASHES.get(rel, set())
        if prior is not None and prior != data and not recognized:
            conflicts.append(path)
    if conflicts and not args.force:
        fail("locally modified managed files (review, then use --force): " + ", ".join(map(str, conflicts)))

    new_manifest = {"schema": 1, "files": {
        str(path.relative_to(dest)): digest(data) for path, data in managed_sources.items()
    }}
    desired[manifest_path] = (json.dumps(new_manifest, indent=2, sort_keys=True) + "\n").encode()

    changes = []
    for path, data in desired.items():
        prior = read_regular(path)
        if prior != data:
            changes.append((path, data, prior))
    if args.dry_run:
        for path, _, prior in changes:
            if prior is not None:
                print(f"backup {path}")
            print(f"write {path}")
        return 0

    for directory in directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    executable = {dest / "bin" / "pr-status"}
    for path, data, prior in changes:
        if prior is not None:
            backup = create_backup(path, prior)
            print(f"backup {path} -> {backup}")
        atomic_write(path, data, 0o700 if path in executable else 0o600)
        print(f"write {path}")
    helper = dest / "bin" / "pr-status"
    if helper.exists() and not os.access(helper, os.X_OK):
        os.chmod(helper, stat.S_IMODE(helper.stat().st_mode) | stat.S_IXUSR)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"claude install: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
