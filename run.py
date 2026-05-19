"""
run.py

ssdm-collector 진입점.

실행 흐름:
1. Supabase 클라이언트 생성
2. SchemaCache 로드 (opportunities 컬럼 자동 파악)
3. opportunities_raw에서 pending 1000건 SELECT
4. source_key별로 그룹핑 → 각 parser로 정제
5. opportunities 테이블에 upsert
6. raw 처리 상태 마킹 (processed / error)

GitHub Actions에서 호출 (workflow_dispatch / repository_dispatch / cron).
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from typing import Dict, List

from supabase import create_client

from parsers import PARSER_REGISTRY
from parsers._repository import (
    fetch_pending_raw,
    mark_raw_error,
    mark_raw_processed,
    upsert_opportunities,
)
from parsers._schema import SchemaCache


def _get_supabase():
    """Supabase 클라이언트 생성 (환경변수 기반)."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY 환경변수가 필요함"
        )
    return create_client(url, key)


def _process_source(sb, source_key: str, raw_rows: List[dict]) -> Dict[str, int]:
    """단일 source의 raw 데이터들을 정제 + upsert."""
    parser = PARSER_REGISTRY.get(source_key)
    if not parser:
        print(f"[run] no parser for source_key={source_key}, skipping {len(raw_rows)} rows")
        # parser 없으면 error 마킹
        for row in raw_rows:
            mark_raw_error(sb, row["id"], f"no parser registered for {source_key}")
        return {"parsed": 0, "upserted": 0, "errors": len(raw_rows)}
    
    # 정제
    opps = []
    success_ids = []
    error_count = 0
    
    for row in raw_rows:
        raw_id = row["id"]
        raw_data = row["raw_data"]
        
        try:
            opp = parser.parse_one(raw_data)
            if opp is None:
                mark_raw_error(sb, raw_id, "parser returned None")
                error_count += 1
                continue
            
            opps.append(opp)
            success_ids.append(raw_id)
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            print(f"[run] parse error for {source_key} raw_id={raw_id}: {err_msg}")
            mark_raw_error(sb, raw_id, err_msg)
            error_count += 1
    
    # opportunities upsert
    upsert_result = upsert_opportunities(sb, opps)
    
    # 성공 건들 processed 마킹
    if success_ids:
        marked = mark_raw_processed(sb, success_ids)
        print(f"[run] {source_key}: marked {marked}/{len(success_ids)} as processed")
    
    return {
        "parsed": len(opps),
        "upserted": upsert_result["upserted"],
        "errors": error_count + upsert_result["failed"],
    }


def main():
    started_at = time.time()
    print(f"[run] ssdm-collector start")
    
    sb = _get_supabase()
    
    # 1. 스키마 캐시 로드
    SchemaCache.load(sb)
    
    # 2. pending raw 조회
    raw_rows = fetch_pending_raw(sb, limit=1000)
    print(f"[run] fetched {len(raw_rows)} pending rows")
    
    if not raw_rows:
        print("[run] nothing to do, exit")
        return 0
    
    # 3. source_key별 그룹핑
    by_source: Dict[str, List[dict]] = defaultdict(list)
    for row in raw_rows:
        by_source[row["source_key"]].append(row)
    
    # 4. source별 처리
    summary = {}
    for source_key, rows in by_source.items():
        print(f"\n[run] processing source={source_key} count={len(rows)}")
        result = _process_source(sb, source_key, rows)
        summary[source_key] = result
    
    # 5. 종합 리포트
    elapsed = round(time.time() - started_at, 2)
    print(f"\n[run] === Summary (elapsed: {elapsed}s) ===")
    total_parsed = total_upserted = total_errors = 0
    for source_key, result in summary.items():
        print(f"  {source_key}: parsed={result['parsed']}, "
              f"upserted={result['upserted']}, errors={result['errors']}")
        total_parsed += result["parsed"]
        total_upserted += result["upserted"]
        total_errors += result["errors"]
    print(f"  TOTAL: parsed={total_parsed}, upserted={total_upserted}, errors={total_errors}")
    
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
