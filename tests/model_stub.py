"""Model ids are the user's choice (OPENROUTER_MODEL / OPENROUTER_AUDIT_MODEL in .env); tests use stand-ins."""
import os
from unittest.mock import patch

import assess
import personalized

_patches = []


def setUpModule():
    for target, name, value in ((assess, 'DEFAULT_MODEL', 'test/model'), (personalized, 'AUDIT_MODEL', 'test/audit-model')):
        stub = patch.object(target, name, value)
        stub.start()
        _patches.append(stub)
    # A real .env loaded by another test must not decide which model these tests see.
    stub = patch.dict(os.environ, {'OPENROUTER_MODEL': 'test/model', 'OPENROUTER_AUDIT_MODEL': 'test/audit-model'})
    stub.start()
    _patches.append(stub)


def tearDownModule():
    while _patches:
        _patches.pop().stop()
