# run.py
"""Compatibility entrypoint to keep `python run.py` working.

The new recommended way to start the app is `python -m inv`.
"""

from cli import main

if __name__ == "__main__":
    raise SystemExit(main())