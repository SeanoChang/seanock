"""No test run writes bytecode into the repo.

Some test files load a source file of config/ as a module: the AGENTS.md
build, the merge gate, and the scripts of three skills. With bytecode on,
Python writes a __pycache__ folder beside each such file. Under config/codex
that folder is a part with no row in manifest.txt, and
tests/test_install_manifest.py fails on it. Under config/claude/hooks and the
skill folders it is linked into ~/.claude by the installer.

pytest.ini, beside this file, makes this folder the root of every pytest run
that starts inside the repo. So pytest reads this file first, before any test
file, whichever folder is tested. CI sets PYTHONDONTWRITEBYTECODE=1; this does
the same for a run that does not.

The one folder that is still written is __pycache__ beside this file, for this
file itself. A run through a link from outside the repo (pytest
~/.claude/skills/<name>/tests) does not find pytest.ini and is not covered.
"""
import sys

sys.dont_write_bytecode = True
