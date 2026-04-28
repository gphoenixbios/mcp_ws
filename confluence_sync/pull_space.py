#!/usr/bin/env python3
"""
Confluence v2 API 로 스페이스 전체 트리를 발견하고
  - sync_config.json 의 page_tree 갱신
  - raw_html/<page_id>.json 로 storage HTML 캐시
저장한다.

사용법:
  python pull_space.py
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
RAW_HTML_DIR = SCRIPT_DIR / "raw_html"
ENV_CANDIDATES = [
    SCRIPT_DIR / ".env",
    SCRIPT_DIR.parent / "jira_sync" / ".env",  # 폴백: jira_sync 의 토큰 재사용
]


def load_credentials() -> tuple[str, str, str, str]:
    """email, token, base_url, space_key 반환."""
    for env_path in ENV_CANDIDATES:
        if env_path.exists():
            load_dotenv(env_path)
            break
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    base = os.environ.get("CONFLUENCE_BASE_URL", "").strip()

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not base:
        base = config.get("confluence_base_url", "").rstrip("/")
    space_key = config.get("space_key", "")

    if not email or not token:
        print("❌ ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN 가 .env 에 없습니다.")
        sys.exit(1)
    if not base or not space_key:
        print("❌ confluence_base_url / space_key 를 sync_config.json 에 설정하세요.")
        sys.exit(1)
    return email, token, base.rstrip("/"), space_key


def sanitize_path_part(s: str) -> str:
    """파일시스템 안전한 폴더/파일명으로 변환."""
    # 슬래시/콜론/이상 문자 치환
    s = s.replace("/", "_").replace("\\", "_").replace(":", "_")
    # 양끝 공백/점 제거
    s = s.strip().strip(".")
    return s or "untitled"


def get_space(session: requests.Session, base: str, key: str) -> dict:
    r = session.get(f"{base}/wiki/api/v2/spaces", params={"keys": key})
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        print(f"❌ space '{key}' not found")
        sys.exit(1)
    return results[0]


def list_all_pages(session: requests.Session, base: str, space_id: str) -> list[dict]:
    """스페이스의 모든 페이지 (parentId, title, id 포함) 페이지네이션."""
    out = []
    cursor = None
    while True:
        params = {"limit": 250, "body-format": "storage", "status": "current"}
        if cursor:
            params["cursor"] = cursor
        url = f"{base}/wiki/api/v2/spaces/{space_id}/pages"
        r = session.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("results", []))
        next_link = data.get("_links", {}).get("next")
        if not next_link:
            break
        # next link 형식: "/wiki/api/v2/spaces/.../pages?...cursor=XYZ"
        m = re.search(r"cursor=([^&]+)", next_link)
        if not m:
            break
        cursor = m.group(1)
    return out


def build_tree(pages: list[dict], homepage_id: str, space_root_name: str) -> dict:
    """
    flat page list → page_tree 딕셔너리
    homepage 는 root (parent_id=None) 으로 처리.
    각 페이지의 local_path 는 "{space_root}/.../{title}" 형태 (홈페이지는 "{space_root}").
    """
    by_id = {p["id"]: p for p in pages}
    children_of: dict[str, list[str]] = {}
    for p in pages:
        parent = p.get("parentId")
        # 홈페이지의 부모는 보통 null. 부모가 없는 다른 페이지는 홈페이지의 자식으로 본다.
        if p["id"] == homepage_id:
            continue
        key = parent if parent in by_id else homepage_id
        children_of.setdefault(key, []).append(p["id"])

    tree: dict = {}
    # 홈페이지를 root 로
    home = by_id.get(homepage_id)
    if not home:
        print(f"❌ homepage {homepage_id} 가 페이지 목록에 없음")
        sys.exit(1)

    tree[homepage_id] = {
        "title": home["title"],
        "parent_id": None,
        "local_path": space_root_name,
        "is_folder": True,  # root 는 항상 folder 처럼 처리 (자식이 없어도)
    }

    def recurse(pid: str, parent_path: str):
        for cid in children_of.get(pid, []):
            cinfo = by_id[cid]
            title = cinfo["title"]
            safe_title = sanitize_path_part(title)
            local_path = f"{parent_path}/{safe_title}"
            has_children = bool(children_of.get(cid))
            entry = {
                "title": title,
                "parent_id": pid,
                "local_path": local_path,
            }
            if has_children:
                entry["is_folder"] = True
            tree[cid] = entry
            if has_children:
                recurse(cid, local_path)

    recurse(homepage_id, space_root_name)
    # 만약 홈페이지에 아무 자식도 없으면 is_folder True 유지 (그냥 표지 페이지)
    if not children_of.get(homepage_id):
        # 단일 페이지 스페이스
        tree[homepage_id]["is_folder"] = True
    return tree


def save_raw_html(pages: list[dict]):
    """body.storage 가 이미 들어있는 페이지 응답을 raw_html/<id>.json 으로 저장."""
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for p in pages:
        pid = p["id"]
        body = p.get("body", {}).get("storage", {}) or {}
        html = body.get("value", "") or ""
        ver = (p.get("version") or {}).get("number", 1)
        out = {
            "id": pid,
            "title": p.get("title", ""),
            "ver": ver,
            "html": html,
        }
        (RAW_HTML_DIR / f"{pid}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        saved += 1
    return saved


def target_fingerprint(base: str, space_key: str) -> str:
    return f"{base.rstrip('/')}|{space_key}"


def cleanup_stale_cache():
    """타겟 변경 시 raw_html / raw_attachments 정리."""
    cleaned = 0
    if RAW_HTML_DIR.exists():
        for f in RAW_HTML_DIR.glob("*.json"):
            f.unlink()
            cleaned += 1
    raw_attach = SCRIPT_DIR / "raw_attachments"
    if raw_attach.exists():
        shutil.rmtree(raw_attach)
        cleaned += 1
    missing = SCRIPT_DIR / ".missing_attachments.json"
    if missing.exists():
        missing.unlink()
        cleaned += 1
    return cleaned


def main():
    email, token, base, space_key = load_credentials()

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    current_fp = target_fingerprint(base, space_key)
    last_fp = config.get("_last_synced_target", "")

    print(f"\n🎯 Target: {base} / space={space_key}")
    if last_fp:
        last_disp = last_fp.replace("|", " / space=")
        print(f"📜 Last synced: {last_disp}")
        if last_fp != current_fp:
            print(f"⚠ 타겟 변경 감지 → 이전 캐시 정리")
            n = cleanup_stale_cache()
            print(f"   - {n} 항목 삭제")

    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json"})

    print(f"\n🔍 스페이스 정보 조회: {base} / {space_key}")
    space = get_space(session, base, space_key)
    space_id = space["id"]
    space_name = space["name"]
    homepage_id = space["homepageId"]
    print(f"   - id: {space_id}")
    print(f"   - name: {space_name}")
    print(f"   - homepage: {homepage_id}")

    print(f"\n📥 페이지 목록 조회 (storage HTML 포함)...")
    pages = list_all_pages(session, base, space_id)
    print(f"   - 총 {len(pages)} 페이지")

    print(f"\n💾 raw_html 저장...")
    n = save_raw_html(pages)
    print(f"   - {n} 파일")

    print(f"\n🌳 페이지 트리 구성...")
    space_root = sanitize_path_part(space_name)
    tree = build_tree(pages, homepage_id, space_root)

    config["confluence_base_url"] = base
    config["space_key"] = space_key
    config["space_id"] = space_id
    config["space_name"] = space_root
    config["homepage_id"] = homepage_id
    config["local_root"] = "../confluence_content"
    config["page_tree"] = tree
    config["_last_synced_target"] = current_fp

    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"   - sync_config.json 갱신 완료 ({len(tree)} 페이지 트리)")
    print(f"\n✅ pull_space 완료. 다음:")
    print(f"   1) python download_attachments.py     # 첨부 동기화")
    print(f"   2) python build_html_md.py            # HTML/MD 빌드\n")


if __name__ == "__main__":
    main()
