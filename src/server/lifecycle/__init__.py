"""Running and looking after a Lumiverb server on its own machine, on any
platform: `lumiverb-server` (cli.py).

On the brain (Linux) systemd runs the services and scripts/update.sh updates
them. A Mac has neither: Homebrew installs Lumiverb and launchd runs it, so
what systemd and the scripts do there is done here instead, in Python that
runs on both.
"""
