#!/usr/bin/env python3
"""Install the bundled Codex orchestration files.

Usage: install.py [--force] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
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
    starts = [i for i in range(len(data)) if data.startswith(START, i)]
    ends = [i for i in range(len(data)) if data.startswith(END, i)]
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        fail(f"invalid managed markers in {path}")
    end = ends[0] + len(END)
    return data[starts[0]:end]


def merge_instructions(existing: bytes | None, source: bytes, path: Path) -> bytes:
    block = managed_block(source, Path("source AGENTS.md"))
    if existing is None:
        return source
    starts = [i for i in range(len(existing)) if existing.startswith(START, i)]
    ends = [i for i in range(len(existing)) if existing.startswith(END, i)]
    if not starts and not ends:
        return existing + (b"" if not existing or existing.endswith(b"\n") else b"\n") + block
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        fail(f"invalid managed markers in {path}")
    end = ends[0] + len(END)
    return existing[:starts[0]] + block + existing[end:]


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
    assert config_source is not None and helper_source is not None
    sources[dest / "bin" / "pr-status"] = helper_source
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
    override = dest / "AGENTS.override.md"
    if override.is_symlink():
        fail(f"refusing symlink: {override}")

    desired = dict(sources)
    desired[dest / "AGENTS.md"] = merge_instructions(existing[dest / "AGENTS.md"], sources[dest / "AGENTS.md"], dest / "AGENTS.md")
    if config_existing is None:
        desired[config] = config_source

    changes: list[tuple[Path, bytes, bytes | None]] = []
    for target, data in desired.items():
        prior = existing.get(target, config_existing if target == config else None)
        if prior != data:
            changes.append((target, data, prior))
    conflicts = [path for path, _, prior in changes if prior is not None and path.name != "AGENTS.md"]
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
