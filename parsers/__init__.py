"""
parsers 패키지 — source별 parser 레지스트리.

새 source 추가 시:
1. parsers/{source}.py 생성 (BaseParser 상속)
2. PARSER_REGISTRY에 등록
"""
from parsers.bizinfo import BizinfoParser
from parsers.g2b import G2BParser

PARSER_REGISTRY = {
    "bizinfo": BizinfoParser(),
    "g2b": G2BParser(),
}

__all__ = ["PARSER_REGISTRY"]
