#!/usr/bin/env python3
"""Run bounded read-only Claude lookup comparisons and retain reviewable results."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import functools
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


def resolve_executable(name, exts=None):
    """Absolute path of `name` from PATH, never from the current directory; None if absent.

    A bare name given to CreateProcess (and shutil.which) is looked up in the current
    directory first on Windows, so a repository could plant `claude.exe`. Empty, relative
    and current-directory PATH entries are skipped for the same reason. `exts` narrows
    the Windows extensions tried (PATHEXT by default)."""
    if os.name != 'nt':
        exts = ['']
    elif exts is None:
        exts = [e for e in os.environ.get('PATHEXT', '.COM;.EXE;.BAT;.CMD').split(';') if e]
    cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry) or os.path.normcase(os.path.realpath(entry)) == cwd:
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


@functools.cache
def executable():
    """Resolved once per process; a failed lookup is not cached."""
    # Only .exe/.com launch directly, so on Windows those are searched for first, along the
    # whole PATH: a stale claude.cmd earlier on it must not hide a real claude.exe later.
    native = resolve_executable('claude', ('.exe', '.com'))
    if native:
        return native
    wrapper = None if os.name != 'nt' else resolve_executable('claude')
    if not wrapper:
        raise RuntimeError('Claude Code is not installed on PATH (the current directory is never searched)')
    # A .bat/.cmd wrapper runs through cmd.exe, which would execute a `"` followed by `&`
    # in the prompt; the npm shim is used only to find the native binary beside it.
    native = Path(wrapper).parent / 'node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    if native.is_file():
        return str(native)
    raise RuntimeError('Cannot resolve native claude.exe; refusing a shell-interpolated launch')


def grade(task, answer):
    """Which required terms the answer names, and whether enough of them do.

    Every term is needed unless the task sets `min_hits`; no task passes without a hit."""
    hits = {term: term.casefold() in answer.casefold() for term in task['required']}
    return hits, sum(hits.values()) >= max(1, task.get('min_hits', len(hits)))


def task_problem(task):
    """Why a task cannot be graded, or None. Without a required term, or with a `min_hits`
    below 1, every answer would pass; a term repeated (ignoring case) is graded once, so
    its task could never pass. Either is a mistake in the task file. A task also needs a
    non-empty string `id` and `prompt`, checked before any run starts."""
    required = task.get('required') if isinstance(task, dict) else None
    if not isinstance(required, list) or not required or \
            not all(isinstance(term, str) and term.strip() for term in required):
        return 'required must list at least one non-empty term'
    if len({term.casefold() for term in required}) != len(required):
        return 'required terms must differ (ignoring case)'
    needed = task.get('min_hits', len(required))
    if isinstance(needed, bool) or not isinstance(needed, int) or not 1 <= needed <= len(required):
        return f'min_hits must be a whole number from 1 to {len(required)}'
    for field in ('id', 'prompt'):
        if not isinstance(task.get(field), str) or not task[field].strip():
            return f'{field} must be a non-empty string'
    return None


SNAPSHOT_FILES = 20000           # untracked and ignored files fingerprinted per snapshot
SNAPSHOT_HASH_BYTES = 64 << 20   # content hashed per snapshot; past it, size and mtime stand in
SUBMODULE_DEPTH = 3              # submodules inside submodules that are snapshotted; deeper ones flag `truncated`
SUBMODULE_COUNT = 50             # submodules snapshotted per repository; more of them flag `truncated`
# Ignored paths that concurrent sessions and hooks rewrite while a run reads the repo; left out of
# the snapshot, so only a write under one of these passes. gitignore-style: a pattern without an
# inner slash matches at any depth, a trailing slash only a directory, others from the work-tree root.
VOLATILE_IGNORED = ('__pycache__/', '*.pyc', '.hunch/', 'relay/state/', 'relay/jev-log*',
                    'relay/handoffs/', '.hunch-cache/', '.test-tmp/', '.playwright-mcp/')


def is_volatile(path):
    """Whether `path` (relative to the work tree, `/`-separated) lies under VOLATILE_IGNORED."""
    parts = path.rstrip('/').split('/')
    for pattern in VOLATILE_IGNORED:
        name = pattern.rstrip('/')
        # A directory pattern needs a directory: any part but the last, unless the path is one.
        names = parts if not pattern.endswith('/') or path.endswith('/') else parts[:-1]
        if '/' in name:
            depth = name.count('/') + 1
            if len(names) >= depth and fnmatch.fnmatchcase('/'.join(names[:depth]), name):
                return True
        elif any(fnmatch.fnmatchcase(part, name) for part in names):
            return True
    return False


def drop_volatile(status):
    """A `status -z` listing without its ignored (`!!`) entries under VOLATILE_IGNORED."""
    kept, fields = [], iter(status.split(b'\0'))
    for field in fields:
        if b'R' in field[:2] or b'C' in field[:2]:
            kept += [field, next(fields, b'')]
        elif not (field[:3] == b'!! ' and is_volatile(os.fsdecode(field[3:]))):
            kept.append(field)
    return b'\0'.join(kept)


# The repository's own files that are fingerprinted (config, HEAD, hooks): how many hooks, and
# how many bytes of each file are hashed (its size is always part of the mark).
GIT_STATE_FILES = 200
GIT_STATE_BYTES = 1 << 20


def git_bytes(git, repo, *args):
    """stdout of a git command in `repo`, as bytes, or None when it fails."""
    try:
        result = subprocess.run([git, '--no-optional-locks', '-C', str(repo), *args],
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def loose_files(top, status):
    """A fingerprint of each untracked (`??`) or ignored (`!!`) file in a `status -z`
    listing: a content hash while the byte budget lasts, then size and mtime. Bounded to
    the first SNAPSHOT_FILES files; the flag says that bound was hit, so files past it were
    not looked at."""
    files, budget = {}, SNAPSHOT_HASH_BYTES
    fields = iter(status.split(b'\0'))
    for field in fields:
        if b'R' in field[:2] or b'C' in field[:2]:
            next(fields, None)  # a rename or copy carries its source as a second field
        if field[:3] not in (b'?? ', b'!! '):
            continue
        root = os.path.join(top, os.fsdecode(field[3:]))
        is_ignored = field[:3] == b'!! '
        paths = [root]
        if os.path.isdir(root) and not os.path.islink(root):
            # A folder git lists whole, such as a nested repository.
            paths = (os.path.join(folder, name) for folder, folders, names in os.walk(root)
                     for name in (folders.sort() or sorted(names)))
        for path in paths:
            rel = os.path.relpath(path, top).replace(os.sep, '/')
            if is_ignored and is_volatile(rel):
                continue
            if len(files) >= SNAPSHOT_FILES:
                return files, True
            try:
                info = os.lstat(path)
                mark = f'{info.st_size}:{info.st_mtime_ns}'
                if stat.S_ISREG(info.st_mode) and info.st_size <= budget:
                    budget -= info.st_size
                    with open(path, 'rb') as handle:
                        mark = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                mark = 'unreadable'
            files[rel] = mark
    return files, False


def file_mark(path):
    """Size and content hash of one small file of the repository, or why there is none."""
    try:
        with open(path, 'rb') as handle:
            return f'{os.path.getsize(path)}:{hashlib.sha256(handle.read(GIT_STATE_BYTES)).hexdigest()}'
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unreadable'


def folder_marks(root, marks, prefix=''):
    """Adds a mark for each file under `root` to `marks`, keyed `prefix` + its path, while
    there are fewer than GIT_STATE_FILES; whether that bound was hit."""
    for folder, folders, names in os.walk(root):
        folders.sort()
        for name in sorted(names):
            if len(marks) >= GIT_STATE_FILES:
                return True
            path = os.path.join(folder, name)
            marks[prefix + os.path.relpath(path, root).replace(os.sep, '/')] = file_mark(path)
    return False


# Files of the repository beside its refs that change what a later command does or shows while
# the work tree stays as it was: excludes, attributes, the sparse-checkout patterns, and the marks
# of a merge, cherry-pick, revert, rebase or bisect in progress (a folder is read file by file).
GIT_STATE_PATHS = ('info/exclude', 'info/attributes', 'info/sparse-checkout', 'MERGE_HEAD', 'MERGE_MSG',
                   'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'REBASE_HEAD', 'BISECT_LOG', 'rebase-merge',
                   'rebase-apply', 'sequencer')


def worktree_marks(worktrees, top):
    """A hash of a `worktree list --porcelain` listing without where the work tree at `top`
    points (its HEAD file and the refs are compared beside it); every other worktree's lines,
    where its HEAD points included, stand as they are, and all of them when none is at `top`."""
    blocks = []
    for block in worktrees.replace(b'\r\n', b'\n').split(b'\n\n'):
        lines = block.splitlines()
        if lines and os.path.normcase(os.fsdecode(lines[0][len(b'worktree '):])) == os.path.normcase(top):
            lines = [line for line in lines if not line.startswith((b'HEAD ', b'branch ', b'detached'))]
        blocks.append(b'\n'.join(lines))
    return hashlib.sha256(b'\n\n'.join(blocks)).hexdigest()


def git_state(git, repo, top):
    """What a write to the repository itself changes while the work tree stays as it was, or
    None when git cannot say: every ref and where HEAD points, the stash list, the worktree
    list (`worktree_marks`), the bytes of the config file, each hook file (honouring
    core.hooksPath), and each of GIT_STATE_PATHS. `hooks_truncated` says the hooks folder (or a
    folder of GIT_STATE_PATHS) holds more than GIT_STATE_FILES files, so some were not looked at."""
    refs = git_bytes(git, repo, 'for-each-ref', '--format=%(refname) %(objectname) %(symref)')
    stash = git_bytes(git, repo, 'stash', 'list', '--format=%H %gs')
    worktrees = git_bytes(git, repo, 'worktree', 'list', '--porcelain')
    names = ('HEAD', 'config', 'hooks', *GIT_STATE_PATHS)
    listed = git_bytes(git, repo, 'rev-parse', *(part for name in names for part in ('--git-path', name)))
    paths = os.fsdecode(listed or b'').splitlines()
    if refs is None or stash is None or worktrees is None or len(paths) != len(names):
        return None
    # A relative path is relative to the repository; an absolute one stands as it is.
    head, config, hooks, *others = (os.path.join(repo, path) for path in paths)
    marks = {}
    truncated = folder_marks(hooks, marks)
    files = {}
    for name, path in zip(GIT_STATE_PATHS, others):
        if os.path.isdir(path) and not os.path.islink(path):
            files[name + '/'] = 'folder'
            truncated = folder_marks(path, files, name + '/') or truncated
        else:
            files[name] = file_mark(path)
    return {'refs': f'{hashlib.sha256(refs).hexdigest()}:{file_mark(head)}',
            'stash': hashlib.sha256(stash).hexdigest(), 'config': file_mark(config),
            'hooks': marks, 'git_files': files, 'worktrees': worktree_marks(worktrees, top),
            'hooks_truncated': truncated}


def flagged_files(git, top):
    """The tracked files git does not look at in the work tree (skip-worktree or
    assume-unchanged, as `ls-files -v` tags them), so neither `status` nor `diff` shows an edit
    to them: a hash of the whole tagged list, so setting or clearing a flag is seen, and a
    content hash of each of the first GIT_STATE_FILES of them. None when git cannot say; the
    flag says some were not hashed (past the bound, or past SNAPSHOT_HASH_BYTES)."""
    listing = git_bytes(git, top, 'ls-files', '-v', '-z')
    if listing is None:
        return None
    flagged = [entry for entry in listing.split(b'\0') if entry and entry[:2] != b'H ']
    marks, budget, truncated = {}, SNAPSHOT_HASH_BYTES, len(flagged) > GIT_STATE_FILES
    for entry in flagged[:GIT_STATE_FILES]:
        rel = os.fsdecode(entry[2:])
        mark = 'missing'
        try:
            digest = hashlib.sha256()
            with open(os.path.join(top, rel), 'rb') as handle:
                for block in iter(lambda: handle.read(1 << 20), b''):
                    budget -= len(block)
                    if budget < 0:
                        truncated = True
                        break
                    digest.update(block)
            mark = digest.hexdigest()
        except FileNotFoundError:
            pass
        except OSError:
            mark = 'unreadable'
        marks[rel] = f'{os.fsdecode(entry[:1])}:{mark}'
    return {'flags': hashlib.sha256(b'\0'.join(flagged)).hexdigest(), 'flagged': marks}, truncated


def folder_names(top):
    """A hash of the names of the work tree's empty folders, which `git status` never lists
    (any other folder shows through what it holds), walking past `.git` and VOLATILE_IGNORED
    folders; whether the walk stopped at SNAPSHOT_FILES folders, so some were not looked at."""
    names, walked = [], 0
    for folder, folders, files in os.walk(top):
        walked += 1
        if walked > SNAPSHOT_FILES:
            return hashlib.sha256('\0'.join(names).encode('utf-8', 'surrogateescape')).hexdigest(), True
        rel = os.path.relpath(folder, top).replace(os.sep, '/')
        if not folders and not files:
            names.append(rel)
        folders[:] = sorted(name for name in folders if name != '.git'
                            and not is_volatile(('' if rel == '.' else rel + '/') + name + '/'))
    return hashlib.sha256('\0'.join(names).encode('utf-8', 'surrogateescape')).hexdigest(), False


def submodule_snapshots(git, top, depth):
    """A snapshot of the work tree of each submodule (gitlink) of the repository at `top`, keyed
    by its path, so a write inside one is seen however `status` shows it (a submodule already
    dirty prints the same line, and `submodule.<name>.ignore=all` prints none): the same
    snapshot as the repository's own, to SUBMODULE_DEPTH levels and SUBMODULE_COUNT submodules
    each. A submodule that is not initialised has no repository to snapshot, so the files
    in its folder are marked. Returns (snapshots, truncated), or None when one cannot be read."""
    listing = git_bytes(git, top, 'ls-files', '--stage', '-z')
    if listing is None:
        return None
    paths = [os.fsdecode(entry.split(b'\t', 1)[1]) for entry in listing.split(b'\0')
             if entry.startswith(b'160000 ') and b'\t' in entry]
    if not paths:
        return {}, False
    if depth >= SUBMODULE_DEPTH:
        return {}, True
    snapshots = {}
    for path in paths[:SUBMODULE_COUNT]:
        folder = os.path.join(top, path)
        if os.path.exists(os.path.join(folder, '.git')):
            snapshots[path] = snapshot(folder, depth + 1)
            if snapshots[path] is None:
                return None
        else:
            marks = {}
            snapshots[path] = {'uninitialised': marks, 'truncated': folder_marks(folder, marks)}
    return snapshots, len(paths) > SUBMODULE_COUNT


def tree_status(repo):
    """Snapshot of `repo`'s work tree, or None when it cannot be read: HEAD (a commit made
    during the run leaves the status clean), `git status --porcelain` with every untracked
    and ignored file, a hash of the unstaged and staged diffs (a second edit to an already
    modified file leaves its status line as it was), a fingerprint of each untracked
    or ignored file, the repository's own state (`git_state`: refs, stash, worktrees, config,
    hooks and the files beside them), which a write can change without touching a file of the
    work tree, the files git does not look at (`flagged_files`), and the empty folders
    (`folder_names`), and that of each submodule (`submodule_snapshots`); status and diff look
    inside submodules whatever their `ignore` setting."""
    return snapshot(repo, 0)


def snapshot(repo, depth):
    """`tree_status` of `repo`, a submodule `depth` levels down."""
    git = resolve_executable('git', ('.exe', '.com'))
    if not git:
        return None
    top = git_bytes(git, repo, 'rev-parse', '--show-toplevel')
    status = git_bytes(git, repo, 'status', '--porcelain', '-z', '--ignored', '--untracked-files=all',
                      '--ignore-submodules=none')
    diffs = [git_bytes(git, repo, 'diff', '--binary', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none', *staged)
             for staged in ((), ('--cached',))]
    if top is None or status is None or None in diffs:
        return None
    status = drop_volatile(status)
    top = os.fsdecode(top).rstrip('\r\n')
    files, truncated = loose_files(top, status)
    # A repository without a commit has no HEAD; that reads as '' and still compares.
    head = git_bytes(git, repo, 'rev-parse', '--verify', '--quiet', 'HEAD') or b''
    state = git_state(git, repo, top)
    flagged = flagged_files(git, top)
    subs = submodule_snapshots(git, top, depth)
    if state is None or flagged is None or subs is None:
        return None
    flags, flags_truncated = flagged
    submodules, subs_truncated = subs
    dirs, dirs_truncated = folder_names(top)
    # Hooks, flagged files and folders past their bounds were not compared either: the same flag reports it.
    truncated = state.pop('hooks_truncated') or flags_truncated or dirs_truncated or truncated or subs_truncated
    truncated = truncated or any(sub['truncated'] for sub in submodules.values())
    return {'head': head.decode('ascii', 'replace').strip(),
            'status': status.decode('utf-8', 'replace').replace('\0', '\n'),
            'diff': hashlib.sha256(b'\0'.join(diffs)).hexdigest(),
            'files': files, 'truncated': truncated, **state, **flags, 'dirs': dirs,
            'submodules': submodules}


def tree_changes(before, after):
    """What differs between two snapshots, for the record: the parts and the first files."""
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return 'git status'
    names = {'head': 'HEAD', 'status': 'git status', 'diff': 'tracked file content', 'refs': 'git refs',
             'stash': 'git stash', 'config': 'git config', 'hooks': 'git hooks', 'git_files': 'git state files',
             'worktrees': 'git worktrees', 'flags': 'skip-worktree or assume-unchanged flags',
             'flagged': 'skip-worktree or assume-unchanged file content', 'dirs': 'empty folders',
             'submodules': 'submodule work trees'}
    parts = [label for key, label in names.items() if before.get(key) != after.get(key)]
    old, new = before.get('files') or {}, after.get('files') or {}
    files = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    return ', '.join(parts + files[:5]) or 'git status'


def run_checked(repo, *args):
    """run_one, failed when the repo's working tree changed during the run or could not
    be compared: a lookup that writes is not a read-only pass."""
    before = tree_status(repo)
    record = run_one(repo, *args)
    after = tree_status(repo)
    if before is None or after is None:
        record['read_only_error'] = 'git status unavailable; the run is unverified as read-only'
    elif before != after:
        record['read_only_error'] = f'{tree_changes(before, after)} changed during the run'
    elif isinstance(after, dict) and after.get('truncated'):
        record['read_only_error'] = ('untracked and ignored files past the snapshot bound were not '
                                     'compared; the run is unverified as read-only')
    if 'read_only_error' in record:
        record['exit'] = record['exit'] or 1
    return record


def run_one(repo, task, model, budget):
    command = [executable(), '-p', task['prompt'], '--model', model, '--output-format', 'json',
               '--max-budget-usd', str(budget), '--no-session-persistence', '--restricted',
               '--strict-mcp-config', '--setting-sources', '', '--tools', 'Read,Grep,Glob']
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=180)
    except subprocess.TimeoutExpired:
        return {'task': task['id'], 'model': model, 'exit': 124, 'error': 'timeout'}
    record = {'task': task['id'], 'model': model, 'exit': result.returncode,
              'wall_ms': round((time.monotonic() - started) * 1000)}
    if result.returncode:
        record['error'] = result.stderr[-1000:]
        return record
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        output = None
    if isinstance(output, dict) and output.get('is_error'):
        # The CLI can exit 0 and still report its own failure (an API error, a spent
        # budget): that text is not an answer to grade.
        detail = output.get('subtype') or output.get('result') or ''
        record.update(exit=1, error=f'CLI reported an error: {str(detail)[:300]}'.rstrip(': '))
        return record
    answer = output.get('result') if isinstance(output, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        # The CLI exited 0 without an answer to grade: a failed run, not a pass.
        record.update(exit=1, error='non-JSON CLI output' if output is None else 'no answer in CLI output')
        return record
    usage = output.get('modelUsage')
    hits, met = grade(task, answer)
    record.update(actual_model=next(iter(usage), None) if isinstance(usage, dict) else None,
                  cost_usd=output.get('total_cost_usd'),
                  duration_ms=output.get('duration_ms'),
                  turns=output.get('num_turns'),
                  required_hits=hits, required_met=met,
                  answer=answer)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--tasks', type=Path, default=Path(__file__).resolve().parents[1] /
                        'benchmarks/claude-readonly.json')
    parser.add_argument('--models', nargs='+', default=['haiku', 'sonnet'])
    parser.add_argument('--budget', type=float, default=.20, help='USD maximum per task/model run')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True, help='Private JSON results file')
    args = parser.parse_args(argv)
    try:
        tasks = json.loads(args.tasks.read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError) as exc:  # RecursionError: nesting too deep
        parser.error(f'tasks file cannot be read as JSON: {exc}')
    if not (math.isfinite(args.budget) and args.budget > 0) or not 1 <= args.jobs <= 4:
        parser.error('budget must be a positive finite number and jobs must be 1-4')
    if not (isinstance(tasks, list) and tasks and all(isinstance(task, dict) for task in tasks)):
        parser.error('tasks file must be a non-empty JSON list of task objects')
    for task in tasks:
        problem = task_problem(task)
        if problem:
            parser.error(f"task {task.get('id') if isinstance(task, dict) else '?'}: {problem}")
    if args.output.exists():
        parser.error('output already exists; use a new file to preserve prior evidence')
    snapshot = tree_status(args.repo)
    if snapshot is None:
        parser.error('--repo must be a git work tree: git status is the read-only check')
    if isinstance(snapshot, dict) and snapshot.get('truncated'):
        parser.error(f'--repo has more than {SNAPSHOT_FILES} untracked and ignored files, past the '
                     'snapshot bound: every run would be unverified as read-only')
    work = [(task, model) for task in tasks for model in args.models]
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_checked, args.repo, task, model, args.budget): (task, model)
                   for task, model in work}
        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:  # one failed run must not discard the completed ones
                task, model = futures[future]
                record = {'task': task.get('id'), 'model': model, 'exit': 1,
                          'error': f'{type(exc).__name__}: {exc}'}
            results.append(record)
            print(f"{record['task']} {record['model']}: exit={record['exit']} "
                  f"cost={record.get('cost_usd')} hits={sum(record.get('required_hits', {}).values())}"
                  f"{'' if record.get('required_met', True) else ' (below the required hits)'}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
    # A run passes only if it exited 0 and its answer met the task's required hits.
    return 0 if all(r['exit'] == 0 and r.get('required_met') is True for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
