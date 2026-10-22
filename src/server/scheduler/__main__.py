"""python -m src.server.scheduler: the scheduler service (see service.py)."""

# Guarded like any entry point: importing it (a tool, a test) starts nothing.
if __name__ == "__main__":
    from src.shared import secrets_env

    # Not dumpable before the service is imported, as its unit starts it (entry() does it too, for lumiverb-scheduler).
    secrets_env.not_dumpable()
    from src.server.scheduler.service import entry

    entry()
