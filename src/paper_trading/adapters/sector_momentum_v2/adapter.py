from __future__ import annotations
import copy, csv, json
from dataclasses import asdict, is_dataclass
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping
from ...models import Candle, Fill, Instrument, OrderRequest, OrderType, Side, TradeIntent
from ...portfolio.accounting import Portfolio
from ...risk.manager import RiskManager
from ...execution.manager import OrderManager

SIGNAL_SCHEMA_VERSION = "SECTOR_MOMENTUM_V2_SIGNAL_V1"
REQUIRED = ("strategy_id","strategy_version","strategy_preregistration_hash","strategy_build_reference","rebalance_id","signal_date","signal_timestamp","execution_earliest_date","execution_policy","sector","sector_code","sector_strength_rank","symbol","liquidity_rank","selection_status","target_weight","target_slot","current_holding_expected","exit_intent","signal_series","tradability_status","corporate_action_safe","replacement_priority","reason")

@dataclass(frozen=True, slots=True)
class V2Signal:
    row: dict[str, str]
    @property
    def symbol(self): return str(self.row["symbol"]).strip().upper().split(":")[-1]
    @property
    def active(self): return self.row.get("selection_status") in {"ENTER","KEEP"}
    @property
    def target_weight(self): return float(self.row.get("target_weight") or 0)
    @property
    def priority(self): return int(float(self.row.get("replacement_priority") or 0))
    @property
    def target_slot(self): return int(float(self.row.get("target_slot") or 0))

def canonical_key(symbol: str) -> str:
    s = str(symbol).strip().upper()
    return s if ":" in s else f"NSE:{s}"

def instrument_for(symbol: str, series: str = "EQ") -> Instrument:
    return Instrument("NSE", str(symbol).split(":")[-1].upper(), instrument_type=series or "EQ")

def load_signals(path: str | Path) -> list[V2Signal]:
    p = Path(path)
    if not p.exists(): raise ValueError(f"target-state file missing: {p}")
    try:
        with p.open(newline="") as f: rows = list(csv.DictReader(f))
    except Exception as exc: raise ValueError(f"malformed target-state file: {exc}") from exc
    if not rows: raise ValueError("target-state file is empty")
    missing = set(REQUIRED) - set(rows[0])
    if missing: raise ValueError(f"missing V2 signal fields: {sorted(missing)}")
    return [V2Signal(r) for r in rows]

def validate_target_state(signals: list[V2Signal], *, preregistration_hash=None, build_reference=None, execution_date=None) -> None:
    if not signals: raise ValueError("target state is empty")
    if any(s.row.get("strategy_id") != "SECTOR_MOMENTUM" or s.row.get("strategy_version") != "V2" for s in signals): raise ValueError("unauthorized strategy or version")
    if preregistration_hash and any(s.row.get("strategy_preregistration_hash") != preregistration_hash for s in signals): raise ValueError("strategy preregistration hash mismatch")
    if build_reference and any(s.row.get("strategy_build_reference") != build_reference for s in signals): raise ValueError("strategy build reference mismatch")
    groups = {}
    for s in signals: groups.setdefault(s.row.get("rebalance_id"), []).append(s)
    for rows in groups.values():
        if execution_date and {s.row.get("execution_earliest_date") for s in rows} != {execution_date}: raise ValueError("unauthorized execution date")
        active = [s for s in rows if s.active]
        if len(active) > 9: raise ValueError("maximum positions exceeded")
        symbols = [s.symbol for s in active]
        if len(symbols) != len(set(symbols)): raise ValueError("duplicate target symbol")
        slots = [s.row.get("target_slot") for s in active if s.row.get("target_slot")]
        if len(slots) != len(set(slots)): raise ValueError("duplicate target slot")
        if any(s.target_weight < 0 for s in active) or sum(s.target_weight for s in active) > 1.0000001: raise ValueError("invalid target weights")
        if any(s.row.get("signal_series") != "EQ" for s in active): raise ValueError("new entries/active targets must be EQ")
        if any(s.row.get("selection_status") == "ENTER" and s.row.get("tradability_status") != "ELIGIBLE_EQ" for s in active): raise ValueError("new entries require EQ tradability")
        if any(str(s.row.get("corporate_action_safe", "")).lower() != "true" for s in active): raise ValueError("unsafe corporate-action state")
        if any(s.row.get("tradability_status") not in {"ELIGIBLE_EQ", "ELIGIBLE_BE_EXIT"} for s in active): raise ValueError("invalid tradability state")

