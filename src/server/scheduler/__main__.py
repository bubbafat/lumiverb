"""python -m src.server.scheduler: the scheduler service (see service.py)."""

# Guarded like any entry point: importing it (a tool, a test) starts nothing.
if __name__ == "__main__":
    from src.server.scheduler.service import entry
    from src.shared.logging_config import escape_log_lines

    escape_log_lines()
    entry()
