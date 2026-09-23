import os
import sys

if not __package__:
    # Run by FILE path (`python3 <copy>/miragen_hook/__main__.py …`, what the
    # daemon-written hook and MCP entries do): make THIS copy's package the
    # one imported — ahead of the working directory (a miragen checkout),
    # PYTHONPATH and site-packages — and stop this directory's own modules
    # from being importable as top-level names.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from miragen_hook.client import main

raise SystemExit(main())
