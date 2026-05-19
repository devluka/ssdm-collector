"""
parsers/_base.py

Parser 추상 인터페이스.

각 source별 parser는 BaseParser 상속:
- source_key 정의
- parse_one(raw_data: dict) -> Opportunity 구현
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from parsers.opportunity_dto import Opportunity


class BaseParser(ABC):
    """모든 source parser의 공통 인터페이스."""
    
    source_key: str = ""           # 서브클래스에서 정의
    source_type: str = "api"       # 'api' | 'crawler'
    
    @abstractmethod
    def parse_one(self, raw_data: dict) -> Optional[Opportunity]:
        """
        단일 raw_data → Opportunity 변환.
        실패 시 None 반환 (run.py가 error 마킹).
        """
        ...
    
    def parse_batch(self, items: list[dict]) -> list[tuple[Optional[Opportunity], Optional[str]]]:
        """
        여러 raw_data 일괄 처리.
        반환: [(Opportunity, None), (None, error_msg), ...]
        """
        results = []
        for raw in items:
            try:
                opp = self.parse_one(raw)
                if opp is None:
                    results.append((None, "parser returned None"))
                else:
                    results.append((opp, None))
            except Exception as e:
                results.append((None, f"{type(e).__name__}: {e}"))
        return results
