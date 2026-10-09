"""python -m src.server.scheduler: the scheduler service (see service.py)."""

# Guarded: face detection's subprocess is started with "spawn", which imports
# this module again in the child; unguarded, the child would start a second
# scheduler (and revoke the first's API key).
if __name__ == "__main__":
    from src.server.scheduler.service import main

    raise SystemExit(main())
