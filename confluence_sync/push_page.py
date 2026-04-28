#!/usr/bin/env python3
"""
로컬 MD → Confluence 페이지 업로드 (첨부 자동 업로드 포함).

기능:
  1. MD 파일에서 참조하는 이미지(`![](attachments/xxx)` / `![](xxx)`) 추출
  2. 페이지의 기존 첨부 목록과 비교 → 새 파일은 자동 업로드
  3. MD → Confluence storage HTML 변환 (이미지는 <ri:attachment ri:filename="..."/> 형태)
  4. 페이지 버전 +1 로 PUT 업데이트

사용법:
  python push_page.py <page_id>
  python push_page.py --all              # 변경된 모든 페이지
  python push_page.py --dry-run <page_id>  # 변환만 미리보기
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from confluence_sync import (
    markdown_to_confluence_html,
    read_local_page,
    check_status,
)

SCRIPT_DIR = Path(__file__).parent.resolve()
WS_ROOT = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
CONTENT_ROOT = WS_ROOT / "confluence_content"
ENV_FILE = SCRIPT_DIR / ".env"
META_DIR = SCRIPT_DIR / ".page_meta"


def load_credentials() -> tuple[str, str, str]:
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    base = os.environ.get("CONFLUENCE_BASE_URL", "").strip()
    if not base:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        base = config.get("confluence_base_url", "").rstrip("/")
    if not email or not token:
        print("❌ ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN 가 .env 에 없습니다.")
        sys.exit(1)
    return email, token, base.rstrip("/")


def find_md_for_page(page_id: str, pages: dict, space_root: str) -> Path | None:
    """page_id 에 해당하는 새 구조의 index.md 경로."""
    info = pages.get(page_id)
    if not info:
        return None
    local_path = info["local_path"]  # "최종2팀/Implementation/..."
    rel = local_path[len(space_root) + 1:] if local_path.startswith(space_root + "/") else local_path
    md_path = CONTENT_ROOT / space_root / "md" / rel / "index.md"
    return md_path if md_path.exists() else None


def md_attachments_dir_for_page(page_id: str, pages: dict, space_root: str) -> Path:
    info = pages[page_id]
    rel = info["local_path"]
    if rel.startswith(space_root + "/"):
        rel = rel[len(space_root) + 1:]
    return CONTENT_ROOT / space_root / "md" / rel / "attachments"


def extract_referenced_images(md_body: str) -> set[str]:
    """MD 본문에서 참조되는 이미지 파일명만 추출 (URL 은 제외)."""
    out = set()
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", md_body):
        path = m.group(1).strip()
        if path.startswith(("http://", "https://")):
            continue
        out.add(path.rsplit("/", 1)[-1])
    return out


def list_server_attachments(session: requests.Session, base: str, page_id: str) -> dict:
    """{filename: attachment_id} 반환."""
    out = {}
    start = 0
    limit = 50
    while True:
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment"
        r = session.get(url, params={"start": start, "limit": limit})
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        data = r.json()
        for a in data.get("results", []):
            out[a["title"]] = a["id"]
        if data.get("size", 0) < limit:
            break
        start += limit
    return out


def upload_attachment(session: requests.Session, base: str, page_id: str, filepath: Path) -> dict:
    """페이지에 첨부 업로드 (multipart). 동일 이름이 있으면 새 버전이 만들어짐."""
    url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment"
    headers = {"X-Atlassian-Token": "nocheck"}
    with open(filepath, "rb") as f:
        files = {"file": (filepath.name, f)}
        data = {"minorEdit": "true"}
        r = session.post(url, headers=headers, files=files, data=data)
    r.raise_for_status()
    res = r.json()
    return res.get("results", [res])[0]


def get_page(session: requests.Session, base: str, page_id: str) -> dict:
    url = f"{base}/wiki/rest/api/content/{page_id}"
    r = session.get(url, params={"expand": "version,space,body.storage"})
    r.raise_for_status()
    return r.json()


def update_page(session: requests.Session, base: str, page_id: str,
                title: str, storage_html: str, new_version: int) -> dict:
    url = f"{base}/wiki/rest/api/content/{page_id}"
    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": new_version, "minorEdit": False},
        "body": {"storage": {"value": storage_html, "representation": "storage"}},
    }
    r = session.put(url, json=payload)
    r.raise_for_status()
    return r.json()


def push_one(session: requests.Session, base: str, page_id: str,
             pages: dict, space_root: str, dry_run: bool = False) -> bool:
    info = pages.get(page_id)
    if not info:
        print(f"❌ page_tree 에 {page_id} 없음")
        return False
    title = info["title"]
    md_path = find_md_for_page(page_id, pages, space_root)
    if not md_path:
        print(f"❌ {title}: MD 파일 없음")
        return False

    meta, body = read_local_page(str(md_path))

    # 참조된 이미지 추출
    referenced = extract_referenced_images(body)
    att_dir = md_attachments_dir_for_page(page_id, pages, space_root)

    # 서버 측 첨부 현황
    server_atts = {} if dry_run else list_server_attachments(session, base, page_id)

    # 업로드 필요한 파일 (서버에 없는데 로컬에 있는 것)
    to_upload = []
    missing_local = []
    for fn in referenced:
        local_file = att_dir / fn
        if fn not in server_atts:
            if local_file.exists():
                to_upload.append(local_file)
            else:
                missing_local.append(fn)

    if missing_local:
        print(f"  ⚠️ 서버에도 없고 로컬에도 없는 파일: {missing_local}")

    # 첨부 업로드
    for f in to_upload:
        if dry_run:
            print(f"    [dry-run] 업로드 예정: {f.name}")
        else:
            try:
                upload_attachment(session, base, page_id, f)
                print(f"    📎 업로드: {f.name}")
            except requests.HTTPError as e:
                print(f"    ❌ 업로드 실패 {f.name}: {e.response.status_code} {e.response.text[:200]}")
                return False

    # MD → storage HTML
    storage_html = markdown_to_confluence_html(body)

    if dry_run:
        print(f"\n  --- dry-run preview ({title}) ---")
        print(f"  참조 이미지: {sorted(referenced)}")
        print(f"  업로드 예정: {[f.name for f in to_upload]}")
        print(f"  storage HTML 첫 500자:\n{storage_html[:500]}")
        return True

    # 현재 버전 조회 후 업데이트
    page = get_page(session, base, page_id)
    cur_ver = page["version"]["number"]
    try:
        update_page(session, base, page_id, title, storage_html, cur_ver + 1)
        print(f"    ✅ 페이지 업데이트: v{cur_ver} → v{cur_ver+1}")
    except requests.HTTPError as e:
        print(f"    ❌ 페이지 업데이트 실패: {e.response.status_code} {e.response.text[:300]}")
        return False
    return True


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    pages = {pid: info for pid, info in config.get("page_tree", {}).items()
             if not pid.endswith("_children")}
    space_root = config.get("space_name", "최종2팀")

    if args[0] == "--all":
        changed, _ = check_status()
        target_ids = []
        # check_status returns paths relative to confluence_content; map to page_ids
        for rel in changed:
            # 기대 형태: "최종2팀/md/.../index.md"
            for pid, info in pages.items():
                lp = info["local_path"]
                expected_rel = lp.replace(space_root, f"{space_root}/md", 1) + "/index.md"
                if rel == expected_rel:
                    target_ids.append(pid)
                    break
        if not target_ids:
            print("ℹ️ 변경된 페이지 없음.")
            return
    else:
        target_ids = [args[0]]

    email, token, base = load_credentials()
    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json"})

    print(f"\n📤 Confluence push 시작 ({len(target_ids)}개){' [DRY RUN]' if dry_run else ''}\n")
    ok = 0
    for pid in target_ids:
        info = pages.get(pid, {})
        print(f"  📄 {pid} — {info.get('title', '?')}")
        if push_one(session, base, pid, pages, space_root, dry_run=dry_run):
            ok += 1
    print(f"\n결과: {ok}/{len(target_ids)} 성공\n")


if __name__ == "__main__":
    main()
