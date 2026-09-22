#!/usr/bin/env python3
"""Install the bundled Codex orchestration files.

Usage: install.py [--force] [--dry-run] [--configure-routing]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path


START = b"<!-- CODEX-ORCHESTRATOR:START -->"
END = b"<!-- CODEX-ORCHESTRATOR:END -->"
ROLE_NAMES = ("scout", "runner", "builder", "critic")
ROLE_SANDBOX = {
    "scout": "read-only", "runner": "workspace-write",
    "builder": "workspace-write", "critic": "read-only",
}
REQUIRED_ROLE_FIELDS = {
    "name", "description", "model", "model_reasoning_effort", "sandbox_mode",
    "approval_policy", "developer_instructions",
}
ROUTING_POLICY = b'{"network_fallback_roles": []}\n'
ROUTING_FIELDS = {
    "default_subagent_model": "gpt-5.6-luna",
    "default_subagent_reasoning_effort": "low",
}


def fail(message: str) -> None:
    raise ValueError(message)


def read_regular(path: Path) -> bytes | None:
    if path.is_symlink():
        fail(f"refusing symlink: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        fail(f"expected regular file: {path}")
    return path.read_bytes()


def parse_toml(path: Path, data: bytes) -> dict:
    try:
        value = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"invalid TOML in {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"invalid TOML in {path}")
    return value


def validate_routing_policy(path: Path, data: bytes) -> None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid routing policy in {path}: {exc}")
    roles = value.get("network_fallback_roles") if isinstance(value, dict) else None
    if not isinstance(roles, list):
        fail(f"invalid routing policy in {path}: network_fallback_roles must be a list")
    if any(not isinstance(role, str) for role in roles):
        fail(f"invalid routing policy in {path}: role names must be strings")
    if len(set(roles)) != len(roles) or any(role not in ROLE_NAMES for role in roles):
        fail(f"invalid routing policy in {path}: unknown or duplicate role name")


def configure_routing_defaults(path: Path, data: bytes) -> bytes:
    config = parse_toml(path, data)
    agents = config.get("agents")
    if agents is None:
        agents = {}
    if not isinstance(agents, dict):
        fail(f"cannot safely configure routing defaults in {path}: agents is not a table")
    missing = {key: value for key, value in ROUTING_FIELDS.items() if key not in agents}
    if not missing:
        return data

    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    table = re.compile(r"^\s*\[agents\]\s*(?:#.*)?(?:\r?\n)?$")
    custom = re.compile(r"^\s*\[agents\.")
    table_indexes = [index for index, line in enumerate(lines) if table.match(line)]
    custom_indexes = [index for index, line in enumerate(lines) if custom.match(line)]
    newline = "\r\n" if "\r\n" in text else "\n"
    additions = [f'{key} = "{value}"{newline}' for key, value in missing.items()]
    if len(table_indexes) == 1:
        if not lines[table_indexes[0]].endswith("\n"):
            lines[table_indexes[0]] += newline
        lines[table_indexes[0] + 1:table_indexes[0] + 1] = additions
    elif not table_indexes and custom_indexes:
        lines[custom_indexes[0]:custom_indexes[0]] = ["[agents]" + newline, *additions, newline]
    elif not table_indexes and "agents" not in config:
        if text and not text.endswith(("\n", "\r")):
            lines.append(newline)
        lines.extend(["[agents]" + newline, *additions])
    else:
        fail(f"cannot safely configure routing defaults in {path}: unsupported agents table syntax")
    updated = "".join(lines).encode("utf-8")
    expected = copy.deepcopy(config)
    expected.setdefault("agents", {}).update(missing)
    if parse_toml(path, updated) != expected:
        fail(f"cannot safely configure routing defaults in {path}: semantic preservation check failed")
    return updated


def validate_role(path: Path, data: bytes, name: str) -> None:
    role = parse_toml(path, data)
    missing = REQUIRED_ROLE_FIELDS - role.keys()
    if missing or role.get("name") != name:
        fail(f"invalid role {path}: missing fields or wrong name")
    if role.get("sandbox_mode") != ROLE_SANDBOX[name] or role.get("approval_policy") != "never":
        fail(f"invalid role boundary in {path}")
    if role.get("web_search") != "disabled":
        fail(f"web search must be disabled in {path}")
    if role.get("features") != {"apps": False}:
        fail(f"unsupported or enabled role features in {path}")
    if role.get("agents") != {"enabled": False}:
        fail(f"nested agents must be disabled in {path}")
    if role.get("sandbox_workspace_write", {}).get("network_access") is not False:
        fail(f"network must be disabled in {path}")


def managed_block(data: bytes, path: Path) -> bytes:
    start, end = data.find(START), data.find(END)
    if data.count(START) != 1 or data.count(END) != 1 or start > end:
        fail(f"invalid managed markers in {path}")
    return data[start:end + len(END)]


def merge_instructions(existing: bytes | None, source: bytes, path: Path) -> bytes:
    block = managed_block(source, Path("source AGENTS.md"))
    if existing is None:
        return source
    start, end = existing.find(START), existing.find(END)
    if start == end == -1:
        return existing + (b"" if not existing or existing.endswith(b"\n") else b"\n") + block
    if existing.count(START) != 1 or existing.count(END) != 1 or start > end:
        fail(f"invalid managed markers in {path}")
    return existing[:start] + block + existing[end + len(END):]


def validate_destination_root(dest: Path) -> None:
    # Ancestors outside the selected Codex home may be legitimate system symlinks.
    if dest.is_symlink():
        fail(f"refusing symlink destination directory: {dest}")
    if dest.exists() and not dest.is_dir():
        fail(f"destination ancestor is not a directory: {dest}")


def validate_subdirs(dest: Path) -> None:
    for directory in (dest / "agents", dest / "bin"):
        if directory.is_symlink():
            fail(f"refusing symlink destination directory: {directory}")
        if directory.exists() and not directory.is_dir():
            fail(f"destination ancestor is not a directory: {directory}")


def backup_path(path: Path) -> Path:
    candidate = path.with_name(path.name + ".bak")
    number = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(path.name + f".bak.{number}")
        number += 1
    return candidate


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else mode
    fd, temporary = tempfile.mkstemp(prefix=".codex-install-", dir=path.parent)
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
    fd, temporary = tempfile.mkstemp(prefix=".codex-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        while True:
            backup = backup_path(path)
            try:
                # link(2) is an atomic create: an existing backup is never replaced.
                os.link(temporary, backup)
                return backup
            except FileExistsError:
                continue
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="replace differing managed files after backing them up")
    parser.add_argument("--dry-run", action="store_true", help="validate and list actions without changing the destination")
    parser.add_argument("--configure-routing", action="store_true", help="add missing default subagent routing settings to config.toml")
    args = parser.parse_args(argv)

    here = Path(__file__).resolve().parent
    dest = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
    sources = {dest / "AGENTS.md": (here / "AGENTS.md").read_bytes()}
    for name in ROLE_NAMES:
        source = here / "agents" / f"{name}.toml"
        data = read_regular(source)
        assert data is not None
        validate_role(source, data, name)
        sources[dest / "agents" / f"{name}.toml"] = data
    config_source = read_regular(here / "config.example.toml")
    helper_source = read_regular(here.parent / "bin" / "pr-status")
    agent_run_source = read_regular(here.parent / "bin" / "agent-run.py")
    assert config_source is not None and helper_source is not None and agent_run_source is not None
    sources[dest / "bin" / "pr-status"] = helper_source
    sources[dest / "bin" / "agent-run.py"] = agent_run_source
    parse_toml(here / "config.example.toml", config_source)
    managed_block(sources[dest / "AGENTS.md"], here / "AGENTS.md")

    validate_destination_root(dest)
    validate_subdirs(dest)
    existing: dict[Path, bytes | None] = {}
    for target, data in sources.items():
        prior = read_regular(target)
        existing[target] = prior
        if target.suffix == ".toml" and prior is not None:
            # Installed roles can follow an older schema. Validate their syntax,
            # then let conflict/--force handling preserve and replace them.
            parse_toml(target, prior)
    config = dest / "config.toml"
    config_existing = read_regular(config)
    if config_existing is not None:
        parse_toml(config, config_existing)
    routing_policy = dest / "agent-routing.json"
    routing_existing = read_regular(routing_policy)
    if routing_existing is not None:
        validate_routing_policy(routing_policy, routing_existing)
    override = dest / "AGENTS.override.md"
    if override.is_symlink():
        fail(f"refusing symlink: {override}")

    desired = dict(sources)
    desired[dest / "AGENTS.md"] = merge_instructions(existing[dest / "AGENTS.md"], sources[dest / "AGENTS.md"], dest / "AGENTS.md")
    if config_existing is None:
        desired[config] = config_source
    elif args.configure_routing:
        desired[config] = configure_routing_defaults(config, config_existing)
    if routing_existing is None:
        desired[routing_policy] = ROUTING_POLICY

    changes: list[tuple[Path, bytes, bytes | None]] = []
    for target, data in desired.items():
        prior = existing.get(target, config_existing if target == config else None)
        if prior != data:
            changes.append((target, data, prior))
    conflicts = [path for path, _, prior in changes
                 if prior is not None and path.name not in ("AGENTS.md", "config.toml", "agent-routing.json")]
    if conflicts and not args.force:
        fail("differing managed files (use --force): " + ", ".join(map(str, conflicts)))

    actions = []
    for path, _, prior in changes:
        if prior is not None:
            actions.append(f"backup {path}")
        actions.append(f"write {path}")
    helper = dest / "bin" / "pr-status"
    if existing[helper] is not None and not os.access(helper, os.X_OK):
        actions.append(f"chmod +x {helper}")
    if override.exists():
        print(f"warning: {override} shadows global AGENTS.md; left untouched", file=sys.stderr)
    if args.dry_run:
        for action in actions:
            print(action)
        return 0

    for directory in (dest, dest / "agents", dest / "bin"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path, data, prior in changes:
        if prior is not None:
            backup = create_backup(path, prior)
            print(f"backup {path} -> {backup}")
        mode = 0o700 if path.name == "pr-status" else 0o600
        atomic_write(path, data, mode)
        print(f"write {path}")
    if helper.exists() and not os.access(helper, os.X_OK):
        os.chmod(helper, stat.S_IMODE(helper.stat().st_mode) | stat.S_IXUSR)
        print(f"chmod +x {helper}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
