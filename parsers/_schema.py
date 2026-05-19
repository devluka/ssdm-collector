"""
parsers/_schema.py

opportunities 테이블 스키마 동적 캐시.

Supabase에서 컬럼 추가/삭제되어도 collector 코드 수정 없이 자동 적응.
시작 시 한 번 get_opportunities_columns() RPC 호출 → 메모리 캐시.
"""
from __future__ import annotations

from typing import Set


class SchemaCache:
    """opportunities 컬럼 셋 메모리 캐시."""
    
    _allowed_columns: Set[str] = set()
    _loaded: bool = False
    
    @classmethod
    def load(cls, supabase_client) -> Set[str]:
        """opportunities 스키마 로드. 시작 시 1회만 호출."""
        try:
            res = supabase_client.rpc("get_opportunities_columns").execute()
            cols = {row["column_name"] for row in (res.data or [])}
            cls._allowed_columns = cols
            cls._loaded = True
            print(f"[Schema] opportunities columns: {len(cols)}개")
            return cols
        except Exception as e:
            # RPC 실패 시 기본 컬럼셋 폴백 (마이그레이션 안 됐을 때)
            print(f"[Schema] load failed: {e} — using fallback")
            cls._allowed_columns = {
                "id", "source_key", "source_type", "opp_type", "title",
                "description", "organization", "deadline",
                "regions", "industries", "stages", "tags",
                "url", "ext_id", "budget_min", "budget_max",
                "procurement_category", "eligibility_flags",
                "region_restriction", "bid_method", "eligibility_criteria",
                "raw_json", "fetched_at", "created_at", "updated_at",
                "participation_managed", "participation_max",
                "is_rnd", "participation_type",
            }
            cls._loaded = True
            return cls._allowed_columns
    
    @classmethod
    def filter_row(cls, row: dict) -> dict:
        """row에서 allowed columns만 남김. 알 수 없는 키는 무시."""
        if not cls._loaded:
            return row
        return {k: v for k, v in row.items() if k in cls._allowed_columns}
    
    @classmethod
    def is_loaded(cls) -> bool:
        return cls._loaded
