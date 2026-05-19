"""
parsers/g2b.py

G2B (나라장터) 입찰공고 raw 응답 → Opportunity 변환.

응답 핵심 필드 (조달청_OpenAPI 사양서 기반):
  bidNtceNo, bidNtceOrd, bidNtceNm, bidNtceDate, bidNtceBgn,
  bidNtceSttusNm, bsnsDivNm, ntceInsttNm, dmndInsttNm,
  bidClseDate, bidClseTm, opengDate, opengTm,
  presmptPrce, asignBdgtAmt, rgnLmtYn,
  cntrctCnclsMthdNm, bidwinrDcsnMthdNm, intrntnlBidYn

매핑 규칙:
- ext_id: bidNtceNo + bidNtceOrd (차수 포함 고유 식별)
- opp_type: 항상 'RFP' (입찰공고)
- deadline: bidClseDate (입찰마감일)
- organization: ntceInsttNm
- industries: bsnsDivNm 매핑 (물품/용역/공사/외자)
- bid_method: cntrctCnclsMthdNm
- budget: presmptPrce (추정가격) → budget_max
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from parsers._base import BaseParser
from parsers.opportunity_dto import Opportunity


# ============================================================
# 마감일 파싱
# ============================================================
def _parse_date(date_str: Optional[str]) -> Optional[date]:
    """'2025-07-08' → date 객체."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


# ============================================================
# 산업 매핑 (bsnsDivNm)
# ============================================================
def _map_industries(bsns_div_nm: Optional[str], title: str = "") -> list[str]:
    """
    G2B의 업무구분명을 ẞDM 산업 분류에 매핑.
    bsnsDivNm: 물품 | 용역 | 공사 | 외자
    """
    industries = []
    bsns = bsns_div_nm or ""
    text = bsns + " " + (title or "")
    
    # G2B 카테고리 직접 매핑
    if "물품" in bsns:
        industries.append("물품")
    if "용역" in bsns:
        industries.append("용역")
    if "공사" in bsns:
        industries.append("공사")
    if "외자" in bsns:
        industries.append("외자")
    
    # title 기반 보조 분류 (Bizinfo와 일관성 위해)
    if any(k in text for k in ["IT", "소프트웨어", "SW", "AI", "디지털", "정보통신", "시스템"]):
        if "IT" not in industries:
            industries.append("IT")
    if any(k in text for k in ["제조", "공장", "설비"]):
        if "제조" not in industries:
            industries.append("제조")
    
    return industries or ["기타"]


# ============================================================
# 지역 추론 (지역제한 + 공고기관명에서)
# ============================================================
def _infer_regions(raw: Dict[str, Any]) -> list[str]:
    """
    rgnLmtYn(지역제한여부) + ntceInsttNm(공고기관명)에서 지역 추론.
    """
    regions = []
    
    inst_name = raw.get("ntceInsttNm", "") or ""
    dmnd_name = raw.get("dmndInsttNm", "") or ""
    text = inst_name + " " + dmnd_name
    
    city_map = {
        "서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천",
        "광주": "광주", "대전": "대전", "울산": "울산", "세종": "세종",
        "경기": "경기", "강원": "강원", "충북": "충북", "충남": "충남",
        "전북": "전북", "전남": "전남", "경북": "경북", "경남": "경남",
        "제주": "제주",
    }
    for key, label in city_map.items():
        if key in text:
            regions.append(label)
    
    # 지역제한 = N이면 전국
    if not regions:
        if raw.get("rgnLmtYn") == "Y":
            return ["지역제한"]  # 명시적 제한 있는데 어디인지 불명
        return ["전국"]
    
    return regions


