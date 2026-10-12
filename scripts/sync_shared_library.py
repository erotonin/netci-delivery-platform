#!/usr/bin/env python3
"""Copy netCI's CI tooling into the Jenkins shared library as `libraryResource`s.

The pipeline used to run `scripts/netci_callback.py` and `templates/<t>/scripts/ci/*.sh`
out of the checked-out repository. That only worked because every lab module built from
the netCI repository itself; a real application repository carries none of it, so the
first callback of a real build failed with "No such file". The library now brings its
own tooling (vars/netciTooling.groovy writes it to the agent), and this script is how the
library's copy is kept identical to the source of truth under `templates/` and
`scripts/` -- the same arrangement as backend/schema.sql and its migrations.

    python scripts/sync_shared_library.py           # rewrite resources/netci/tooling
    python scripts/sync_shared_library.py --check   # fail if it has drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "jenkins" / "shared-library" / "resources" / "netci" / "tooling"
MANIFEST = DESTINATION / "MANIFEST"


def sources() -> list[Path]:
    """Every file the tooling consists of, relative to the repository root.

    The layout is kept as it is in the repository because the scripts rely on it: each
    computes `repo_root` as four directories above itself, and the Kubernetes template
    reuses the container template's scripts through that root.
    """

    files = sorted(
        path.relative_to(ROOT)
        for path in (ROOT / "templates").glob("*/scripts/ci/*")
        if path.is_file()
    )
    files.append(Path("scripts") / "netci_callback.py")
    return files


def expected() -> dict[str, bytes]:
    tree = {str(path): (ROOT / path).read_bytes() for path in sources()}
    tree["MANIFEST"] = ("\n".join(str(path) for path in sources()) + "\n").encode()
    return tree


def actual() -> dict[str, bytes]:
    if not DESTINATION.is_dir():
        return {}
    return {
        str(path.relative_to(DESTINATION)): path.read_bytes()
        for path in DESTINATION.rglob("*")
        if path.is_file()
    }


def write() -> int:
    wanted = expected()
    for stale in set(actual()) - set(wanted):
        (DESTINATION / stale).unlink()
    for relative, content in wanted.items():
        target = DESTINATION / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    print(f"shared library tooling written: {len(wanted) - 1} files")
    return 0


def check() -> int:
    wanted, found = expected(), actual()
    drift = sorted(
        {name for name in set(wanted) | set(found) if wanted.get(name) != found.get(name)}
    )
    if drift:
        print(
            "jenkins/shared-library/resources/netci/tooling is stale -- run "
            "`python scripts/sync_shared_library.py`:\n  " + "\n  ".join(drift),
            file=sys.stderr,
        )
        return 1
    print(f"shared library tooling matches {len(wanted) - 1} source files")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if the library copy has drifted")
    return check() if parser.parse_args().check else write()


if __name__ == "__main__":
    raise SystemExit(main())
