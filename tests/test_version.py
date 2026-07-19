#!/usr/bin/env python3
import urio


def test_version():
    # given: the package is installed

    # when: we access the version
    version = urio.__version__

    # then: version is set correctly
    assert version == '0.1.0'


def test_public_api():
    # given: the imported package

    # when: inspecting its public names
    names = set(urio.__all__)

    # then: the documented surface is exported
    assert {'open', 'Path', 'AsyncPath', 'set_backend', 'get_backend'} <= names