# ============================================================
# 예산 추출
# ============================================================
def _extract_budget(raw: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """
    presmptPrce(추정가격) 또는 asignBdgtAmt(배정예산금액)에서 budget 추출.
    G2B는 일반적으로 추정가격 = 상한선.
    """
    # 추정가격 우선
    presmpt = raw.get("presmptPrce")
    if presmpt:
        try:
            return None, int(str(presmpt).replace(",", ""))
        except (ValueError, TypeError):
            pass
    
    # 폴백: 배정예산
    asign = raw.get("asignBdgtAmt")
    if asign:
        try:
            return None, int(str(asign).replace(",", ""))
        except (ValueError, TypeError):
            pass
    
    return None, None


# ============================================================
# 자격 조건 추출
# ============================================================
def _extract_eligibility(raw: Dict[str, Any]) -> dict:
    """입찰참가자격 관련 정보 dict로 묶음."""
    el = {}
    
    # 국제입찰 여부
    if raw.get("intrntnlBidYn"):
        el["international"] = raw["intrntnlBidYn"] == "Y"
    
    # 공동계약 여부
    if raw.get("cmmnCntrctYn"):
        el["joint_contract"] = raw["cmmnCntrctYn"] == "Y"
    
    # 전자입찰 여부
    if raw.get("elctrnBidYn"):
        el["electronic"] = raw["elctrnBidYn"] == "Y"
    
    # 입찰참가자격등록 마감
    rgst_date = raw.get("bidPrtcptQlfctRgstClseDate")
    if rgst_date:
        el["qualification_deadline"] = rgst_date
    
    return el or None


# ============================================================
# 지역제한 정보
# ============================================================
def _extract_region_restriction(raw: Dict[str, Any]) -> dict:
    """지역제한 관련 정보 dict로 묶음."""
    rr = {}
    if raw.get("rgnLmtYn"):
        rr["restricted"] = raw["rgnLmtYn"] == "Y"
    return rr or None


# ============================================================
# Parser 클래스
# ============================================================
class G2BParser(BaseParser):
    source_key = "g2b"
    source_type = "api"
    
    def parse_one(self, raw: dict) -> Optional[Opportunity]:
        # 필수: 입찰공고번호 + 차수
        bid_no = raw.get("bidNtceNo")
        bid_ord = raw.get("bidNtceOrd", "000")
        if not bid_no:
            return None
        
        ext_id = f"{bid_no}-{bid_ord}"
        title = raw.get("bidNtceNm") or "제목 없음"
        
        # 마감일: 입찰마감일자
        deadline = _parse_date(raw.get("bidClseDate"))
        
        # URL: 나라장터 공고 상세 (입찰공고번호 기반 — G2B 표준 URL 패턴)
        url = (
            f"https://www.g2b.go.kr:8101/ep/invitation/publish/bidInfoDtl.do"
            f"?bidno={bid_no}&bidseq={bid_ord}"
        )
        
        # 예산
        budget_min, budget_max = _extract_budget(raw)
        
        # 태그
        tags = ["나라장터", "입찰공고"]
        if raw.get("bsnsDivNm"):
            tags.append(raw["bsnsDivNm"])
        if raw.get("bidNtceSttusNm"):
            tags.append(raw["bidNtceSttusNm"])
        
        # 단계 — G2B는 사업체 대상이라 폭넓게
        stages = ["창업기", "성장기", "중견기업"]
        
        return Opportunity(
            source_key="g2b",
            source_type="api",
            opp_type="RFP",  # 입찰공고는 모두 RFP
            title=title,
            description=None,  # G2B 응답에 본문 description 없음
            organization=raw.get("ntceInsttNm") or raw.get("dmndInsttNm"),
            deadline=deadline,
            regions=_infer_regions(raw),
            industries=_map_industries(raw.get("bsnsDivNm"), title),
            stages=stages,
            tags=tags,
            url=url,
            ext_id=ext_id,
            raw_json=dict(raw),
            budget_min=budget_min,
            budget_max=budget_max,
            procurement_category=raw.get("bsnsDivNm"),
            bid_method=raw.get("cntrctCnclsMthdNm"),
            eligibility_flags=_extract_eligibility(raw),
            region_restriction=_extract_region_restriction(raw),
        )
