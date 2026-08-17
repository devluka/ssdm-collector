"""
parsers/_repository.py

Opportunity → opportunities 테이블 upsert + opportunities_raw 처리 상태 마킹.

동작:
1. SchemaCache.filter_row()로 알 수 없는 컬럼 자동 제거
2. Supabase upsert (ON CONFLICT source_key, ext_id DO UPDATE)
3. 처리한 raw_id들을 opportunities_raw에서 'processed' 마킹

[변경 이력]
- upsert를 (raw_id, Opportunity) 쌍으로 받아, 성공한 batch의 raw_id만 반환.
  기존에는 upsert 실패 여부와 무관하게 parse 성공분 전체를 processed로 마킹해
  데이터가 유실될 수 있었음.
- dedupe로 제외된 raw_id도 승자와 함께 반환. 이 행들은 같은 (source_key, ext_id)의
  최신 행이 upsert되었으므로 함께 처리해야 pending에 남지 않음.
- mark_raw_error_bulk 추가. 사이클 반복 시 개별 호출 폭증 방지.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from parsers._schema import SchemaCache
from parsers.opportunity_dto import Opportunity


def upsert_opportunities(
    sb,
    pairs: List[Tuple[int, Opportunity]],
    batch_size: int = 100,
) -> dict:
    """
    (raw_id, Opportunity) 쌍 리스트를 opportunities 테이블에 upsert.

    같은 (source_key, ext_id) 조합이 한 batch 안에 두 번 이상 들어가면
    PostgreSQL 21000 cardinality_violation 에러가 발생하므로, batch 만들기 전에
    dedupe 처리한다. fetch_pending_raw가 id ASC 정렬로 가져오므로
    나중에 온 행(= 더 큰 id = 더 나중에 적재된 행)이 승자가 된다.

    Returns:
        {
            "total": N,              # 입력 쌍 개수
            "upserted": M,           # upsert 성공한 row 수 (dedupe 후 기준)
            "failed": K,             # upsert 실패한 row 수 (dedupe 후 기준)
            "deduped": D,            # dedupe로 제외된 쌍 개수
            "ok_raw_ids": [...],     # processed 마킹 대상 (중복 흡수분 포함)
            "failed_raw_ids": [...], # upsert 실패 (중복 흡수분 포함)
        }
    """
    if not pairs:
        return {
            "total": 0, "upserted": 0, "failed": 0, "deduped": 0,
            "ok_raw_ids": [], "failed_raw_ids": [],
        }

    original_count = len(pairs)

    # (source_key, ext_id) 기준 dedupe.
    winner_of: Dict[tuple, int] = {}      # key -> 현재 승자 raw_id
    absorbed: Dict[int, List[int]] = {}   # 승자 raw_id -> 흡수한 raw_id 전체
    winner_opp: Dict[int, Opportunity] = {}

    for raw_id, opp in pairs:
        row = opp.to_row()
        key = (row.get("source_key"), row.get("ext_id"))

        prev = winner_of.get(key)
        if prev is None:
            winner_of[key] = raw_id
            absorbed[raw_id] = [raw_id]
            winner_opp[raw_id] = opp
        else:
            # 뒤에 온 행이 최신(fetched_at ASC) → 승자 교체, 이전 이력 승계
            prev_ids = absorbed.pop(prev, [prev])
            winner_opp.pop(prev, None)
            winner_of[key] = raw_id
            absorbed[raw_id] = prev_ids + [raw_id]
            winner_opp[raw_id] = opp

    winners: List[int] = list(winner_opp.keys())
    deduped = original_count - len(winners)
    if deduped > 0:
        print(f"[Repository] deduped {deduped} duplicate rows "
              f"({original_count} → {len(winners)})")

    upserted = 0
    failed = 0
    ok_raw_ids: List[int] = []
    failed_raw_ids: List[int] = []

    for i in range(0, len(winners), batch_size):
        batch_ids = winners[i : i + batch_size]
        rows = [SchemaCache.filter_row(winner_opp[rid].to_row()) for rid in batch_ids]

        # 이 batch가 커버하는 raw_id 전체 (흡수된 중복 포함)
        covered: List[int] = []
        for rid in batch_ids:
            covered.extend(absorbed.get(rid, [rid]))

        try:
            res = sb.table("opportunities").upsert(
                rows,
                on_conflict="source_key,ext_id",
            ).execute()
            count = len(res.data) if res.data else len(rows)
            upserted += count
            ok_raw_ids.extend(covered)
            print(f"[Repository] batch {i // batch_size + 1}: {count} upserted")
        except Exception as e:
            failed += len(rows)
            failed_raw_ids.extend(covered)
            print(f"[Repository] batch {i // batch_size + 1} failed: {e}")

    return {
        "total": original_count,
        "upserted": upserted,
        "failed": failed,
        "deduped": deduped,
        "ok_raw_ids": ok_raw_ids,
        "failed_raw_ids": failed_raw_ids,
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
            "error_message": error_message[:500],
        }).eq("id", raw_id).execute()
        return True
    except Exception as e:
        print(f"[Repository] mark_error failed: {e}")
        return False


def mark_raw_error_bulk(sb, raw_ids: List[int], error_message: str) -> int:
    """
    여러 raw_id를 한 번에 error 마킹.

    사이클 반복 시 개별 호출이 폭증하는 것을 막기 위해 추가.
    같은 사유(예: upsert 실패, parser 없음)로 묶이는 건들에 사용.
    """
    if not raw_ids:
        return 0

    try:
        res = sb.table("opportunities_raw").update({
            "process_status": "error",
            "processed_at": "now()",
            "error_message": error_message[:500],
        }).in_("id", raw_ids).execute()
        return len(res.data) if res.data else len(raw_ids)
    except Exception as e:
        print(f"[Repository] mark_error_bulk failed: {e}")
        return 0


class FetchError(RuntimeError):
    """
    pending 조회 자체가 실패했음을 나타낸다.

    빈 리스트로 반환하면 호출부가 '처리할 것이 없다'로 오인해
    정상 종료(exit 0)해버린다. 실제로는 수십만 건이 남아 있는데
    워크플로우는 초록불이 되므로 장애를 놓친다.
    """


def fetch_pending_raw(
    sb,
    source_key: Optional[str] = None,
    limit: int = 1000,
    after_id: Optional[int] = None,
) -> List[dict]:
    """
    opportunities_raw에서 pending 상태인 raw 데이터 SELECT.
    source_key 지정 시 해당 source만 처리.

    정렬은 id ASC.
    fetched_at 정렬은 적체가 커지면 statement timeout을 유발한다
    (부분 인덱스가 없으면 전체 정렬). id는 PK 인덱스를 그대로 타고,
    bigint 자동증가라 적재 순서와 사실상 동일하다.

    after_id (커서):
        지정하면 id > after_id 인 행만 조회한다.
        커서가 없으면 사이클이 진행될수록 앞쪽에 쌓인 processed 구간을
        매번 다시 훑어야 해서 조회 비용이 계속 증가하고 결국 timeout이 난다.
        직전 사이클의 마지막 id를 넘겨주면 스캔 시작점이 앞으로 밀려
        사이클 비용이 일정하게 유지된다.

        주의: 커서를 쓰면 커서보다 작은 id 중 이번 실행에서 실패해
        pending으로 남은 행은 같은 실행 안에서 재조회되지 않는다.
        다음 실행(커서 초기화)에서 다시 잡히므로 유실은 아니다.

    Raises:
        FetchError: 조회 실패 (timeout, 인증 등). 호출부가 반드시 구분해야 함.
    """
    try:
        query = sb.table("opportunities_raw").select(
            "id, source_key, ext_id, raw_data, fetched_at"
        ).eq("process_status", "pending")

        if source_key:
            query = query.eq("source_key", source_key)

        if after_id is not None:
            query = query.gt("id", after_id)

        query = query.order("id", desc=False).limit(limit)

        res = query.execute()
        return res.data or []
    except Exception as e:
        print(f"[Repository] fetch_pending failed: {e}")
        raise FetchError(f"{type(e).__name__}: {e}") from e
