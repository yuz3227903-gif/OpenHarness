"""Local Agent Bridge.

The bridge runs on the user's own machine and lets the workbench connect to
Agents already installed there — Codex, OpenClaw and others — instead of the
platform starting an Agent runtime itself.

A browser must never be able to reach it just because it can address
``localhost``: every route outside the pairing handshake requires a token that
is only issued after the user confirms a code printed in the bridge's own
terminal. See :mod:`openharness.local_bridge.pairing`.
"""

from openharness.local_bridge.pairing import PairingStore

# `server` is deliberately not imported here. Running the bridge with
# `python -m openharness.local_bridge.server` imports this package first, and
# re-importing the module from here makes runpy load it twice.

__all__ = ["PairingStore"]
