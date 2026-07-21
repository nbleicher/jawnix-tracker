from __future__ import annotations

from jawnix.config import Settings
from jawnix_data.scraper import _prepare_nppes


class Response:
    text = '<a href="NPPES_Data_Dissemination_July_2026_V2.zip">download</a>'

    def raise_for_status(self):
        return None


def test_nppes_static_archive_is_versioned_before_refresh(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    current = data / "nppes.zip"
    current.write_bytes(b"old-version")
    settings = Settings(JAWNIX_SCRAPER_DB_PATH=data / "leads.db")
    monkeypatch.setattr("jawnix_data.scraper.httpx.get", lambda *_args, **_kwargs: Response())

    upstream, marker = _prepare_nppes(settings)

    assert upstream.endswith("NPPES_Data_Dissemination_July_2026_V2.zip")
    assert not current.exists()
    assert list((data / "nppes_versions").glob("legacy-*.zip"))[0].read_bytes() == b"old-version"
    marker.write_text(upstream + "\n")
    current.write_bytes(b"current-version")
    _prepare_nppes(settings)
    assert current.read_bytes() == b"current-version"
