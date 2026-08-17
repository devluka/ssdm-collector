"""
run.py

ssdm-collector 진입점.

실행 흐름:
1. Supabase 클라이언트 생성
2. SchemaCache 로드 (opportunities 컬럼 자동 파악)
3. 사이클 루프:
   3-1. opportunities_raw에서 pending BATCH_LIMIT건 SELECT
   3-2. source_key별로 그룹핑 → 각 parser로 정제
   3-3. opportunities 테이블에 upsert
   3-4. upsert 성공분만 processed, 실패분은 error 마킹
   3-5. 종료 조건 검사 후 다음 사이클
4. 전체 요약 출력

종료 조건 (먼저 도달하는 것):
- pending 없음 (완주)
- MAX_CYCLES 도달
- TIME_BUDGET_SEC 초과 (다음 사이클 시작 전 검사)
- 직전 사이클과 동일한 raw_id 집합을 다시 잡음 (마킹 실패 → 무한루프 방지)

환경변수:
- BATCH_LIMIT      사이클당 조회 건수 (기본 1000)
- MAX_CYCLES       최대 사이클 수 (기본 300, 0이면 무제한)
- TIME_BUDGET_SEC  시간 예산 초 (기본 19800 = 5시간 30분)

GitHub Actions에서 호출 (workflow_dispatch / repository_dispatch / cron).
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from supabase import create_client

from parsers import PARSER_REGISTRY
from parsers._repository import (
    FetchError,
    fetch_pending_raw,
    mark_raw_error_bulk,
    mark_raw_processed,
    upsert_opportunities,
)
from parsers._schema import SchemaCache


# ---------------------------------------------------------------- config

def _env_int(name: str, default: int) -> int:
    """환경변수를 int로 읽되, 비었거나 파싱 실패면 default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[run] invalid {name}={raw!r}, using default {default}")
        return default


BATCH_LIMIT = _env_int("BATCH_LIMIT", 1000)
MAX_CYCLES = _env_int("MAX_CYCLES", 300)
TIME_BUDGET_SEC = _env_int("TIME_BUDGET_SEC", 19800)


