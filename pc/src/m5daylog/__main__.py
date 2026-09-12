"""Support ``python -m m5daylog`` alongside the ``m5daylog`` entrypoint."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
