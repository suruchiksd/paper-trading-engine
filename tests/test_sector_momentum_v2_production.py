from datetime import datetime
from pathlib import Path

import pytest

from paper_trading.adapters.sector_momentum_v2 import (
    QuoteResolver, SectorMomentumV2Orchestrator, V2SessionStore,
    canonical_key, instrument_for, validate_target_state, V2Signal,
)
from paper_trading.execution.costs import BasisPointsTransactionCosts
from paper_trading.execution.manager import OrderManager
from paper_trading.execution.paper import PaperBroker, SlippageConfig
from paper_trading.models import Fill, Side
from paper_trading.portfolio.accounting import Portfolio
from paper_trading.risk.manager import RiskLimits, RiskManager


def sig(symbol, status="ENTER", series="EQ", slot="1", weight="0.1111111111", priority="1"):
    return V2Signal({"strategy_id":"SECTOR_MOMENTUM","strategy_version":"V2","strategy_preregistration_hash":"h","strategy_build_reference":"b","rebalance_id":"r","signal_date":"2026-09-18","signal_timestamp":"2026-09-18T18:00:00+05:30","execution_earliest_date":"2026-09-21","execution_policy":"NEXT_VALID_SESSION_OPEN","sector":"S","sector_code":"S","sector_strength_rank":"1","symbol":symbol,"liquidity_rank":"1","selection_status":status,"target_weight":weight,"target_slot":slot,"current_holding_expected":"False","exit_intent":"false","signal_series":series,"tradability_status":"ELIGIBLE_EQ","corporate_action_safe":"True","replacement_priority":priority,"reason":"synthetic"})


def make_portfolio(symbols=(), quantity=100, cash=500000):
    p=Portfolio(cash)
    for i,s in enumerate(symbols):
        inst=instrument_for(s)
        p.process_fill(Fill(f"seed{i}",inst,Side.BUY,quantity,100,datetime(2026,9,18),fees=0))
        p.mark(inst.key,100)
    return p


def orch(p, normalized, raw=None, dry=False):
    return SectorMomentumV2Orchestrator(p, RiskManager(RiskLimits(9,100,100)), OrderManager(PaperBroker(slippage=SlippageConfig(market_bps=5),cost_model=BasisPointsTransactionCosts(20))), QuoteResolver(normalized,raw), dry_run=dry)


def rows(symbols, series="EQ", price=100):
    return {("2026-09-21",s): {"open":price,"close":price,"series":series} for s in symbols}


def test_empty_portfolio_nine_entries():
    ss=[sig(f"S{i}",slot=str(i),priority=str(i)) for i in range(9)]
    r=orch(make_portfolio((),cash=500000),rows([f"S{i}" for i in range(9)])); out=r.rebalance(ss,execution_date="2026-09-21")
    assert len(out["fills"]) == 9 and len(r.portfolio.open_positions)==9


def test_retained_within_band_has_no_resize():
    syms=[f"S{i}" for i in range(9)]; p=make_portfolio(syms,100,cash=1)
    out=orch(p,rows(syms)).rebalance([sig(s, "KEEP", slot=str(i)) for i,s in enumerate(syms)],execution_date="2026-09-21")
    assert not out["orders"]


def test_retained_outside_band_resizes():
    syms=[f"S{i}" for i in range(9)]; p=make_portfolio(syms,100,cash=100000); p.positions[canonical_key("S0")].quantity=140
    out=orch(p,rows(syms)).rebalance([sig(s, "KEEP", slot=str(i)) for i,s in enumerate(syms)],execution_date="2026-09-21")
    assert any(x.side is Side.SELL and x.instrument.tradingsymbol=="S0" for x in out["fills"])


def test_deselection_frees_slot_before_entry():
    old=[f"S{i}" for i in range(9)]; p=make_portfolio(old,100,cash=100000); target=[sig(s,"KEEP",slot=str(i)) for i,s in enumerate(old[1:])] + [sig("NEW","ENTER",slot="9")]
    out=orch(p,rows(old+['NEW'])).rebalance(target,execution_date="2026-09-21")
    assert any(x.instrument.tradingsymbol=="S0" and x.side is Side.SELL for x in out["fills"])
    assert any(x.instrument.tradingsymbol=="NEW" and x.side is Side.BUY for x in out["fills"])
    assert len(p.open_positions)==9


