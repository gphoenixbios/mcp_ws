#!/usr/bin/env python3
"""
Jira Cloud → 로컬 캐시 다운로더.

기능:
  1. 보드 정보 조회 (/rest/agile/1.0/board/{id})
  2. 보드의 모든 스프린트 조회 (active / future / closed)
  3. 보드의 모든 이슈 키 조회 → 각 이슈별 상세(description/comments 렌더 HTML 포함) 다운로드
  4. raw_board.json / raw_sprints.json / raw_issues/<KEY>.json 캐시
  5. sync_config.json 의 메타 갱신

사용법:
  python pull_jira.py
"""

import json
import os
import sys
import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
RAW_BOARD = SCRIPT_DIR / "raw_board.json"
RAW_SPRINTS = SCRIPT_DIR / "raw_sprints.json"
RAW_ISSUES_DIR = SCRIPT_DIR / "raw_issues"


def load_credentials() -> tuple[str, str, str]:
    """Email, token, base_url 로드. confluence_sync/.env 도 폴백."""
    candidates = [
        SCRIPT_DIR / ".env",
        SCRIPT_DIR.parent / "confluence_sync" / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            load_dotenv(env_path)
            break

    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    base = os.environ.get("JIRA_BASE_URL", "").strip()

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not base:
        base = config.get("jira_base_url", "").rstrip("/")

    if not email or not token:
        print("❌ ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN 가 .env 에 없습니다.")
        sys.exit(1)
    if not base:
        print("❌ jira_base_url 을 sync_config.json 에 설정하세요.")
        sys.exit(1)
    return email, token, base.rstrip("/")


def get_board(session: requests.Session, base: str, board_id: int) -> dict:
    url = f"{base}/rest/agile/1.0/board/{board_id}"
    r = session.get(url)
    r.raise_for_status()
    return r.json()


def get_all_sprints(session: requests.Session, base: str, board_id: int) -> list[dict]:
    out = []
    start = 0
    while True:
        url = f"{base}/rest/agile/1.0/board/{board_id}/sprint"
        r = session.get(url, params={"startAt": start, "maxResults": 50,
                                     "state": "active,future,closed"})
        if r.status_code == 400:
            # 칸반 보드 등은 sprint 미지원
            return []
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("values", []))
        if data.get("isLast", True):
            break
        start += len(data.get("values", []))
        if not data.get("values"):
            break
    return out


def list_board_issue_keys(session: requests.Session, base: str, board_id: int) -> list[str]:
    """보드에 속한 모든 이슈 키 (paginated)."""
    keys = []
    start = 0
    while True:
        url = f"{base}/rest/agile/1.0/board/{board_id}/issue"
        r = session.get(url, params={
            "startAt": start,
            "maxResults": 100,
            "fields": "summary",  # 키만 필요하므로 최소
        })
        r.raise_for_status()
        data = r.json()
        issues = data.get("issues", [])
        keys.extend(i["key"] for i in issues)
        total = data.get("total", 0)
        start += len(issues)
        if not issues or start >= total:
            break
    return keys


def fetch_issue_full(session: requests.Session, base: str, key: str) -> dict:
    """이슈 전체 (모든 필드 + 렌더링된 HTML + 댓글 + 변경이력)."""
    url = f"{base}/rest/api/3/issue/{key}"
    r = session.get(url, params={
        "expand": "renderedFields,names,schema",
        "fields": "*all",
    })
    r.raise_for_status()
    return r.json()


def fetch_comments(session: requests.Session, base: str, key: str) -> list[dict]:
    """댓글 목록 (rendered HTML 포함)."""
    url = f"{base}/rest/api/3/issue/{key}/comment"
    r = session.get(url, params={"expand": "renderedBody", "maxResults": 100})
    if r.status_code != 200:
        return []
    return r.json().get("comments", [])


def target_fingerprint(base: str, project_key: str, board_id: int) -> str:
    return f"{base.rstrip('/')}|{project_key}|{board_id}"


def cleanup_stale_cache():
    """타겟 변경 시 raw_board / raw_sprints / raw_issues 정리."""
    cleaned = 0
    if RAW_BOARD.exists():
        RAW_BOARD.unlink()
        cleaned += 1
    if RAW_SPRINTS.exists():
        RAW_SPRINTS.unlink()
        cleaned += 1
    if RAW_ISSUES_DIR.exists():
        for f in RAW_ISSUES_DIR.glob("*.json"):
            f.unlink()
            cleaned += 1
    return cleaned


def main():
    email, token, base = load_credentials()
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    board_id = int(config["board_id"])
    project_key = config.get("project_key", "")

    current_fp = target_fingerprint(base, project_key, board_id)
    last_fp = config.get("_last_synced_target", "")

    print(f"\n🎯 Target: {base} / project={project_key} / board={board_id}")
    if last_fp:
        last_disp = last_fp.replace("|", " / ", 1).replace("|", " / board=", 1)
        # 표시용: "url / project / board=N"
        parts = last_fp.split("|")
        if len(parts) == 3:
            last_disp = f"{parts[0]} / project={parts[1]} / board={parts[2]}"
        print(f"📜 Last synced: {last_disp}")
        if last_fp != current_fp:
            print(f"⚠ 타겟 변경 감지 → 이전 캐시 정리")
            n = cleanup_stale_cache()
            print(f"   - {n} 항목 삭제")

    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json"})

    print(f"\n🔍 보드 정보 조회: {base} / board {board_id}")
    board = get_board(session, base, board_id)
    board_name = board.get("name", "")
    board_type = board.get("type", "")
    print(f"   - name: {board_name}")
    print(f"   - type: {board_type}")
    RAW_BOARD.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n📅 스프린트 조회...")
    sprints = get_all_sprints(session, base, board_id)
    print(f"   - {len(sprints)} 스프린트")
    for s in sprints:
        print(f"     · [{s.get('state')}] {s.get('name')} (id={s.get('id')})")
    RAW_SPRINTS.write_text(json.dumps(sprints, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n🔑 보드 이슈 키 조회...")
    keys = list_board_issue_keys(session, base, board_id)
    print(f"   - 총 {len(keys)} 이슈")

    RAW_ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n📥 이슈 상세 다운로드...")
    for i, key in enumerate(keys, 1):
        try:
            issue = fetch_issue_full(session, base, key)
            comments = fetch_comments(session, base, key)
            issue["_comments_full"] = comments
            (RAW_ISSUES_DIR / f"{key}.json").write_text(
                json.dumps(issue, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"   [{i}/{len(keys)}] {key}: {issue.get('fields', {}).get('summary', '')[:60]}")
        except requests.HTTPError as e:
            print(f"   [{i}/{len(keys)}] {key}: ❌ {e}")

    # ─── Prune: 보드에서 사라진 이슈의 raw 캐시 삭제 ────────────────
    current_keys = set(keys)
    pruned = 0
    for f in RAW_ISSUES_DIR.glob("*.json"):
        if f.stem not in current_keys:
            f.unlink()
            pruned += 1
            print(f"   🗑  prune (raw_issues): {f.name}")
    if pruned:
        print(f"   - {pruned} stale issue cache 삭제")

    config["board_name"] = board_name
    config["board_type"] = board_type
    config["_last_synced_target"] = current_fp
    config["stats"] = {
        "sprint_count": len(sprints),
        "issue_count": len(keys),
        "last_synced": datetime.datetime.now().isoformat(),
    }
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ pull_jira 완료. 다음: python build_md.py\n")


if __name__ == "__main__":
    main()
