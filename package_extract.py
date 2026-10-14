#!/usr/bin/env python3
"""Download missing RPM or DEB dependencies, then merge payloads into one directory.

Uses the system's DNF or APT command-line tools, not Python bindings. The JSON
state's 'installed' value means "extracted to the output", NOT installed into
RPMDB or dpkg's package database.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

STATE_NAME = '.package-flat-state.json'
APT_INST = re.compile(r'^Inst\s+(\S+)(?:\s+\[[^\]]*\])?\s+\(([^\s()]+)(?:\s|\))')
SIMPLE_APT_SPEC = re.compile(r'^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?$', re.IGNORECASE)


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def checked_text(command, **kwargs):
    completed = run(command, text=True, capture_output=True, **kwargs)
    return completed.stdout.strip()


def detect_backend(selected):
    if selected != 'auto':
        return selected
    has_dnf = shutil.which('dnf') is not None
    has_apt = shutil.which('apt-get') is not None
    if not has_dnf and not has_apt:
        raise RuntimeError('Neither dnf nor apt-get was found in PATH')
    if has_dnf and not has_apt:
        return 'dnf'
    if has_apt and not has_dnf:
        return 'apt'

    # A system may have both package managers installed. Prefer its native one.
    os_info = {}
    try:
        for line in Path('/etc/os-release').read_text(encoding='utf-8').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                os_info[key] = value.strip('"\'')
    except OSError:
        pass
    ids = {os_info.get('ID', ''), *os_info.get('ID_LIKE', '').split()}
    if ids & {'debian', 'ubuntu', 'linuxmint', 'pop', 'kali'}:
        return 'apt'
    if ids & {'rhel', 'fedora', 'centos', 'rocky', 'almalinux', 'ol', 'amzn', 'suse'}:
        return 'dnf'
    raise RuntimeError('Both dnf and apt-get exist; use --backend dnf or --backend apt')


def read_state(path):
    if not path.exists():
        return {'format': 1, 'packages': {}}
    with path.open(encoding='utf-8') as f:
        state = json.load(f)
    if (not isinstance(state, dict) or state.get('format') != 1
            or not isinstance(state.get('packages'), dict)):
        raise ValueError(f'Invalid state file: {path}')
    return state


def save_state(path, state):
    fd, temp = tempfile.mkstemp(prefix='.package-flat-state-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def identical_file(a, b):
    if a.stat().st_size != b.stat().st_size:
        return False
    with a.open('rb') as x, b.open('rb') as y:
        while True:
            xx, yy = x.read(1024 * 1024), y.read(1024 * 1024)
            if xx != yy:
                return False
            if not xx:
                return True


def merge_tree(stage, dest):
    """Preflight collisions, then place contents into a shared directory.

    A failure such as ENOSPC after the preflight can leave partial output; the
    state record is only written once this call returns successfully.
    """
    entries = sorted(stage.rglob('*'), key=lambda p: (len(p.relative_to(stage).parts), str(p)))
    for src in entries:
        relative = src.relative_to(stage)
        target = dest / relative
        current = dest
        stage_parent = stage
        for part in relative.parts[:-1]:
            current = current / part
            stage_parent = stage_parent / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise RuntimeError(f'Unsafe destination parent: {current}')
            if stage_parent.is_symlink():
                raise RuntimeError(f'Payload contains a symlinked parent: {stage_parent}')
        if src.is_symlink():
            if target.is_symlink():
                if os.readlink(src) != os.readlink(target):
                    raise RuntimeError(f'Conflicting symlink: {target}')
            elif target.exists():
                raise RuntimeError(f'Conflicting path: {target}')
        elif src.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise RuntimeError(f'Conflicting directory: {target}')
        elif src.is_file():
            if target.is_symlink() or (target.exists() and
                                       (not target.is_file() or not identical_file(src, target))):
                raise RuntimeError(f'Conflicting file: {target}')
        else:
            raise RuntimeError(f'Unsupported payload entry: {src}')

    for src in entries:
        target = dest / src.relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            if not target.is_symlink():
                target.symlink_to(os.readlink(src))
        elif src.is_dir():
            target.mkdir(exist_ok=True)
        elif not target.exists():
            shutil.copy2(src, target)


def extract_rpm(archive, output):
    with tempfile.TemporaryDirectory(prefix='pkg-flat-rpm-stage-') as tmp:
        stage = Path(tmp)
        producer = subprocess.Popen(['rpm2cpio', str(archive)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        try:
            consumer = subprocess.run(['cpio', '-idm', '--quiet', '--no-absolute-filenames'],
                                      cwd=stage, stdin=producer.stdout, capture_output=True)
        finally:
            producer.stdout.close()
        err = producer.stderr.read().decode(errors='replace')
        producer.stderr.close()
        code = producer.wait()
        if code or consumer.returncode:
            raise RuntimeError(f'Failed to unpack {archive}: {err}\n'
                               + consumer.stderr.decode(errors='replace'))
        merge_tree(stage, output)


def extract_deb(archive, output):
    with tempfile.TemporaryDirectory(prefix='pkg-flat-deb-stage-') as tmp:
        stage = Path(tmp)
        run(['dpkg-deb', '--extract', str(archive), str(stage)])
        merge_tree(stage, output)


def rpm_metadata(path):
    fields = checked_text(['rpm', '-qp', '--qf',
                           '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}',
                           str(path)]).split('\t')
    if len(fields) != 5:
        raise ValueError(f'Invalid RPM metadata: {path}')
    name, epoch, version, release, arch = fields
    ident = f'{name}-{epoch}:{version}-{release}.{arch}'
    return ident, {'name': name, 'epoch': epoch, 'version': version, 'release': release,
                   'arch': arch, 'format': 'rpm', 'status': 'installed',
                   'installation_scope': 'extracted-only'}


def rpm_installed():
    lines = checked_text(['rpm', '-qa', '--qf', '%{NAME}\t%{ARCH}\n']).splitlines()
    return {(name, arch) for line in lines if '\t' in line
            for name, arch in [line.split('\t', 1)]}


def process_dnf(args, output, state_file, state):
    for cmd in ('dnf', 'rpm', 'rpm2cpio', 'cpio'):
        if not shutil.which(cmd):
            raise RuntimeError(f'DNF backend requires command: {cmd}')
    arch = platform.machine()  # uname -m
    allowed = {arch, 'noarch'}
    print(f'RPM architecture: {arch} (also accepting noarch)', flush=True)
    installed = rpm_installed()
    with tempfile.TemporaryDirectory(prefix='pkg-flat-rpm-') as temp:
        command = ['dnf', 'download', '--resolve', '--arch', f'{arch},noarch',
                   '--downloaddir', temp, '--', *args.packages]
        print('Running:', ' '.join(command), flush=True)
        run(command)
        archives = sorted(Path(temp).glob('*.rpm'))
        checked = []
        for archive in archives:
            identity, record = rpm_metadata(archive)
            if record['arch'] not in allowed:
                raise RuntimeError(f'Unexpected RPM architecture: {archive} ({record["arch"]})')
            checked.append((archive, identity, record))
        for index, (archive, identity, record) in enumerate(checked, 1):
            if (record['name'], record['arch']) in installed:
                print(f'[{index}/{len(checked)}] Already installed in RPMDB: {identity}')
                continue
            if identity in state['packages']:
                print(f'[{index}/{len(checked)}] Already extracted: {identity}')
                continue
            print(f'[{index}/{len(checked)}] Extracting RPM: {identity}', flush=True)
            extract_rpm(archive, output)
            if args.keep_packages:
                saved = output / '.rpm-downloads'
                saved.mkdir(exist_ok=True)
                shutil.copy2(archive, saved / archive.name)
            state['packages'][identity] = record
            save_state(state_file, state)
    print(f'DNF finished. Output: {output}')


def apt_installed():
    """Map (binary package name, dpkg architecture) -> installed version."""
    installed = {}
    format_string = '${Package}\t${Architecture}\t${db:Status-Abbrev}\t${Version}\n'
    output = checked_text(['dpkg-query', '-W', f'-f={format_string}'])
    for line in output.splitlines():
        fields = line.split('\t')
        if len(fields) != 4:
            continue
        name, arch, status, version = fields
        if len(status) >= 2 and status[1] == 'i':
            if ':' in name:
                name = name.rsplit(':', 1)[0]
            installed[(name, arch)] = version
    return installed


def apt_name_arch(name, native_arch):
    if ':' not in name:
        return name, native_arch
    base, arch = name.rsplit(':', 1)
    if arch == 'native':
        arch = native_arch
    return base, arch


def apt_identity(name, version, arch):
    return f'deb:{name}:{arch}={version}'


def apt_metadata(path):
    template = '${Package}\t${Version}\t${Architecture}'
    fields = checked_text(['dpkg-deb', '--show', f'--showformat={template}', str(path)]).split('\t')
    if len(fields) != 3:
        raise ValueError(f'Invalid DEB metadata: {path}')
    name, version, arch = fields
    identity = apt_identity(name, version, arch)
    return identity, {'name': name, 'version': version, 'arch': arch, 'format': 'deb',
                      'status': 'installed', 'installation_scope': 'extracted-only'}


def process_apt(args, output, state_file, state):
    for cmd in ('apt-get', 'dpkg-query', 'dpkg', 'dpkg-deb'):
        if not shutil.which(cmd):
            raise RuntimeError(f'APT backend requires command: {cmd}')
    native = checked_text(['dpkg', '--print-architecture'])
    if not native:
        raise RuntimeError('dpkg did not report an architecture')
    allowed = {native, 'all'}
    print(f'DEB architecture: {native} (uname -m: {platform.machine()}; also accepting all)',
          flush=True)
    installed = apt_installed()

    # An already-installed requested package is out of scope even if APT would
    # otherwise propose a newer candidate for it.
    requested = []
    for spec in args.packages:
        base_spec = spec.split('=', 1)[0].split('/', 1)[0]
        if SIMPLE_APT_SPEC.fullmatch(base_spec):
            name, arch = apt_name_arch(base_spec, native)
            if arch not in allowed:
                raise RuntimeError(f'Unsupported requested DEB architecture: {spec} ({arch})')
            if (name, arch) in installed or (name, 'all') in installed:
                print(f'Already installed, skipping requested package: {spec}', flush=True)
                continue
        requested.append(spec)
    if not requested:
        print('All requested packages are already installed. Nothing to extract.')
        return

    apt_options = ['-o', f'APT::Architecture={native}',
                   '-o', f'APT::Architectures={native}']
    cmd = ['apt-get', *apt_options, '-s', '--no-install-recommends', '--no-remove',
           'install', '--', *requested]
    print('Resolving with:', ' '.join(cmd), flush=True)
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    resolution = run(cmd, text=True, capture_output=True, env=env)
    selected = []
    for line in resolution.stdout.splitlines():
        if line.startswith('Remv '):
            raise RuntimeError('APT resolution would remove installed packages: ' + line)
        if line.startswith('Inst '):
            match = APT_INST.match(line)
            if not match:
                raise RuntimeError(f'Cannot parse APT solver output: {line}')
            spec_name, version = match.groups()
            name, arch = apt_name_arch(spec_name, native)
            if arch not in allowed:
                raise RuntimeError(f'APT selected foreign architecture {arch}: {line}')
            # APT can choose upgrades for system packages to satisfy dependencies.
            # Reject that plan, rather than falsely claiming extraction is enough.
            current = installed.get((name, arch)) or installed.get((name, 'all'))
            if current is not None:
                raise RuntimeError(f'APT requires upgrading installed {name} '
                                   f'from {current} to {version}; installed packages '
                                   'are excluded, so cannot complete safely')
            if (name, version) not in selected:
                selected.append((name, version))
    if not selected:
        print('APT reports no new packages required.')
        return

    to_download = []
    for name, version in selected:
        if (apt_identity(name, version, native) in state['packages']
                or apt_identity(name, version, 'all') in state['packages']):
            print(f'Already extracted, skipping download: {name}={version}', flush=True)
            continue
        to_download.append((name, version))
    if not to_download:
        print('Everything resolved by APT is already recorded as extracted.')
        return

    with tempfile.TemporaryDirectory(prefix='pkg-flat-deb-') as temp:
        # Let APT's restricted _apt user create files rather than downloading
        # unsandboxed as root in root-invoked runs.
        if os.geteuid() == 0:
            try:
                apt_user = pwd.getpwnam('_apt')
                os.chown(temp, apt_user.pw_uid, apt_user.pw_gid)
            except (KeyError, OSError):
                pass
        specs = [f'{name}={version}' for name, version in to_download]
        cmd = ['apt-get', *apt_options, 'download', '--', *specs]
        print('Downloading with:', ' '.join(cmd), flush=True)
        run(cmd, cwd=temp, env=env)
        archives = sorted(Path(temp).glob('*.deb'))
        checked = []
        actual = set()
        requested_pairs = set(to_download)
        for archive in archives:
            identity, record = apt_metadata(archive)
            pair = (record['name'], record['version'])
            if record['arch'] not in allowed:
                raise RuntimeError(f'Unexpected DEB architecture: {archive} ({record["arch"]})')
            if pair not in requested_pairs:
                raise RuntimeError(f'Downloaded unexpected DEB: {archive} ({identity})')
            actual.add(pair)
            checked.append((archive, identity, record))
        if actual != requested_pairs:
            raise RuntimeError(f'APT did not download expected packages: {sorted(requested_pairs - actual)}')
        for index, (archive, identity, record) in enumerate(checked, 1):
            if identity in state['packages']:
                print(f'[{index}/{len(checked)}] Already extracted: {identity}')
                continue
            print(f'[{index}/{len(checked)}] Extracting DEB: {identity}', flush=True)
            extract_deb(archive, output)
            if args.keep_packages:
                saved = output / '.deb-downloads'
                saved.mkdir(exist_ok=True)
                shutil.copy2(archive, saved / archive.name)
            state['packages'][identity] = record
            save_state(state_file, state)
    print(f'APT finished. Output: {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packages', nargs='+', help='Package name(s) or package specifications')
    parser.add_argument('-o', '--output', type=Path, required=True, help='Shared extraction root')
    parser.add_argument('--state', type=Path, help='JSON state (default: OUTPUT/.package-flat-state.json)')
    parser.add_argument('--backend', choices=('auto', 'dnf', 'apt'), default='auto',
                        help='Package manager detection (default: auto)')
    parser.add_argument('--keep-packages', '--keep-rpms', action='store_true', dest='keep_packages',
                        help='Keep downloaded RPM/DEB archives in the output directory')
    args = parser.parse_args()

    backend = detect_backend(args.backend)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Retain compatibility with the older RPM-only script's default state path.
    if args.state:
        state_file = args.state.expanduser().resolve()
    elif (output / STATE_NAME).exists():
        state_file = output / STATE_NAME
    elif (output / '.rpm-flat-state.json').exists():
        state_file = output / '.rpm-flat-state.json'
    else:
        state_file = output / STATE_NAME
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = read_state(state_file)
    print(f'Package manager: {backend}', flush=True)
    if backend == 'dnf':
        process_dnf(args, output, state_file, state)
    else:
        process_apt(args, output, state_file, state)
    print(f'State file: {state_file}')


def cli() -> int:
    """Use the same exit status and error reporting for all CLI entry points."""
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli())