@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str; day: str; purpose: str; source: str; series: str; open: float|None; close: float|None; status: str; reason: str

class QuoteResolver:
    def __init__(self, normalized: Mapping[tuple[str,str], Mapping], raw: Mapping[tuple[str,str], Mapping]|None=None): self.normalized=dict(normalized); self.raw=dict(raw or {}); self.audit=[]; self.mark_audit=[]
    def _row(self, day, symbol):
        key=(day,str(symbol).split(":")[-1].upper()); row=self.normalized.get(key) or self.raw.get(key); source="normalized_daily" if key in self.normalized else ("raw_provider" if row else "none"); return key[1],row,source
    def execution(self, day, symbol, purpose, held=False):
        sym,row,source=self._row(day,symbol)
        if row and float(row.get("open",0) or 0)>0 and str(row.get("series","")) in {"EQ","BE"}:
            series=str(row["series"])
            if purpose=="entry" and series!="EQ": q=Quote(sym,day,purpose,source,series,None,None,"ENTRY_SKIPPED_NON_EQ","new entries are EQ-only")
            else: q=Quote(sym,day,purpose,source,series,float(row["open"]),float(row.get("close")) if row.get("close") not in (None,"") else None,"USED","supported execution row")
        else: q=Quote(sym,day,purpose,"none","",None,None,"MISSING_AND_RETAINED" if held else "ENTRY_SKIPPED_NO_EQ","no supported execution row")
        self.audit.append(q); return q
    def mark(self, day, symbol, previous):
        sym,row,source=self._row(day,symbol)
        if row and float(row.get("close",0) or 0)>0 and str(row.get("series","")) in {"EQ","BE"}: q=Quote(sym,day,"mark",source,str(row["series"]),float(row.get("open",0) or 0),float(row["close"]),"USED","supported close")
        else: q=Quote(sym,day,"mark","previous_mark","",None,previous,"CARRIED","no supported close")
        self.mark_audit.append(q); return q

