"""
scripts/healthcheck.py

ssdm 파이프라인 자가점검.

목적:
1. 파이프라인이 조용히 죽는 것을 감지 (예: GitHub 60일 무활동 워크플로우 비활성화)
2. 점검 리포트를 checks/ 아래 커밋 → 리포지토리 활동 발생 → 비활성화 자체를 예방
3. 치명적 이상은 Issue로 승격

판정:
- FAIL : DB 연결 불가 / 최근 N일간 processed 전무  → Issue 생성
- WARN : pending 적체 / error 급증 / raw 용량 초과  → 리포트 기록만
- INFO : 액션 버전 등 참고 정보

환경변수:
- SUPABASE_URL, SUPABASE_SERVICE_KEY            (필수)
- HC_STALE_DAYS          기본 3      최근 processed 없으면 FAIL 판정 기준(일)
- HC_PENDING_WARN        기본 50000  pending 적체 WARN 임계
- HC_ERROR_WARN          기본 1000   최근 7일 error WARN 임계
- HC_ERROR_WINDOW_DAYS   기본 7      error 집계 기간(일)
- HC_RAW_SIZE_WARN_MB    기본 500    raw 테이블 용량 WARN 임계(MB)
- HC_OUT_DIR             기본 checks 리포트 출력 디렉토리

출력:
- checks/health_YYYY-MM.md   (같은 달이면 덮어씀)
- GITHUB_OUTPUT: status / issue_title / issue_body_file
- exit code는 항상 0 (워크플로우 자체를 실패시키지 않음. 판정은 status로 전달)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client


KST = timezone(timedelta(hours=9))

STALE_DAYS = int(os.environ.get("HC_STALE_DAYS", "3"))
PENDING_WARN = int(os.environ.get("HC_PENDING_WARN", "50000"))
ERROR_WARN = int(os.environ.get("HC_ERROR_WARN", "1000"))
ERROR_WINDOW_DAYS = int(os.environ.get("HC_ERROR_WINDOW_DAYS", "7"))
RAW_SIZE_WARN_MB = int(os.environ.get("HC_RAW_SIZE_WARN_MB", "500"))
OUT_DIR = os.environ.get("HC_OUT_DIR", "checks")


# ---------------------------------------------------------------- 결과 수집기

class Report:
    """점검 항목 결과를 모아 마크다운으로 렌더링."""

    LEVEL_ORDER = {"FAIL": 0, "WARN": 1, "INFO": 2, "OK": 3}
    ICON = {"FAIL": "🔴", "WARN": "🟡", "OK": "🟢", "INFO": "ℹ️"}

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []
        self.started = datetime.now(KST)

    def add(self, level: str, name: str, detail: str, value: str = "") -> None:
        self.items.append(
            {"level": level, "name": name, "detail": detail, "value": value}
        )

    @property
    def status(self) -> str:
        """전체 판정. FAIL > WARN > OK."""
        levels = {i["level"] for i in self.items}
        if "FAIL" in levels:
            return "FAIL"
        if "WARN" in levels:
            return "WARN"
        return "OK"

    def failures(self) -> List[Dict[str, Any]]:
        return [i for i in self.items if i["level"] == "FAIL"]

    def warnings(self) -> List[Dict[str, Any]]:
        return [i for i in self.items if i["level"] == "WARN"]

    def to_markdown(self) -> str:
        ts = self.started.strftime("%Y-%m-%d %H:%M:%S KST")
        lines = [
            f"# ssdm 파이프라인 점검 리포트",
            "",
            f"- 실행 시각: {ts}",
            f"- 종합 판정: **{self.status}** {self.ICON[self.status]}",
            "",
            "## 점검 결과",
            "",
            "| 판정 | 항목 | 값 | 내용 |",
            "|---|---|---|---|",
        ]
        ordered = sorted(
            self.items, key=lambda i: self.LEVEL_ORDER.get(i["level"], 9)
        )
        for i in ordered:
            icon = self.ICON.get(i["level"], "")
            value = i["value"] or "-"
            lines.append(f"| {icon} {i['level']} | {i['name']} | {value} | {i['detail']} |")

        lines += [
            "",
            "## 임계값",
            "",
            "| 항목 | 임계 |",
            "|---|---|",
            f"| 최근 처리 없음 (FAIL) | {STALE_DAYS}일 |",
            f"| pending 적체 (WARN) | {PENDING_WARN:,}건 |",
            f"| error 급증 (WARN) | 최근 {ERROR_WINDOW_DAYS}일 {ERROR_WARN:,}건 |",
            f"| raw 용량 (WARN) | {RAW_SIZE_WARN_MB}MB |",
            "",
            "---",
            "",
            "이 리포트는 `healthcheck.yml`이 자동 생성합니다. "
            "커밋 자체가 리포지토리 활동으로 기록되어, "
            "GitHub의 60일 무활동 스케줄 비활성화를 예방합니다.",
            "",
        ]
        return "\n".join(lines)

    def to_issue_body(self) -> str:
        ts = self.started.strftime("%Y-%m-%d %H:%M:%S KST")
        lines = [
            f"자동 점검에서 이상이 감지되었습니다. (실행: {ts})",
            "",
            "## 🔴 FAIL",
            "",
        ]
        for i in self.failures():
            lines.append(f"- **{i['name']}** — {i['detail']} (값: {i['value'] or '-'})")

        warns = self.warnings()
        if warns:
            lines += ["", "## 🟡 WARN", ""]
            for i in warns:
                lines.append(f"- **{i['name']}** — {i['detail']} (값: {i['value'] or '-'})")

        lines += [
            "",
            "## 확인할 것",
            "",
            "1. Actions 탭에서 `Collector — Parse Raw to Opportunities` 워크플로우가 "
            "비활성화(disabled)되지 않았는지",
            "2. 최근 실행 로그에 인증/스키마 오류가 있는지",
            "3. 스크래퍼(Cloud Shell) 쪽이 `repository_dispatch`를 보내고 있는지",
            "",
            f"상세 리포트: `{OUT_DIR}/health_{self.started.strftime('%Y-%m')}.md`",
            "",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------- 개별 점검

def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY 필요")
    return create_client(url, key)


def _count(sb, table: str, filters: Optional[List[Tuple[str, str, Any]]] = None) -> int:
    """count-only 조회. 행 본문을 받지 않아 대량 테이블에도 안전."""
    q = sb.table(table).select("id", count="exact").limit(1)
    for op, col, val in filters or []:
        if op == "eq":
            q = q.eq(col, val)
        elif op == "gte":
            q = q.gte(col, val)
    res = q.execute()
    return res.count or 0


def check_connection(sb, rep: Report) -> bool:
    """DB 연결 및 기본 테이블 접근 가능 여부."""
    try:
        _count(sb, "opportunities_raw", [("eq", "process_status", "pending")])
        rep.add("OK", "DB 연결", "Supabase 접근 정상")
        return True
    except Exception as e:
        rep.add("FAIL", "DB 연결", f"접근 실패: {type(e).__name__}: {e}")
        return False


def check_recent_processing(sb, rep: Report) -> None:
    """최근 STALE_DAYS 안에 processed 처리가 있었는지. 없으면 파이프라인 중단."""
    since = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    try:
        n = _count(
            sb,
            "opportunities_raw",
            [("eq", "process_status", "processed"), ("gte", "processed_at", since.isoformat())],
        )
        if n == 0:
            # 마지막 처리 시각을 찾아 얼마나 멈췄는지 표시
            last = "확인 불가"
            try:
                res = (
                    sb.table("opportunities_raw")
                    .select("processed_at")
                    .eq("process_status", "processed")
                    .order("processed_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if res.data:
                    last = str(res.data[0].get("processed_at", "?"))
            except Exception:
                pass
            rep.add(
                "FAIL",
                "최근 처리",
                f"최근 {STALE_DAYS}일간 processed 건이 없음. "
                f"컬렉터 중단 의심 (마지막 처리: {last})",
                "0건",
            )
        else:
            rep.add("OK", "최근 처리", f"최근 {STALE_DAYS}일간 정상 처리 중", f"{n:,}건")
    except Exception as e:
        rep.add("FAIL", "최근 처리", f"조회 실패: {type(e).__name__}: {e}")


def check_pending_backlog(sb, rep: Report) -> None:
    """pending 적체량."""
    try:
        n = _count(sb, "opportunities_raw", [("eq", "process_status", "pending")])
        if n > PENDING_WARN:
            rep.add(
                "WARN",
                "pending 적체",
                f"임계 {PENDING_WARN:,}건 초과. 처리량이 유입량을 못 따라가는지 확인 필요",
                f"{n:,}건",
            )
        else:
            rep.add("OK", "pending 적체", "정상 범위", f"{n:,}건")
    except Exception as e:
        rep.add("WARN", "pending 적체", f"조회 실패: {type(e).__name__}: {e}")


def check_error_rate(sb, rep: Report) -> None:
    """최근 기간 error 건수. 파서 깨짐 조기 감지."""
    since = datetime.now(timezone.utc) - timedelta(days=ERROR_WINDOW_DAYS)
    try:
        n = _count(
            sb,
            "opportunities_raw",
            [("eq", "process_status", "error"), ("gte", "processed_at", since.isoformat())],
        )
        if n > ERROR_WARN:
            rep.add(
                "WARN",
                "error 급증",
                f"최근 {ERROR_WINDOW_DAYS}일 error가 임계 {ERROR_WARN:,}건 초과. "
                f"파서 또는 원본 스키마 변경 의심",
                f"{n:,}건",
            )
        else:
            rep.add("OK", "error 건수", f"최근 {ERROR_WINDOW_DAYS}일 정상 범위", f"{n:,}건")
    except Exception as e:
        rep.add("WARN", "error 건수", f"조회 실패: {type(e).__name__}: {e}")


def check_table_size(sb, rep: Report) -> None:
    """
    raw 테이블 물리 용량.

    pg_stat_user_tables 조회에는 RPC가 필요하다. RPC가 없으면 행 수로 대체 판정.
    """
    try:
        res = sb.rpc("get_table_sizes").execute()
        rows = res.data or []
        target = next(
            (r for r in rows if r.get("table_name") == "opportunities_raw"), None
        )
        if target:
            mb = float(target.get("total_mb", 0))
            if mb > RAW_SIZE_WARN_MB:
                rep.add(
                    "WARN",
                    "raw 테이블 용량",
                    f"임계 {RAW_SIZE_WARN_MB}MB 초과. "
                    f"processed 정리 및 VACUUM 검토 필요",
                    f"{mb:,.0f}MB",
                )
            else:
                rep.add("OK", "raw 테이블 용량", "정상 범위", f"{mb:,.0f}MB")
            return
        raise RuntimeError("opportunities_raw 항목 없음")
    except Exception as e:
        # RPC 미설치 등 — 행 수로 간접 판정
        try:
            total = _count(sb, "opportunities_raw")
            rep.add(
                "INFO",
                "raw 테이블 용량",
                f"용량 RPC 사용 불가({type(e).__name__}) — 행 수로 대체 표시. "
                f"정확한 용량은 SQL Editor에서 pg_stat_user_tables 조회",
                f"{total:,}행",
            )
        except Exception as e2:
            rep.add("WARN", "raw 테이블 용량", f"조회 실패: {type(e2).__name__}: {e2}")


def check_workflow_actions(rep: Report) -> None:
    """워크플로우가 참조하는 액션 버전 수집. 판정 없이 기록만."""
    wf_dir = ".github/workflows"
    if not os.path.isdir(wf_dir):
        rep.add("INFO", "워크플로우 액션", "워크플로우 디렉토리를 찾지 못함")
        return

    found: List[str] = []
    for fn in sorted(os.listdir(wf_dir)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        try:
            with open(os.path.join(wf_dir, fn), encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("- uses:") or s.startswith("uses:"):
                        ref = s.split("uses:", 1)[1].strip()
                        found.append(f"{fn}: {ref}")
        except Exception:
            continue

    if found:
        rep.add("INFO", "워크플로우 액션", "; ".join(found), f"{len(found)}개")
    else:
        rep.add("INFO", "워크플로우 액션", "참조 액션 없음")


# ---------------------------------------------------------------- 출력

def write_outputs(rep: Report) -> None:
    """리포트 파일 생성 + GITHUB_OUTPUT 기록."""
    os.makedirs(OUT_DIR, exist_ok=True)
    month = rep.started.strftime("%Y-%m")
    report_path = os.path.join(OUT_DIR, f"health_{month}.md")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(rep.to_markdown())
    print(f"[healthcheck] report written: {report_path}")

    issue_body_path = ""
    if rep.status == "FAIL":
        issue_body_path = os.path.join(OUT_DIR, "_issue_body.md")
        with open(issue_body_path, "w", encoding="utf-8") as f:
            f.write(rep.to_issue_body())
        print(f"[healthcheck] issue body written: {issue_body_path}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        date_str = rep.started.strftime("%Y-%m-%d")
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"status={rep.status}\n")
            f.write(f"report_path={report_path}\n")
            f.write(f"issue_body_file={issue_body_path}\n")
            f.write(f"issue_title=[healthcheck] 파이프라인 이상 감지 ({date_str})\n")


def main() -> int:
    rep = Report()
    print(f"[healthcheck] start ({rep.started.isoformat()})")

    try:
        sb = _client()
    except Exception as e:
        rep.add("FAIL", "환경 설정", f"{type(e).__name__}: {e}")
        write_outputs(rep)
        print(f"[healthcheck] status={rep.status}")
        return 0

    if check_connection(sb, rep):
        check_recent_processing(sb, rep)
        check_pending_backlog(sb, rep)
        check_error_rate(sb, rep)
        check_table_size(sb, rep)

    check_workflow_actions(rep)
    write_outputs(rep)

    print(f"\n[healthcheck] === {rep.status} ===")
    for i in rep.items:
        print(f"  [{i['level']}] {i['name']}: {i['value'] or '-'} — {i['detail']}")

    # 워크플로우 자체는 성공시킨다. 판정은 status output으로 전달.
    return 0


if __name__ == "__main__":
    sys.exit(main())
