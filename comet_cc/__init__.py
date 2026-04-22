"""CoMeT-CC — persistent memory plugin for Claude Code.

Runs a two-tier pipeline natively through `claude -p` subprocess calls:
a lightweight sensor that gates each turn, and a structuring compacter
that emits retrievable memory nodes on trip. A local embedder + sqlite
store serve those nodes back into future sessions via hooks.
"""

__version__ = "0.0.1"
