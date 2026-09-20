from importlib.metadata import version

import veyro


def test_public_version_matches_distribution_metadata() -> None:
    assert veyro.__version__ == version("veyro-factory")
