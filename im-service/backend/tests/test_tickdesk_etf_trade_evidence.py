from datetime import datetime, timedelta

from app.services.tickdesk_etf_trade_evidence import (
    SSE_ETF_RULES,
    read_etf_trade_evidence,
    record_etf_trade_evidence,
)


def test_secondary_market_rules_are_source_bound_and_fail_closed(tmp_path):
    observed = datetime.fromisoformat("2026-11-16T11:30:02.001234+08:00")
    pdf = "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-08-13/510300_20260813_FPSL.pdf"
    documents = [(pdf, "华泰柏瑞沪深 300 ETF 招募说明书", observed, b"%PDF-test")]
    same_day = [(SSE_ETF_RULES, "上交所 ETF 业务实施细则", observed, b"<html>test</html>"),
                (pdf.replace("510300_", "511010_"), "国泰国债 ETF 产品资料", observed, b"%PDF-bond")]
    record_etf_trade_evidence(tmp_path, "510300.SH", False, documents)
    record_etf_trade_evidence(tmp_path, "511010.SH", True, same_day)
    record_etf_trade_evidence(tmp_path, "510300.SH", False, documents)
    cutoff = int(observed.timestamp() * 1000) + 1
    symbols = ["510300.SH", "511010.SH", "510500.SH"]
    assert read_etf_trade_evidence(tmp_path, symbols, cutoff - 1) == []
    rows = read_etf_trade_evidence(tmp_path, symbols, cutoff)
    policies = {row["record"]["symbol"]: row for row in rows if row["record"]["kind"] == "etfAuctionSellRule"}
    assert len(rows) == 3 and set(policies) == {"510300.SH", "511010.SH"}
    assert policies["510300.SH"]["record"]["sameDaySale"] is False
    assert policies["511010.SH"]["record"]["sameDaySale"] is True
    assert set(policies["511010.SH"]["record"]["proofRefs"]) == {src[0] for src in same_day}
    next_day = int((observed + timedelta(days=1)).timestamp() * 1000)
    assert read_etf_trade_evidence(tmp_path, symbols, next_day) == rows
    assert policies["510300.SH"]["record"]["verifiedOn"] == "2026-11-16"
    record_etf_trade_evidence(tmp_path, "511010.SH", False, same_day)
    [unambiguous] = read_etf_trade_evidence(tmp_path, symbols, next_day)
    assert unambiguous["record"]["symbol"] == "510300.SH"