def test_existing_buy_at_nine_is_allowed():
    syms=[f"S{i}" for i in range(9)]; p=make_portfolio(syms,80,cash=100000)
    out=orch(p,rows(syms)).rebalance([sig(s,"KEEP",slot=str(i)) for i,s in enumerate(syms)],execution_date="2026-09-21")
    assert any(x.instrument.tradingsymbol=="S0" and x.side is Side.BUY for x in out["fills"])
    assert len(p.open_positions)==9


def test_new_be_entry_blocked():
    with pytest.raises(ValueError): validate_target_state([sig("X",series="BE")])


def test_raw_fallback_and_held_be_exit():
    p=make_portfolio(["H"],100,cash=100000); r=orch(p,{}, {("2026-09-21","H"):{"open":50,"close":49,"series":"BE"}})
    out=r.rebalance([sig("H","EXIT")],execution_date="2026-09-21")
    assert out["fills"] and out["fills"][0].side is Side.SELL and r.resolver.audit[0].source=="raw_provider"


def test_missing_exit_is_retained():
    p=make_portfolio(["H"],100,cash=100000); out=orch(p,{}).rebalance([sig("H","EXIT")],execution_date="2026-09-21")
    assert out["pending_exits"]==["H"] and len(p.open_positions)==1


def test_no_quote_no_fabricated_order():
    p=make_portfolio((),cash=500000); out=orch(p,{}).rebalance([sig("X")],execution_date="2026-09-21")
    assert not out["orders"] and not out["fills"]


def test_mark_hierarchy():
    r=QuoteResolver({("d","A"):{"open":10,"close":11,"series":"EQ"}}, {("d","B"):{"open":10,"close":12,"series":"BE"}})
    assert r.mark("d","A",9).close==11 and r.mark("d","B",9).close==12 and r.mark("d","C",9).close==9


def test_low_cash_clips_and_dry_run_does_not_mutate():
    p=make_portfolio((),cash=50); o=orch(p,rows(["X"]),dry=True); before=p.cash; out=o.rebalance([sig("X")],execution_date="2026-09-21")
    assert out["dry_run"] and out["state_mutated"] is False and p.cash==before and not p.positions


def test_state_persistence(tmp_path):
    p=make_portfolio(["X"],10,cash=1000); store=V2SessionStore(tmp_path/"state.json"); store.save(p,session_date="2026-09-21",strategy_hash="h",pending_exits=["Y"]); d=store.load(); assert d["cash"]==p.cash and d["positions"][0]["symbol"]=="NSE:X" and d["pending_exits"]==["Y"]


def test_malformed_and_duplicate_targets_fail_closed():
    with pytest.raises(ValueError): validate_target_state([sig("X"),sig("X",slot="2")])
    with pytest.raises(ValueError): validate_target_state([sig("X",slot="1"),sig("Y",slot="1")])


def test_new_entries_follow_frozen_target_slot_order():
    p = make_portfolio((), cash=500000)
    signals = [
        sig("SECTOR3FIRST", slot="7", priority="1"),
        sig("SECTOR1THIRD", slot="3", priority="3"),
        sig("SECTOR3SECOND", slot="8", priority="2"),
    ]
    out = orch(p, rows([s.symbol for s in signals])).rebalance(
        signals, execution_date="2026-09-21"
    )
    assert [fill.instrument.tradingsymbol for fill in out["fills"]] == [
        "SECTOR1THIRD", "SECTOR3FIRST", "SECTOR3SECOND"
    ]


def test_reloaded_position_can_be_sold_and_reloaded_again(tmp_path):
    original = make_portfolio(["OLD"], quantity=10, cash=1000)
    state_path = tmp_path / "state.json"
    store = V2SessionStore(state_path)
    store.save(original, session_date="2026-09-21", strategy_hash="h")

    restored = Portfolio(1000)
    store.restore_cash_and_positions(restored)
    outcome = orch(restored, rows(["OLD"])).rebalance(
        [sig("OLD", status="EXIT")], execution_date="2026-09-21"
    )
    assert len(outcome["fills"]) == 1
    assert outcome["fills"][0].side is Side.SELL
    assert len(restored.open_positions) == 0

    store.save(restored, session_date="2026-09-21", strategy_hash="h")
    fresh = Portfolio(1000)
    store.restore_cash_and_positions(fresh)
    assert fresh.cash == restored.cash
    assert len(fresh.open_positions) == 0
