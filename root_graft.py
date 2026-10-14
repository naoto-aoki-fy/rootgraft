#!/usr/bin/env python3
"""Run a command in bwrap with a mount-aware overlay of /.

Directory overlays use temporary writable OverlayFS. Files are read-only bind
mounted. Mountpoints below a directory force recursive processing so that those
mounts are not hidden by an overlay on their ancestor.
"""

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape_mountinfo(s: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), s)


def mountpoints() -> set[str]:
    points = set()
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            fields = line.split(' - ', 1)[0].split()
            points.add(os.path.normpath(unescape_mountinfo(fields[4])))
    return points


def descendants(path: str, mounts: set[str]) -> bool:
    prefix = path.rstrip('/') + '/'
    return any(p.startswith(prefix) for p in mounts)


def inside(path: str, parent: str) -> bool:
    return os.path.commonpath((path, parent)) == parent


def build_mounts(overlay: Path, mounts: set[str]) -> tuple[list[str], list[str]]:
    args: list[str] = []
    report: list[str] = []
    overlay_abs = str(overlay)

    def walk(src: Path, dest: str) -> None:
        source = str(src)
        src_mode = src.lstat().st_mode
        if not stat.S_ISDIR(src_mode):
            raise ValueError(f'Expected directory: {src}')

        # Avoid OverlayFS's unsupported ancestor/descendant source pairing.
        # The destination is also a lowerdir, so it must not contain src.
        # (This check is intentionally strict, rather than creating a subtly
        # incorrect nested overlay.)
        nested_source = inside(source, dest)
        split = (dest == '/' or descendants(dest, mounts)
                 or descendants(source, mounts) or nested_source)
        if not split:
            if os.path.lexists(dest):
                if not stat.S_ISDIR(os.lstat(dest).st_mode):
                    raise ValueError(f'Overlay directory conflicts with non-directory: {dest}')
                args.extend(['--overlay-src', dest,
                             '--overlay-src', source, '--tmp-overlay', dest])
            else:
                # OverlayFS only needs one lowerdir for this new directory.
                args.extend(['--dir', dest,
                             '--overlay-src', source, '--tmp-overlay', dest])
            report.append(f'OVERLAY {source} -> {dest}')
            return

        # If a destination directory does not exist, construct it before
        # creating its children. Never overlay an ancestor of live mounts.
        if dest != '/' and not os.path.lexists(dest):
            args.extend(['--dir', dest])
            report.append(f'MKDIR   {dest}')
        elif dest != '/' and not stat.S_ISDIR(os.lstat(dest).st_mode):
            raise ValueError(f'Cannot recurse into non-directory: {dest}')

        with os.scandir(src) as entries:
            children = sorted(entries, key=lambda e: e.name)
        for entry in children:
            child_src = src / entry.name
            child_dest = os.path.join(dest, entry.name)
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                walk(child_src, child_dest)
            elif stat.S_ISREG(mode):
                # Reject destination symlinks to avoid resolving outside the
                # intended destination; do not overwrite live mountpoints.
                if os.path.islink(child_dest):
                    raise ValueError(f'Destination is a symlink: {child_dest}')
                if child_dest in mounts:
                    raise ValueError(f'Destination is a mountpoint: {child_dest}')
                if os.path.lexists(child_dest) and not stat.S_ISREG(os.lstat(child_dest).st_mode):
                    raise ValueError(f'Destination is not a regular file: {child_dest}')
                args.extend(['--ro-bind', str(child_src), child_dest])
                report.append(f'BIND    {child_src} -> {child_dest}')
            else:
                # Symlinks, sockets, devices, FIFOs need independent semantics.
                # Refuse to silently dereference or misrepresent them.
                raise ValueError(f'Unsupported overlay entry: {child_src}')

    walk(overlay, '/')
    return args, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('overlay', type=Path, help='directory to layer over /')
    parser.add_argument('--dry-run', action='store_true', help='print operations without running bwrap')
    parser.add_argument('--bwrap', default='bwrap', help='bwrap executable (default: bwrap)')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='command after --')
    ns = parser.parse_args()

    src = ns.overlay.resolve(strict=True)
    if not src.is_dir():
        parser.error('overlay must be a directory')
    command = ns.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        command = ['/bin/sh']

    try:
        operations, report = build_mounts(src, mountpoints())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    invocation = [
        ns.bwrap,
        '--die-with-parent',
        '--bind', '/', '/',
        *operations,
        "--dev-bind", "/dev", "/dev",
        '--', *command]
    if ns.dry_run:
        import shlex
        print('\n'.join(report))
        print('\nCOMMAND:')
        print(shlex.join(invocation))
        return 0
    if not shutil.which(ns.bwrap):
        parser.error(f'bwrap not found: {ns.bwrap}')
    return subprocess.call(invocation)


if __name__ == '__main__':
    sys.exit(main())
