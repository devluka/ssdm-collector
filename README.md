# ssdm-collector

ẞDM의 정부지원사업 공고 정제 워커.

`opportunities_raw` 테이블의 raw 데이터를 룰베이스 추론으로 정제하여 
`opportunities` 테이블에 upsert.

## 역할

```
ssdm-scraper (한국 IP)
  ↓ 정부 API raw 응답 → opportunities_raw INSERT
  ↓ 끝나면 repository_dispatch 'scrap-finished' 이벤트
  
ssdm-collector (이 레포)
  ↓ pending raw SELECT (1000건씩)
  ↓ source별 parser로 정제 (Bizinfo·G2B·...)
  ↓ opportunities upsert (ON CONFLICT)
  ↓ raw 처리 상태 마킹 (processed/error)
```

수집과 정제를 분리한 이유:
- 수집은 **한국 IP 필요** (정부 API 차단 회피) → GitHub Actions runner
- 정제는 IP 무관 → 별도 워커
- 한쪽 망가져도 다른 쪽 안 죽음

## 구조

```
ssdm-collector/
├── .github/workflows/
│   └── collect.yml              repository_dispatch + cron 백업
├── parsers/
│   ├── __init__.py              PARSER_REGISTRY
│   ├── _base.py                 BaseParser 추상 클래스
│   ├── _schema.py               SchemaCache (동적 컬럼 매핑)
│   ├── _repository.py           Supabase upsert + raw 마킹
│   ├── opportunity_dto.py       공통 DTO
│   ├── bizinfo.py               Bizinfo parser (룰베이스 추론)
│   └── g2b.py                   G2B parser (입찰공고 → RFP)
├── run.py                       진입점
├── requirements.txt
├── .gitignore
└── README.md
```

## 동작 트리거

| 트리거 | 시점 |
|--------|------|
| `repository_dispatch: scrap-finished` | ssdm-scraper 수집 완료 직후 |
| `workflow_dispatch` | 수동 실행 |
| `schedule: cron` | KST 05:30, 16:00 (백업 — 이벤트 놓쳤을 때) |

## GitHub Secrets

```
SUPABASE_URL
SUPABASE_SERVICE_KEY
```

## 동적 컬럼 매핑

`SchemaCache`가 시작 시 `get_opportunities_columns()` RPC 호출해서 
`opportunities` 테이블의 현재 컬럼 셋을 메모리에 캐시.

**컬럼 추가/삭제되어도 collector 코드 수정 불필요** — 알 수 없는 키는 자동으로 무시.

## 새 source 추가 패턴

예: NTIS 추가 시

1. `parsers/ntis.py` 작성 (BaseParser 상속, `parse_one()` 구현)
2. `parsers/__init__.py`에 등록:
   ```python
   from parsers.ntis import NTISParser
   PARSER_REGISTRY = {
       "bizinfo": BizinfoParser(),
       "g2b": G2BParser(),
       "ntis": NTISParser(),  # 추가
   }
   ```
3. ssdm-scraper에 `scrapers/ntis.py` + 워크플로 추가

## 로컬 실행

```bash
pip install -r requirements.txt

export SUPABASE_URL=https://xlrbsirplkrbkpmuylwk.supabase.co
export SUPABASE_SERVICE_KEY=...

python run.py
```

## 운영 관계

```
ssdm-scraper (public)        ← 수집만
ssdm-collector (public)      ← 정제만 (이 레포)
sdm-backend (private)        ← 사용자 API 서빙
                               opportunities 테이블에서 SELECT
```
