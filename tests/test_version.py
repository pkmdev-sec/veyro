from importlib.metadata import version

import veyro


def test_public_version_matches_distribution_metadata() -> None:
    assert veyro.__version__ == version("veyro-factory")


def test_root_package_exports_only_the_version() -> None:
    assert veyro.__all__ == ["__version__"]
    for legacy_name in (
        "FactoryAssessment",
        "FactoryConfig",
        "FactoryRuntime",
        "FactoryState",
        "InterventionType",
    ):
        assert not hasattr(veyro, legacy_name)
