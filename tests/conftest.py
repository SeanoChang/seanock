"""Tests in this folder write no bytecode into the repo.

Some tests load a source file of config/ as a module (the AGENTS.md build).
Python then writes a __pycache__ folder beside that file. Under config/ such a
folder is a part with no row in manifest.txt, so
test_install_manifest.py::test_every_part_under_config_has_a_row_or_a_reason
fails on it. CI sets PYTHONDONTWRITEBYTECODE=1; this does the same for a run
that does not.

pytest reads this file before the test files of the folder. The one folder
that is still written is tests/__pycache__, for this file itself.
"""
import sys

sys.dont_write_bytecode = True