def _get_supabase():
    """Supabase 클라이언트 생성 (환경변수 기반)."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY 환경변수가 필요함"
        )
    return create_client(url, key)


# ---------------------------------------------------------------- source 단위 처리

def _process_source(sb, source_key: str, raw_rows: List[dict]) -> Dict[str, int]:
    """
    단일 source의 raw 데이터들을 정제 + upsert + 상태 마킹.

    마킹 규칙:
    - parse 실패        → error (사유별 bulk)
    - upsert 성공       → processed
    - upsert 실패       → error (재시도 대상이 아님. pending 유지 시 다음 사이클에
                          같은 행을 다시 잡아 무한루프가 되므로 error로 격리)
    """
    parser = PARSER_REGISTRY.get(source_key)
    if not parser:
        ids = [r["id"] for r in raw_rows]
        print(f"[run] no parser for source_key={source_key}, marking {len(ids)} as error")
        mark_raw_error_bulk(sb, ids, f"no parser registered for {source_key}")
        return {
            "parsed": 0, "upserted": 0, "processed": 0,
            "errors": len(ids), "settled": len(ids),
        }

    pairs: List[Tuple[int, object]] = []
    none_ids: List[int] = []
    exc_ids: Dict[str, List[int]] = defaultdict(list)

    for row in raw_rows:
        raw_id = row["id"]
        raw_data = row["raw_data"]

        try:
            opp = parser.parse_one(raw_data)
            if opp is None:
                none_ids.append(raw_id)
                continue
            pairs.append((raw_id, opp))
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            exc_ids[err_msg].append(raw_id)

    # parse 실패분 bulk 마킹
    parse_error_count = 0
    if none_ids:
        parse_error_count += len(none_ids)
        mark_raw_error_bulk(sb, none_ids, "parser returned None")
    for err_msg, ids in exc_ids.items():
        parse_error_count += len(ids)
        print(f"[run] parse error x{len(ids)} ({source_key}): {err_msg}")
        mark_raw_error_bulk(sb, ids, err_msg)

    # upsert
    upsert_result = upsert_opportunities(sb, pairs)

    # upsert 성공분 processed
    ok_ids = upsert_result["ok_raw_ids"]
    processed = 0
    if ok_ids:
        processed = mark_raw_processed(sb, ok_ids)
        print(f"[run] {source_key}: marked {processed}/{len(ok_ids)} as processed")

    # upsert 실패분 error 격리
    fail_ids = upsert_result["failed_raw_ids"]
    if fail_ids:
        mark_raw_error_bulk(sb, fail_ids, "upsert failed")
        print(f"[run] {source_key}: marked {len(fail_ids)} as error (upsert failed)")

    errors = parse_error_count + len(fail_ids)

    return {
        "parsed": len(pairs),
        "upserted": upsert_result["upserted"],
        "processed": processed,
        "errors": errors,
        # settled: 이번 사이클에서 pending을 벗어나야 하는 행 수
        "settled": parse_error_count + len(ok_ids) + len(fail_ids),
    }


# ---------------------------------------------------------------- 사이클 1회

def _run_cycle(sb, cycle_no: int, after_id: Optional[int]) -> Dict[str, object]:
    """
    사이클 1회 실행.

    after_id는 직전 사이클의 마지막 id (커서).
    이미 처리한 구간을 다시 스캔하지 않게 해 조회 비용을 일정하게 유지한다.

    Returns:
        {
            "fetched": N, "parsed": N, "upserted": N,
            "processed": N, "errors": N, "settled": N,
            "id_set": set[int], "max_id": int | None,
        }
        fetched == 0 이면 더 처리할 pending 없음.
    """
    raw_rows = fetch_pending_raw(sb, limit=BATCH_LIMIT, after_id=after_id)
    fetched = len(raw_rows)

    if fetched == 0:
        return {
            "fetched": 0, "parsed": 0, "upserted": 0,
            "processed": 0, "errors": 0, "settled": 0,
            "id_set": set(), "max_id": None,
        }

    id_set: Set[int] = {r["id"] for r in raw_rows}

    by_source: Dict[str, List[dict]] = defaultdict(list)
    for row in raw_rows:
        by_source[row["source_key"]].append(row)

    totals = {
        "fetched": fetched, "parsed": 0, "upserted": 0,
        "processed": 0, "errors": 0, "settled": 0,
        "id_set": id_set,
        # id ASC 정렬이므로 마지막 행의 id가 최대값 = 다음 사이클의 커서
        "max_id": raw_rows[-1]["id"],
    }

    for source_key, rows in by_source.items():
        print(f"[run] cycle {cycle_no} | source={source_key} count={len(rows)}")
        result = _process_source(sb, source_key, rows)
        for k in ("parsed", "upserted", "processed", "errors", "settled"):
            totals[k] += result[k]

    return totals


# ---------------------------------------------------------------- main

def main():
    started_at = time.time()
    print("[run] ssdm-collector start")
    print(f"[run] config: BATCH_LIMIT={BATCH_LIMIT} "
          f"MAX_CYCLES={MAX_CYCLES or 'unlimited'} "
          f"TIME_BUDGET_SEC={TIME_BUDGET_SEC}")

    sb = _get_supabase()

    SchemaCache.load(sb)

    grand = {
        "cycles": 0, "fetched": 0, "parsed": 0,
        "upserted": 0, "processed": 0, "errors": 0,
    }
    stop_reason = "unknown"
    cycle_no = 0
    prev_id_set: Set[int] = set()
    fetch_failed = False
    cursor: Optional[int] = None

    while True:
        if MAX_CYCLES and cycle_no >= MAX_CYCLES:
            stop_reason = f"max cycles reached ({MAX_CYCLES})"
            break

        elapsed = time.time() - started_at
        if elapsed >= TIME_BUDGET_SEC:
            stop_reason = f"time budget exceeded ({int(elapsed)}s / {TIME_BUDGET_SEC}s)"
            break

        cycle_no += 1
        cycle_started = time.time()
        try:
            result = _run_cycle(sb, cycle_no, cursor)
        except FetchError as e:
            # 조회 실패를 '처리할 것 없음'으로 오인하면 안 된다.
            # 적체가 남은 채 초록불이 뜨는 것이 이번 장애의 핵심 원인이었다.
            stop_reason = f"fetch failed: {e}"
            fetch_failed = True
            cycle_no -= 1
            break

        if result["fetched"] == 0:
            stop_reason = "no pending rows left"
            cycle_no -= 1
            break

        # 무한루프 방지: 직전 사이클과 동일한 행을 다시 잡았다면
        # 상태 마킹이 반영되지 않은 것 → 즉시 중단
        cur_id_set: Set[int] = result["id_set"]
        if prev_id_set and cur_id_set == prev_id_set:
            stop_reason = (f"same {len(cur_id_set)} rows fetched again "
                           f"— status marking not taking effect")
            cycle_no -= 1
            break
        prev_id_set = cur_id_set
        cursor = result["max_id"]

        grand["cycles"] = cycle_no
        for k in ("fetched", "parsed", "upserted", "processed", "errors"):
            grand[k] += result[k]

        cycle_elapsed = round(time.time() - cycle_started, 2)
        print(f"[run] cycle {cycle_no} done ({cycle_elapsed}s): "
              f"fetched={result['fetched']} parsed={result['parsed']} "
              f"upserted={result['upserted']} processed={result['processed']} "
              f"errors={result['errors']} settled={result['settled']} "
              f"| cursor={cursor} cumulative fetched={grand['fetched']}")

        # 이번 사이클에서 아무 행도 pending을 벗어나지 못했다면 다음 사이클도 동일
        if result["settled"] == 0:
            stop_reason = "cycle settled 0 rows (all marking failed)"
            break

    total_elapsed = round(time.time() - started_at, 2)
    print(f"\n[run] === Summary (elapsed: {total_elapsed}s) ===")
    print(f"  stop reason: {stop_reason}")
    print(f"  cycles:    {grand['cycles']}")
    print(f"  fetched:   {grand['fetched']}")
    print(f"  parsed:    {grand['parsed']}")
    print(f"  upserted:  {grand['upserted']}")
    print(f"  processed: {grand['processed']}")
    print(f"  errors:    {grand['errors']}")

    # 비정상 종료 사유는 무조건 실패 처리.
    # (문제가 있는데 초록불이 뜨면 장애를 놓친다)
    if fetch_failed:
        print("[run] ABNORMAL: pending 조회 실패 — 인덱스/타임아웃/인증 확인 필요")
        return 1

    if stop_reason.startswith("same ") or stop_reason.startswith("cycle settled 0"):
        print("[run] ABNORMAL: status marking is not taking effect — check DB/RLS")
        return 1

    if grand["fetched"] == 0:
        print("[run] nothing to do")
        return 0

    return 0 if grand["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
