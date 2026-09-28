import json
import os
import shutil
import tempfile
from unittest.mock import patch

import pytest

_REPO_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "core", "config.json",
)


@pytest.fixture(autouse=True)
def setup_test_env():
    # Give every test its own throwaway data root.
    #
    # The repository tracks a sample data/vectors.index.npy in the legacy
    # pickle format. The v4 snapshot reader fails closed on it, by design, so
    # any test that builds a MemoryService against the repository's own data
    # directory raises "Legacy or unsupported vector snapshot; manual migration
    # required" before it reaches the behaviour under test. Routine tests must
    # never read or write the tracked dataset, and the fail-closed guard must
    # not be weakened to accommodate them, so the suite gets a clean, empty
    # data root instead. Migrating a real dataset stays a separate, reviewed,
    # offline gate.
    #
    # The redirect goes through a temporary config file rather than
    # PINAK_DATA_ROOT: that environment variable outranks the config, so
    # setting it here would override the config paths that individual tests
    # (test_startup_verify, test_deployment_policy) set for themselves and
    # break the writer-lock checks they exist to assert. Function scope is
    # deliberate too, since a shared root lets one test's database and
    # snapshot leak into the next.
    data_root = tempfile.mkdtemp(prefix="pinak-test-data-")

    config = {}
    if os.path.exists(_REPO_CONFIG):
        with open(_REPO_CONFIG) as handle:
            config = json.load(handle)
    config["data_root"] = data_root
    config_path = os.path.join(data_root, "config.json")
    with open(config_path, "w") as handle:
        json.dump(config, handle)

    # The API dependency caches its MemoryService. Left cached, the instance
    # built under the first test's data root outlives it and later tests get a
    # service pointing at a directory that no longer exists.
    try:
        from app.api.v1.endpoints import _service_factory
    except Exception:  # pragma: no cover - import failures surface in the test
        _service_factory = None

    if _service_factory is not None:
        _service_factory.cache_clear()

    env = {
        "PINAK_CONFIG_PATH": config_path,
        "PINAK_JWT_SECRET": "test-secret",
        "PINAK_JWT_ALGORITHM": "HS256",
        "PINAK_EMBEDDING_BACKEND": "dummy",
        # Backward-compat keys for any legacy test usage
        "JWT_SECRET": "test-secret",
        "JWT_ALGORITHM": "HS256",
        "EMBEDDING_BACKEND": "dummy",
    }
    with patch.dict(os.environ, env):
        os.environ.pop("PINAK_DATA_ROOT", None)
        try:
            yield data_root
        finally:
            if _service_factory is not None:
                _service_factory.cache_clear()
            shutil.rmtree(data_root, ignore_errors=True)
