"""Tiled, reached only when a person asks: ``nrw tiled ...``.

The experiment's metadata is pushed to its node on Tiled (tiled.ornl.gov at
ORNL), what is there is read back, and reduced files are copied in when the
data mount is out of reach. Through the official client, ``tiled``, installed
with the ``tiled`` extra (the installer's ``NRW_EXTRAS=tiled``) and imported
only inside these commands, so no other ``nrw`` command pays for it.

What it never does: write anything under ``autoreduction/``, which another
process fills; run under ``nrw serve``, the poller, or an agent, which the
guard refuses every ``nrw tiled`` command; or take the server, the key, or the
token cache from a project's files, which anyone who commits writes.
"""
