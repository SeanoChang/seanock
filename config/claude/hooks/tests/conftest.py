"""Tests in this folder write no bytecode into the repo.

test_merge_gate.py loads merge-gate.py as a module. Python then writes
config/claude/hooks/__pycache__, and the installer links every entry of
config/claude/hooks into ~/.claude/hooks, that folder too. CI sets
PYTHONDONTWRITEBYTECODE=1; this does the same for a run that does not.

pytest reads this file before the test files of the folder. The one folder
that is still written is the __pycache__ of this folder, for this file itself.
"""
import sys

sys.dont_write_bytecode = True
