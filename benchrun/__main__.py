"""Entry point for ``python -m benchrun``."""

import sys

if __name__ == "__main__":
    try:
        from benchrun.cli import main

        sys.exit(main())
    except KeyboardInterrupt:  # Stopped while still starting up, with nothing connected.
        sys.exit(3)
