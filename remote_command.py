#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

# Allow direct use from a source checkout without installing the package.
source_directory = Path(__file__).resolve().parent / "src"
if source_directory.is_dir():
    sys.path.insert(0, str(source_directory))

from remote_command_server.client_cli import main


if __name__ == "__main__":
    raise SystemExit(main())

