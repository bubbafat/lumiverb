"""python -m src.server.api: uvicorn, as a Linux install's unit runs the API
(deploy-api.sh), with uvicorn's own arguments:

    python -m src.server.api src.server.api.main:app --host H --port P --workers 2

uvicorn's own process holds the env file's settings, secrets included, and
starts the workers: it's made not dumpable first (src/shared/secrets_env.py),
so the other processes of its user, a plug-in's among them, can't read its
environment or memory. The one other program it starts and keeps,
multiprocessing's resource tracker, is started here without the secrets.
Each worker makes itself not dumpable as it imports the app and, told so
here, forgets the secrets once its settings are read (main.py), so what it
starts has none. Nothing else differs from running uvicorn itself.
"""

from src.shared import secrets_env

secrets_env.not_dumpable()

if __name__ == "__main__":
    import os
    from multiprocessing import resource_tracker

    from uvicorn.main import main

    os.environ[secrets_env.API_FORGETS] = "1"
    secrets_env.started_without_secrets(resource_tracker.ensure_running)
    main(prog_name="uvicorn")