class V2SessionStore:
    def __init__(self,path): self.path=Path(path)
    def save(self, portfolio, *, session_date, strategy_hash, pending_exits=()):
        positions=[]
        for key,p in portfolio.positions.items():
            if p.quantity == 0:
                continue
            campaign=portfolio._campaigns.get(key)
            positions.append({
                "symbol":key,"quantity":p.quantity,"average_price":p.average_price,
                "last_price":p.last_price,
                "campaign": None if campaign is None else {
                    "entry_timestamp":campaign.entry_timestamp.isoformat(),
                    "entry_fill_ids":list(campaign.entry_fill_ids),
                    "unallocated_entry_costs":campaign.unallocated_entry_costs,
                    "signal_id":campaign.signal_id,"strategy_id":campaign.strategy_id,
                    "strategy_version":campaign.strategy_version,"intent":campaign.intent.value,
                },
            })
        payload={
            "session_date":session_date,"strategy_hash":strategy_hash,
            "starting_capital":portfolio.starting_capital,"cash":portfolio.cash,
            "gross_realized_pnl":portfolio.gross_realized_pnl,
            "realized_pnl":portfolio.realized_pnl,"total_costs":portfolio.total_costs,
            "positions":positions,"pending_exits":sorted(set(pending_exits)),
        }
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text(json.dumps(payload,sort_keys=True,indent=2)+"\n")
    def load(self):
        if not self.path.exists(): raise ValueError("portfolio state missing")
        try: return json.loads(self.path.read_text())
        except Exception as exc: raise ValueError(f"corrupt portfolio state: {exc}") from exc

    def restore_cash_and_positions(self, portfolio, instrument_factory=instrument_for):
        """Restore executable accounting state, including campaigns needed by SELLs.

        The first prospective state predates campaign persistence.  For that one
        backward-compatible format, its single bootstrap fill per position is
        reconstructed from average fill price and the certified 20-bps entry fee.
        Subsequent saves carry the exact campaign fields.
        """
        data = self.load()
        portfolio.positions.clear(); portfolio._campaigns.clear(); portfolio.closed_trades.clear()
        portfolio.cash=float(data.get("starting_capital",portfolio.starting_capital))
        session_timestamp=datetime.fromisoformat(str(data["session_date"])+"T09:16:00+05:30")
        inferred_costs=0.0
        for index,row in enumerate(data.get("positions", [])):
            inst=instrument_factory(row["symbol"])
            quantity=int(row["quantity"]); average=float(row["average_price"])
            campaign=row.get("campaign") or {}
            fees=float(campaign.get("unallocated_entry_costs",average*quantity*20/10000))
            inferred_costs+=fees
            intent=TradeIntent(campaign.get("intent",TradeIntent.SWING.value))
            fill=Fill(
                order_id=f"RESTORE-{data['session_date']}-{index}",instrument=inst,
                side=Side.BUY if quantity>0 else Side.SELL,quantity=abs(quantity),price=average,
                timestamp=datetime.fromisoformat(campaign.get("entry_timestamp",session_timestamp.isoformat())),
                signal_id=campaign.get("signal_id"),strategy_id=campaign.get("strategy_id","SECTOR_MOMENTUM"),
                strategy_version=campaign.get("strategy_version","V2"),
                fill_id=(campaign.get("entry_fill_ids") or [f"RESTORE-FILL-{index}"])[0],
                fees=fees,intent=intent,
            )
            portfolio.process_fill(fill)
            position=portfolio.positions[inst.key]; position.last_price=row.get("last_price")
            restored=portfolio._campaigns[inst.key]
            restored.entry_fill_ids=list(campaign.get("entry_fill_ids") or restored.entry_fill_ids)
            restored.unallocated_entry_costs=fees
        portfolio.cash=float(data["cash"])
        portfolio.gross_realized_pnl=float(data.get("gross_realized_pnl",0.0))
        portfolio.realized_pnl=float(data.get("realized_pnl",0.0))
        portfolio.total_costs=float(data.get("total_costs",inferred_costs))
        return data


def write_session_outputs(directory, result, *, signal_date, execution_date, starting_cash, ending_cash, strategy_version="V2"):
    """Write deterministic operational artifacts; no performance metrics are emitted."""
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    def rows(items):
        out=[]
        for x in items:
            if is_dataclass(x): out.append(asdict(x))
            elif isinstance(x, dict): out.append(x)
            else: out.append({"value": str(x)})
        return out
    def write_csv(name, values):
        values=rows(values); fields=sorted({k for r in values for k in r})
        with (directory / name).open("w", newline="") as f:
            w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(values)
    write_csv("orders.csv", result.get("orders", [])); write_csv("fills.csv", result.get("fills", [])); write_csv("risk_decisions.csv", result.get("risk_decisions", [])); write_csv("execution_quote_audit.csv", result.get("quote_audit", [])); write_csv("mark_audit.csv", result.get("mark_audit", []))
    (directory / "session_summary.json").write_text(json.dumps({"signal_date": signal_date, "execution_date": execution_date, "strategy_version": strategy_version, "starting_cash": starting_cash, "ending_cash": ending_cash, "orders_submitted": len(result.get("orders", [])), "fills": len(result.get("fills", [])), "risk_decisions": len(result.get("risk_decisions", [])), "unavailable_exits": len(result.get("pending_exits", [])), "state_mutated": result.get("state_mutated", False)}, sort_keys=True, indent=2) + "\n")

