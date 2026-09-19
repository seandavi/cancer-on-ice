import pytest

from canceronice import catalog


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("CANCERONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("CANCERONICE_URI", raising=False)
    return catalog()
