from pathlib import Path
import csv
from paper_trading.adapters.sector_momentum_v2 import load_signals, validate_target_state, SIGNAL_SCHEMA_VERSION

def test_adapter_schema_and_version():
    p = Path('/home/suruchi-pandey/Projects/mft-strategy-research/reports/sector_strategy/v2/paper_handoff/signals_v1_sample.csv')
    s = load_signals(p); assert s; validate_target_state(s); assert SIGNAL_SCHEMA_VERSION == 'SECTOR_MOMENTUM_V2_SIGNAL_V1'; assert all(x.row['strategy_version']=='V2' for x in s)

def test_adapter_rejects_new_be():
    p = Path('/tmp/v2_bad_signal.csv'); p.write_text('strategy_id,strategy_version,strategy_preregistration_hash,strategy_build_reference,rebalance_id,signal_date,signal_timestamp,execution_earliest_date,execution_policy,sector,sector_code,sector_strength_rank,symbol,liquidity_rank,selection_status,target_weight,target_slot,current_holding_expected,exit_intent,signal_series,tradability_status,corporate_action_safe,replacement_priority,reason\nSECTOR_MOMENTUM,V2,x,x,r,2026-09-18,2026-09-18T18:00:00+05:30,2026-09-21,NEXT_VALID_SESSION_OPEN,S,S,1,X,1,ENTER,1,1,False,false,BE,ELIGIBLE_BE_EXIT,True,1,bad\n')
    s=load_signals(p)
    try: validate_target_state(s); assert False
    except ValueError: pass
