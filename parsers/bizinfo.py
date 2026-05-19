"""
parsers/bizinfo.py

Bizinfo (기업마당) raw 응답 → Opportunity 변환.

응답 핵심 필드:
  pblancId, pblancNm, pblancUrl, jrsdInsttNm, excInsttNm,
  trgetNm, reqstBeginEndDe, bsnsSumryCn, hashtags,
  pldirSportRealmLclasCodeNm, pldirSportRealmMlsfcCodeNm

룰베이스 추론:
- 지역(_infer_regions): 17개 시도 매핑
- 산업(_infer_industries): IT/제조/바이오/에너지
- 단계(_infer_stages): 예비창업/창업기/성장기/중견기업
- 공고타입(_infer_opp_type): GRANT/EVENT/PROGRAM
- 예산(_extract_budget): 억원/천만원/백만원/만원/원 단위
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from parsers._base import BaseParser
from parsers.opportunity_dto import Opportunity


# ============================================================
# 정규식 - 금액 추출
# ============================================================
_MONEY_UNIT_RE = r"억원|천만원|백만원|만원|억|천만|백만|만|원"
_MONEY_COMPONENT_RE = re.compile(
    rf"([0-9][0-9,]*(?:\.[0-9]+)?)\s*({_MONEY_UNIT_RE})"
)
_MONEY_PHRASE_RE = re.compile(
    rf"(?:[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:{_MONEY_UNIT_RE})\s*)+"
)
_RANGE_RE = re.compile(
    rf"((?:[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:{_MONEY_UNIT_RE})\s*)+)"
    r"\s*(?:~|∼|-)\s*"
    rf"((?:[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:{_MONEY_UNIT_RE})\s*)+)"
)


# ============================================================
# 마감일 추출
# ============================================================
def _parse_deadline(val: Optional[str]) -> Optional[date]:
    """'2026-03-12 ~ 2026-03-31' 형식에서 종료일 추출."""
    if not val or val.strip() == "상시 접수":
        return None
    if "~" in val:
        val = val.split("~")[-1].strip()
    match = re.search(r"(\d{4}-\d{2}-\d{2})", val)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


# ============================================================
# 지역 추론 (17개 시도)
# ============================================================
def _infer_regions(hashtags: str, jurisdiction: str) -> List[str]:
    regions = []
    city_map = {
        "서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천",
        "광주": "광주", "대전": "대전", "울산": "울산", "세종": "세종",
        "경기": "경기", "강원": "강원", "충북": "충북", "충남": "충남",
        "전북": "전북", "전남": "전남", "경북": "경북", "경남": "경남",
        "제주": "제주",
    }
    text = (hashtags or "") + " " + (jurisdiction or "")
    for key, label in city_map.items():
        if key in text:
            regions.append(label)
    return regions or ["전국"]


# ============================================================
# 산업 추론
# ============================================================
def _infer_industries(title: str, tags: str) -> List[str]:
    text = (title or "") + " " + (tags or "")
    industries = []
    if any(k in text for k in ["IT", "소프트웨어", "SW", "AI", "디지털", "플랫폼", "정보통신"]):
        industries.append("IT")
    if any(k in text for k in ["제조", "공장", "설비", "뿌리산업"]):
        industries.append("제조")
    if any(k in text for k in ["바이오", "헬스", "의료", "제약"]):
        industries.append("바이오")
    if any(k in text for k in ["에너지", "그린", "환경", "탄소"]):
        industries.append("에너지")
    return industries or ["기타"]


# ============================================================
# 단계 추론
# ============================================================
def _infer_stages(target: str) -> List[str]:
    stages = []
    if any(k in (target or "") for k in ["예비창업", "창업준비"]):
        stages.append("예비창업")
    if any(k in (target or "") for k in ["창업벤처", "창업기업", "스타트업", "창업 7년"]):
        stages.append("창업기")
    if any(k in (target or "") for k in ["성장", "중소기업", "벤처기업"]):
        stages.append("성장기")
    if any(k in (target or "") for k in ["중견기업"]):
        stages.append("중견기업")
    if any(k in (target or "") for k in ["여성", "소상공인", "재창업"]):
        stages.append("창업기")
    return stages or ["창업기", "성장기"]


# ============================================================
# 공고 타입 추론
# ============================================================
def _infer_opp_type(subcategory: str) -> str:
    sub = subcategory or ""
    if "R&D" in sub or "기술개발" in sub:
        return "GRANT"
    if "행사" in sub or "박람회" in sub or "교육" in sub:
        return "EVENT"
    if "사업화" in sub or "지원금" in sub or "보조" in sub:
        return "GRANT"
    return "PROGRAM"


# ============================================================
# 예산 추출
# ============================================================
def _strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    no_entities = re.sub(r"&nbsp;|&#160;", " ", no_tags)
    return re.sub(r"\s+", " ", no_entities).strip()


def _normalize_money_text(text: str) -> str:
    normalized = text or ""
    replacements = [
        ("억 원", "억원"),
        ("천만 원", "천만원"),
        ("백만 원", "백만원"),
        ("만 원", "만원"),
    ]
    for src, dst in replacements:
        normalized = normalized.replace(src, dst)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _amount_phrase_to_won(phrase: str) -> Optional[int]:
    unit_scale = {
        "억원": 100_000_000, "천만원": 10_000_000, "백만원": 1_000_000,
        "만원": 10_000, "억": 100_000_000, "천만": 10_000_000,
        "백만": 1_000_000, "만": 10_000, "원": 1,
    }
    total = 0.0
    matched = False

    for num_text, unit in _MONEY_COMPONENT_RE.findall(phrase):
        try:
            num = float(num_text.replace(",", ""))
        except ValueError:
            continue
        total += num * unit_scale[unit]
        matched = True

    if not matched:
        return None
    return int(total)


def _extract_budget_from_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None

    normalized = _normalize_money_text(_strip_html(text))
    if not normalized:
        return None, None

    # 명시적 범위 (예: "3천만원~1억원")
    for left_phrase, right_phrase in _RANGE_RE.findall(normalized):
        left = _amount_phrase_to_won(left_phrase)
        right = _amount_phrase_to_won(right_phrase)
        values = [v for v in (left, right) if v is not None and v >= 0]
        if len(values) == 2:
            return min(values), max(values)

    values: List[int] = []
    for phrase in _MONEY_PHRASE_RE.findall(normalized):
        won = _amount_phrase_to_won(phrase)
        if won is None or won < 0:
            continue
        values.append(won)

    if not values:
        return None, None

    # Bizinfo 예산은 대부분 상한선 표현
    return None, max(values)


def _extract_budget(raw: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    description = raw.get("bsnsSumryCn")
    budget_min, budget_max = _extract_budget_from_text(str(description or ""))
    if budget_min is not None or budget_max is not None:
        return budget_min, budget_max

    hashtags = raw.get("hashtags")
    return _extract_budget_from_text(str(hashtags or ""))


# ============================================================
# Parser 클래스
# ============================================================
class BizinfoParser(BaseParser):
    source_key = "bizinfo"
    source_type = "api"
    
    def parse_one(self, raw: dict) -> Optional[Opportunity]:
        title = raw.get("pblancNm") or "제목 없음"
        ext_id = raw.get("pblancId")
        org = raw.get("excInsttNm") or raw.get("jrsdInsttNm")
        deadline = _parse_deadline(raw.get("reqstBeginEndDe"))
        url = raw.get("pblancUrl") or "https://www.bizinfo.go.kr"

        hashtags = raw.get("hashtags") or ""
        jurisdiction = raw.get("jrsdInsttNm") or ""
        subcategory = raw.get("pldirSportRealmMlsfcCodeNm") or ""
        budget_min, budget_max = _extract_budget(raw)

        raw_payload = dict(raw or {})
        raw_payload["_extracted_budget_min"] = budget_min
        raw_payload["_extracted_budget_max"] = budget_max

        tags = ["기업마당", "지원사업"]
        if hashtags:
            tags += [t.strip() for t in hashtags.split(",") if t.strip()][:6]

        return Opportunity(
            source_key="bizinfo",
            source_type="api",
            opp_type=_infer_opp_type(subcategory),
            title=title,
            description=raw.get("bsnsSumryCn"),
            organization=org,
            deadline=deadline,
            regions=_infer_regions(hashtags, jurisdiction),
            industries=_infer_industries(title, hashtags),
            stages=_infer_stages(raw.get("trgetNm")),
            tags=tags,
            url=url,
            ext_id=ext_id,
            raw_json=raw_payload,
            budget_min=budget_min,
            budget_max=budget_max,
        )
