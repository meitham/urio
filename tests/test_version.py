#!/usr/bin/env python3
from importlib.metadata import version as dist_version

import urio


def test_version():

    version = urio.__version__

    assert version == dist_version('urio')


def test_public_api():

    names = set(urio.__all__)

    assert {'open', 'Path', 'AsyncPath', 'set_backend', 'get_backend'} <= names
