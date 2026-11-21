from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.services import tickdesk_exchange_master as master
from app.services.tickdesk_adapter import CN, MarketAdapter, timestamp


def _listing(symbol, listing, observed, *, status="listed"):
    return {"symbol": symbol, "name": "测试证券", "exchange": "SZ", "board": "深主板",
            "status": status, "listing_date": listing, "source": "深圳证券交易所",
            "source_ref": master.SZSE_LIST, "as_of": observed}


def test_delistings_require_both_the_exchange_date_and_actual_observation(tmp_path):
    observed = timestamp(datetime(2026, 11, 16, 11, tzinfo=CN))
    listing, removed = date(2014, 11, 4), date(2026, 11, 13)
    row = {**_listing("002731.SZ", listing, observed, status="delisted"), "delisted_on": removed}
    archive = tmp_path / "tickdesk-market/delisting-observations/2026-11-16.parquet"
    archive.parent.mkdir(parents=True)
    pl.DataFrame([row]).write_parquet(archive)

    assert master.read_exchange_delistings(tmp_path, observed - 1, effective_on=removed) == []
    assert master.read_exchange_delistings(tmp_path, observed, effective_on=date(2026, 11, 12)) == []
    assert master.read_exchange_delistings(tmp_path, observed, effective_on=removed) == [row]
    assert master.read_exchange_master(tmp_path, observed) == []


def test_catalog_preserves_delisted_identities_but_requires_proof_of_relisting(tmp_path):
    observed = timestamp(datetime(2026, 11, 16, 11, tzinfo=CN))
    old_start, removed = date(2014, 11, 4), date(2026, 11, 13)
    codes = ["002731.SZ", "000005.SZ", "000006.SZ", "000007.SZ"]
    market = tmp_path / "tickdesk-market"
    archive = market / "delisting-observations/2026-11-16.parquet"
    archive.parent.mkdir(parents=True)
    pl.DataFrame([{**_listing(code, old_start, observed, status="delisted"),
                   "delisted_on": date(2026, 11, 16) if code == codes[3] else removed}
                  for code in codes]).write_parquet(archive)
    relisted = _listing(codes[1], date(2026, 11, 16), observed)
    unresolved = _listing(codes[2], old_start, observed)
    pre_removal_listing = _listing(codes[3], old_start, timestamp(datetime(2026, 11, 13, 13, tzinfo=CN)))
    pl.DataFrame([relisted, unresolved, pre_removal_listing]).write_parquet(market / "exchange-master.parquet")
    indexes = {row["symbol"]: row for row in master.read_exchange_catalog(tmp_path, observed)}
    assert [indexes[code]["status"] for code in codes] == ["delisted", "listed", "unknown", "delisted"]
    assert master.read_exchange_master(tmp_path, observed) == [relisted]
    assert master.read_exchange_delistings(tmp_path, observed, effective_on=removed)[1]["listing_date"] == old_start

    vendor_only = pl.DataFrame([_listing(code, old_start, observed) for code in codes])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments_asset=lambda kind: vendor_only if kind == "stock" else pl.DataFrame())
    response = MarketAdapter(repo, None).prepare({"requestedAt": observed, "data": {"cmd": "search_symbols"}})
    by_symbol = {row["symbol"]: row for row in response["symbols"]}
    assert [by_symbol[code]["supported"] for code in codes] == [False, True, False, False]
    assert all(by_symbol[code]["source"]["name"] == "深圳证券交易所" for code in codes)


def test_delisting_refresh_keeps_original_observation_and_prior_revision(tmp_path, monkeypatch):
    current = datetime(2026, 11, 16, 11, tzinfo=CN)

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return current.astimezone(tz)

    monkeypatch.setattr(master, "datetime", Clock)
    monkeypatch.setattr(master, "_shanghai_delistings", lambda *_: [])
    listings = []
    monkeypatch.setattr(master, "_shenzhen_delistings", lambda *_: listings)
    for symbol, ended in (("002731.SZ", date(2026, 11, 13)), ("000016.SZ", date(2026, 10, 23))):
        row = _listing(symbol, date(2014, 11, 4), 0, status="delisted")
        row.pop("as_of")
        listings.append({**row, "delisted_on": ended})
    first = timestamp(current)
    assert master.refresh_exchange_delistings(tmp_path) == 2

    current = datetime(2026, 11, 17, 11, tzinfo=CN)
    lists_as_of = timestamp(current)
    corrected = {**listings[1], "delisted_on": date(2026, 10, 24)}
    listings[:] = [corrected]
    assert master.refresh_exchange_delistings(tmp_path) == 2
    before = {row["symbol"]: row for row in master.read_exchange_delistings(tmp_path, lists_as_of - 1)}
    after = {row["symbol"]: row for row in master.read_exchange_delistings(tmp_path, lists_as_of)}
    assert before["000016.SZ"]["delisted_on"] == date(2026, 10, 23)
    assert (after["000016.SZ"]["delisted_on"], after["000016.SZ"]["as_of"]) == (date(2026, 10, 24), lists_as_of)
    assert after["002731.SZ"] == before["002731.SZ"]
    assert after["002731.SZ"]["as_of"] == first
