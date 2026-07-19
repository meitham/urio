#!/usr/bin/env python3
from importlib.metadata import version as dist_version

import urio


def test_version():
    # given: the package is installed

    # when: we access the version
    version = urio.__version__

    # then: it matches the installed distribution's metadata
    assert version == dist_version('urio')


def test_public_api():
    # given: the imported package

    # when: inspecting its public names
    names = set(urio.__all__)

    # then: the documented surface is exported
    assert {'open', 'Path', 'AsyncPath', 'set_backend', 'get_backend'} <= names
