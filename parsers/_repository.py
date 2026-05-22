"""
parsers/_repository.py

Opportunity → opportunities 테이블 upsert + opportunities_raw 처리 상태 마킹.

동작:
1. SchemaCache.filter_row()로 알 수 없는 컬럼 자동 제거
2. Supabase upsert (ON CONFLICT source_key, ext_id DO UPDATE)
3. 처리한 raw_id들을 opportunities_raw에서 'processed' 마킹
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from parsers._schema import SchemaCache
from parsers.opportunity_dto import Opportunity


def upsert_opportunities(
    sb,
    opps: List[Opportunity],
    batch_size: int = 100
) -> dict:
    """
    Opportunity 리스트를 opportunities 테이블에 upsert.
    
    같은 (source_key, ext_id) 조합이 한 batch 안에 두 번 이상 들어가면
    PostgreSQL 21000 cardinality_violation 에러가 발생하므로, batch 만들기 전에
    dedupe 처리한다. fetch_pending_raw가 fetched_at ASC 정렬로 가져오므로
    dict 덮어쓰기 시 최신 fetched_at 행이 살아남는다.
    
    Returns:
        {"total": N, "upserted": M, "failed": K, "deduped": D}
    """
    if not opps:
        return {"total": 0, "upserted": 0, "failed": 0, "deduped": 0}
    
    # (source_key, ext_id) 기준 dedupe
    original_count = len(opps)
    deduped_map = {}
    for opp in opps:
        row = opp.to_row()
        key = (row.get("source_key"), row.get("ext_id"))
        deduped_map[key] = opp
    
    opps = list(deduped_map.values())
    deduped = original_count - len(opps)
    if deduped > 0:
        print(f"[Repository] deduped {deduped} duplicate rows "
              f"({original_count} → {len(opps)})")
    
    upserted = 0
    failed = 0
    
    for i in range(0, len(opps), batch_size):
        batch = opps[i : i + batch_size]
        # to_row() 호출 후 SchemaCache로 알 수 없는 컬럼 제거
        rows = [SchemaCache.filter_row(opp.to_row()) for opp in batch]
        
        try:
            res = sb.table("opportunities").upsert(
                rows,
                on_conflict="source_key,ext_id",
            ).execute()
            count = len(res.data) if res.data else len(rows)
            upserted += count
            print(f"[Repository] batch {i // batch_size + 1}: {count} upserted")
        except Exception as e:
            failed += len(rows)
            print(f"[Repository] batch {i // batch_size + 1} failed: {e}")
    
    return {
        "total": original_count,
        "upserted": upserted,
        "failed": failed,
        "deduped": deduped,
    }


def mark_raw_processed(sb, raw_ids: List[int]) -> int:
    """opportunities_raw의 처리 완료 건들을 processed로 마킹."""
    if not raw_ids:
        return 0
    
    try:
        res = sb.table("opportunities_raw").update({
            "process_status": "processed",
            "processed_at": "now()",
        }).in_("id", raw_ids).execute()
        return len(res.data) if res.data else len(raw_ids)
    except Exception as e:
        print(f"[Repository] mark_processed failed: {e}")
        return 0


def mark_raw_error(sb, raw_id: int, error_message: str) -> bool:
    """opportunities_raw의 정제 실패 건을 error로 마킹."""
    try:
        sb.table("opportunities_raw").update({
            "process_status": "error",
            "processed_at": "now()",
            "error_message": error_message[:500],  # 너무 길면 잘림
        }).eq("id", raw_id).execute()
        return True
    except Exception as e:
        print(f"[Repository] mark_error failed: {e}")
        return False


def fetch_pending_raw(
    sb,
    source_key: Optional[str] = None,
    limit: int = 1000
) -> List[dict]:
    """
    opportunities_raw에서 pending 상태인 raw 데이터 SELECT.
    source_key 지정 시 해당 source만 처리.
    """
    try:
        query = sb.table("opportunities_raw").select(
            "id, source_key, ext_id, raw_data, fetched_at"
        ).eq("process_status", "pending")
        
        if source_key:
            query = query.eq("source_key", source_key)
        
        query = query.order("fetched_at", desc=False).limit(limit)
        
        res = query.execute()
        return res.data or []
    except Exception as e:
        print(f"[Repository] fetch_pending failed: {e}")
        return []
