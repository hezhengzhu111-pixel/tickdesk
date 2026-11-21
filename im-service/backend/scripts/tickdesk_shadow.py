"""Capture, list or replay locally sealed v3 research evaluations."""
import argparse
import json
import re
import sys
from datetime import datetime
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tickdesk_shadow import capture_observation, list_runs, replay_run


def trial(data_dir: Path, archive: Path, symbol: str, cutoff: datetime, *, st_snapshot: Path | None = None) -> dict:
    from app.services.tickdesk_adapter import MarketAdapter, timestamp
    from app.services.tickdesk_regime_evidence import INDUSTRY_FIELD, MARKET_FIELD, RULE_ID
    from app.services.tickdesk_research import (
        read_research_config,
        research_data_identity,
        research_sources,
        scan_research,
        strategy_directories,
    )
    from app.strategy.config import list_overrides
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

    config = read_research_config(data_dir)
    if config is None:
        raise ValueError("research configuration unavailable")
    config = {**config, "symbols": [symbol]}
    st_reader = None
    fields = (MARKET_FIELD, INDUSTRY_FIELD)
    if st_snapshot is not None:
        from app.services.tickdesk_szse_risk import read_szse_st_snapshot

        st_reader = partial(read_szse_st_snapshot, st_snapshot)
        fields += ("is_st",)
    store = DataStore.readonly(data_dir)
    regimes = {}

    def record(strategy_id, context, _pool, _params, _overrides, _result, _bars):
        inputs = context.quant_inputs[str(context.as_of)]
        facts = {}
        for field in fields:
            fact = inputs.fact(symbol, field)
            facts[field] = {"value": fact.value, "reason": fact.reason,
                            "revisionIds": fact.revision_ids, "availableAt": fact.available_at}
        regimes[strategy_id] = {"decisionAt": inputs.cutoff, "facts": facts}

    try:
        engine = StrategyEngine(strategy_directories(data_dir))
        adapter = MarketAdapter(KlineRepository(store), engine)
        sources, manifests = research_sources(engine, config["strategies"])
        overrides = list_overrides(data_dir)
        request = {"requestedAt": timestamp(cutoff)}
        data_identity = research_data_identity(adapter, config, request["requestedAt"])
        original = scan_research(adapter, request, config, manifests, overrides)
        candidate = scan_research(adapter, request, config, manifests, overrides, capture=record,
                                  regime_archive=archive, st_reader=st_reader)
        if (research_sources(engine, config["strategies"]) != (sources, manifests) or list_overrides(data_dir) != overrides
                or research_data_identity(adapter, config, request["requestedAt"]) != data_identity):
            raise ValueError("strategy inputs changed during comparison")
    finally:
        store.db.close()
    return {"stage": "research", "ruleId": RULE_ID, "symbol": symbol, "cutoff": cutoff.isoformat(),
            "baseline": original["observations"], "trial": candidate["observations"], "regimeEvidence": regimes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", metavar="SHA256")
    mode.add_argument("--regime-trial", metavar="SYMBOL", help="Compare a single security in the research engine")
    mode.add_argument("--observe", action="store_true", help="Seal research inputs as of the current Shanghai session")
    mode.add_argument("--archive-replay", action="store_true", help="Seal a past cutoff as historical research")
    parser.add_argument("--cutoff", type=datetime.fromisoformat, help="Timezone-aware historical or regime-trial cutoff")
    parser.add_argument("--archive-dir", type=Path, help="Research archive root, required outside source data for capture")
    parser.add_argument("--regime-archive", type=Path)
    parser.add_argument("--szse-st-snapshot", type=Path, help="Read an explicit Shenzhen same-day source snapshot")
    args = parser.parse_args()
    if args.observe or args.archive_replay:
        if args.archive_dir is None or args.regime_archive or args.szse_st_snapshot:
            parser.error("capture needs --archive-dir and accepts no regime-trial sources")
        if args.observe and args.cutoff:
            parser.error("--observe uses the current time")
        if args.archive_replay and (args.cutoff is None or args.cutoff.utcoffset() is None):
            parser.error("--archive-replay needs a timezone-aware --cutoff")
        result = capture_observation(args.data_dir, args.archive_dir, cutoff=args.cutoff)
    elif args.regime_trial:
        if not re.fullmatch(r"[0-9]{6}\.(SZ|SH)", args.regime_trial) or args.cutoff is None or args.cutoff.utcoffset() is None:
            parser.error("--regime-trial needs a full security code and a timezone-aware --cutoff")
        archive = args.regime_archive or args.data_dir / "tickdesk-market/analysis-index-sources.sqlite3"
        result = trial(args.data_dir, archive, args.regime_trial, args.cutoff, st_snapshot=args.szse_st_snapshot)
    else:
        if args.cutoff or args.regime_archive or args.szse_st_snapshot:
            parser.error("source and cutoff options require --regime-trial or historical capture")
        root = args.archive_dir or args.data_dir
        result = replay_run(root, args.replay) if args.replay else list_runs(root, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
