"""
parsers/opportunity_dto.py

수집기-DB 사이의 통일된 데이터 구조.
모든 source(Bizinfo, G2B, NTIS, ...)가 이 dataclass로 변환된 결과를 produce하면,
_repository가 Supabase opportunities 테이블에 upsert.

향후 source 추가 시에도 이 모델만 만족하면 됨.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Dict, List, Optional


@dataclass
class Opportunity:
    """공고 데이터 통합 모델"""

    # ── 필수 ────────────────────────────────────────────────
    source_key: str          # 'bizinfo' | 'g2b' | 'ntis' | ...
    source_type: str         # 'api' | 'crawler'
    opp_type: str            # 'GRANT' | 'EVENT' | 'RFP' | 'PROGRAM'
    title: str

    # ── 선택 ────────────────────────────────────────────────
    description: Optional[str] = None
    organization: Optional[str] = None
    deadline: Optional[date] = None

    # 분류 태그
    regions: List[str] = field(default_factory=list)
    industries: List[str] = field(default_factory=list)
    stages: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    # 외부 식별
    url: Optional[str] = None
    ext_id: Optional[str] = None

    # 원본 보관
    raw_json: Optional[dict] = None

    # ── enrichment ──────────────────────────────────────────
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    procurement_category: Optional[str] = None
    eligibility_flags: Optional[Dict[str, object]] = None
    region_restriction: Optional[Dict[str, object]] = None
    bid_method: Optional[str] = None
    eligibility_criteria: Optional[Dict[str, object]] = None

    def to_row(self) -> dict:
        """Supabase upsert용 dict 변환. None 제거 + date ISO 변환."""
        row = asdict(self)
        # date → ISO string
        if isinstance(row.get("deadline"), date):
            row["deadline"] = row["deadline"].isoformat()
        # None 제거 (Supabase가 명시적 NULL 갱신 안 하도록)
        return {k: v for k, v in row.items() if v is not None}