class SectorMomentumV2Orchestrator:
    def __init__(self, portfolio, risk, order_manager, resolver, *, slippage_bps=5, transaction_cost_bps=20, dry_run=False): self.portfolio=portfolio; self.risk=risk; self.order_manager=order_manager; self.resolver=resolver; self.slippage_bps=slippage_bps; self.transaction_cost_bps=transaction_cost_bps; self.dry_run=dry_run
    def _request(self,symbol,side,qty,price,day,reason): return OrderRequest(instrument_for(symbol),side,OrderType.MARKET,int(qty),datetime.fromisoformat(day+"T00:00:00"),eligible_after=datetime.fromisoformat(day+"T09:00:00"),reference_price=float(price),strategy_id="SECTOR_MOMENTUM",strategy_version="V2",metadata={"reason":reason},intent=TradeIntent.SWING)
    def _execute(self,req,open_price,day,decisions,orders,fills):
        d=self.risk.evaluate(req,self.portfolio); decisions.append({"symbol":req.instrument.tradingsymbol,"side":req.side.value,"requested_quantity":req.quantity,"approved":d.approved,"approved_quantity":d.approved_quantity,"reason":d.reason})
        if not d.approved: return
        if self.dry_run: orders.append(req); return
        order=self.order_manager.submit(req); fs=self.order_manager.process_candle(Candle(req.instrument,"1D",datetime.fromisoformat(day+"T10:00:00"),open_price,open_price,open_price,open_price))
        for f in fs: self.portfolio.process_fill(f); fills.append(f)
        orders.append(order)
    def rebalance(self,signals,*,execution_date,strategy_hash=""):
        validate_target_state(signals,execution_date=execution_date); work=copy.deepcopy(self) if self.dry_run else self; work.dry_run=self.dry_run
        active=sorted([s for s in signals if s.active],key=lambda s:(s.target_slot,s.symbol)); target={s.symbol:s for s in active}; held={k.split(":")[-1]:p for k,p in work.portfolio.positions.items() if p.quantity>0}; marks={}
        for sym,p in held.items(): q=work.resolver.execution(execution_date,sym,"valuation",True); marks[sym]=q.open if q.open is not None else (p.last_price or p.average_price)
        pretrade=work.portfolio.cash+sum(p.quantity*marks[s] for s,p in held.items()); target_n=max(len(active),1); target_value=pretrade/target_n; decisions=[]; orders=[]; fills=[]; pending=[]
        for sym in sorted(set(held)-set(target)):
            q=work.resolver.execution(execution_date,sym,"exit",True)
            if q.open is None: pending.append(sym); continue
            work._execute(work._request(sym,Side.SELL,held[sym].quantity,q.open,execution_date,"DESELECT"),q.open,execution_date,decisions,orders,fills)
        unit=lambda p:p*(1+self.slippage_bps/10000)*(1+self.transaction_cost_bps/10000)
        for sig in active:
            key=canonical_key(sig.symbol)
            if key not in work.portfolio.positions or work.portfolio.positions[key].quantity<=0: continue
            q=work.resolver.execution(execution_date,sig.symbol,"retained_resize",True)
            if q.open is None: continue
            p=work.portfolio.positions[key]; deviation=abs(p.quantity*q.open/pretrade-1/target_n) if pretrade else 0
            if deviation<0.02: continue
            desired=int(target_value/unit(q.open)); delta=desired-p.quantity
            if delta>0: delta=min(delta,int(max(work.portfolio.cash,0)/unit(q.open)))
            if delta: work._execute(work._request(sig.symbol,Side.BUY if delta>0 else Side.SELL,abs(delta),q.open,execution_date,"RETAINED_RESIZE"),q.open,execution_date,decisions,orders,fills)
        for sig in active:
            key=canonical_key(sig.symbol)
            if key in work.portfolio.positions and work.portfolio.positions[key].quantity>0: continue
            q=work.resolver.execution(execution_date,sig.symbol,"entry",False)
            if q.open is None or q.series!="EQ" or len(work.portfolio.open_positions)>=9: continue
            qty=min(int(target_value/unit(q.open)),int(max(work.portfolio.cash,0)/unit(q.open)))
            if qty>0: work._execute(work._request(sig.symbol,Side.BUY,qty,q.open,execution_date,"ENTRY"),q.open,execution_date,decisions,orders,fills)
        for key,p in list(work.portfolio.positions.items()):
            if p.quantity<=0: continue
            q=work.resolver.mark(execution_date,key.split(":")[-1],p.last_price or p.average_price)
            if q.close is not None: work.portfolio.mark(key,q.close)
        return {"orders":orders,"fills":fills,"risk_decisions":decisions,"pending_exits":pending,"dry_run":self.dry_run,"state_mutated":not self.dry_run,"portfolio":work.portfolio.positions,"quote_audit":work.resolver.audit,"mark_audit":work.resolver.mark_audit}
