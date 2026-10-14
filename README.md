# rootgraft

Extract missing RPM or DEB dependencies into a shared directory, then run
commands with that directory layered over the host filesystem using Bubblewrap.
Requires Linux and Python 3.9 or newer.

## Install

From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

For development, use `python -m pip install -e .` instead. Activating the virtual
environment makes both CLI commands available from any directory. Without
activation, invoke `.venv/bin/rootgraft` or `.venv/bin/package-extract` directly.

## Use

```sh
rootgraft --help
package-extract --help

# Extract missing packages and dependencies into ./root.
package-extract --output ./root PACKAGE_NAME

# Preview the overlay operations without launching Bubblewrap.
rootgraft --dry-run ./root -- /bin/sh

# Run a command with the overlay.
rootgraft ./root -- /bin/sh
```

Put `rootgraft` options before the overlay path; arguments after the path are
passed to the command. If no command is supplied, `/bin/sh` is used.

The overlay runner requires `bwrap` with support for `--overlay-src` and
`--tmp-overlay`. Package extraction uses system tools: `apt-get`, `dpkg-query`,
`dpkg`, and `dpkg-deb` for DEB packages, or `dnf` with its download command, `rpm`,
`rpm2cpio`, and `cpio` for RPM packages. Install these tools separately using
your system package manager.

The extraction state records packages copied into the output directory;
extraction does not install them into the system package database.
