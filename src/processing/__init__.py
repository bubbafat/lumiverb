"""Processing both sides run: the scheduler on the brain (src/server/scheduler)
and the CLI's scan (src/client/cli).

What's here: the API client results are saved through (api.py), this
machine's way of processing (machine.py: where libraries are, caches, how
video is rendered), scanning (scan.py, ingest.py), the account's AI machines
(ai_pool.py and the job guards), failure reports, the producers' settings as
the server says them, and the media tools (video/, proxy/, workers/). A
producer's work for one clip is in its own folder (src/producers/<artifact>/).

Nothing here imports the CLI (src/client) or the API server (src/server).
"""
