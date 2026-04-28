#!/usr/bin/env python3
"""
Confluence 페이지의 첨부파일을 REST API로 다운로드.

사전 준비:
  1. https://id.atlassian.com/manage-profile/security/api-tokens 에서 API 토큰 발급
  2. confluence_sync/.env 파일에 ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN 설정
     (.env.example 참조)

저장 위치:
  confluence_sync/raw_attachments/<page_id>/<filename>
  confluence_sync/raw_attachments/<page_id>/.meta.json   (id/version/mediaType 메타)

사용법:
  python download_attachments.py              # 모든 페이지의 첨부 동기화
  python download_attachments.py <page_id>    # 특정 페이지만
  python download_attachments.py --missing    # .missing_attachments.json 에 적힌 것만
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
RAW_ATTACH_DIR = SCRIPT_DIR / "raw_attachments"
MISSING_LOG = SCRIPT_DIR / ".missing_attachments.json"
ENV_CANDIDATES = [
    SCRIPT_DIR / ".env",
    SCRIPT_DIR.parent / "jira_sync" / ".env",  # 폴백: jira_sync 의 토큰 재사용
]


def load_credentials() -> tuple[str, str, str]:
    """Email, token, base_url 을 로드."""
    for env_path in ENV_CANDIDATES:
        if env_path.exists():
            load_dotenv(env_path)
            break
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    base = os.environ.get("CONFLUENCE_BASE_URL", "").strip()

    if not base:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        base = config.get("confluence_base_url", "").rstrip("/")

    if not email or not token:
        print("❌ ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN 가 .env 에 없습니다.")
        print(f"   {SCRIPT_DIR / '.env.example'} 를 참고해서 .env 를 만들어주세요.")
        sys.exit(1)
    if not base:
        print("❌ Confluence base URL 을 결정할 수 없습니다.")
        sys.exit(1)
    return email, token, base.rstrip("/")


def list_attachments(session: requests.Session, base: str, page_id: str) -> list[dict]:
    """페이지의 모든 첨부 메타데이터 반환."""
    out = []
    start = 0
    limit = 50
    while True:
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment"
        r = session.get(url, params={"start": start, "limit": limit, "expand": "version"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("results", []))
        if data.get("size", 0) < limit:
            break
        start += limit
    return out


def download_one(session: requests.Session, base: str, att: dict, dest: Path) -> bool:
    """첨부 한 개 다운로드. 이미 동일 버전이면 스킵."""
    download_path = att["_links"]["download"]
    # Confluence Cloud 의 _links.download 는 "/download/attachments/{id}/{name}?..."
    # 형태로 /wiki 접두어가 빠진 채 반환됨 → 직접 붙여줌.
    if not download_path.startswith("/wiki/"):
        download_path = "/wiki" + download_path
    url = f"{base}{download_path}"

    # 동일 버전 스킵 — .meta.json 에 기록된 버전 비교
    meta_file = dest.parent / ".meta.json"
    meta = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    cur_ver = att.get("version", {}).get("number", 1)
    saved_ver = meta.get(att["title"], {}).get("version")
    if dest.exists() and saved_ver == cur_ver:
        return False  # skip

    r = session.get(url, stream=True)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)

    # 메타 저장
    meta[att["title"]] = {
        "id": att["id"],
        "version": cur_ver,
        "mediaType": att.get("metadata", {}).get("mediaType")
                     or att.get("extensions", {}).get("mediaType"),
        "fileSize": att.get("extensions", {}).get("fileSize"),
    }
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def sync_page(session: requests.Session, base: str, page_id: str, title: str = "") -> dict:
    """한 페이지의 모든 첨부 동기화."""
    try:
        atts = list_attachments(session, base, page_id)
    except requests.HTTPError as e:
        print(f"  ❌ {page_id} ({title}): {e.response.status_code} {e.response.reason}")
        return {"downloaded": 0, "skipped": 0, "failed": 1}

    if not atts:
        return {"downloaded": 0, "skipped": 0, "failed": 0}

    page_dir = RAW_ATTACH_DIR / page_id
    page_dir.mkdir(parents=True, exist_ok=True)
    stats = {"downloaded": 0, "skipped": 0, "failed": 0}
    for att in atts:
        filename = att["title"]
        dest = page_dir / filename
        try:
            if download_one(session, base, att, dest):
                stats["downloaded"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            print(f"  ⚠️ {filename}: {e}")
            stats["failed"] += 1
    return stats


def main():
    args = sys.argv[1:]
    email, token, base = load_credentials()
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    pages = {pid: info for pid, info in config.get("page_tree", {}).items()
             if not pid.endswith("_children")}

    # 대상 페이지 결정
    if args and args[0] == "--missing":
        if not MISSING_LOG.exists():
            print("ℹ️ .missing_attachments.json 가 없습니다 — 누락 없음.")
            return
        missing = json.loads(MISSING_LOG.read_text(encoding="utf-8"))
        target_ids = list(missing.keys())
    elif args and not args[0].startswith("-"):
        target_ids = [args[0]]
    else:
        target_ids = list(pages.keys())

    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json"})

    print(f"\n📥 Confluence 첨부 다운로드 시작 ({len(target_ids)}개 페이지)\n")

    total = {"downloaded": 0, "skipped": 0, "failed": 0}
    for pid in target_ids:
        info = pages.get(pid, {})
        title = info.get("title", "(unknown)")
        stats = sync_page(session, base, pid, title)
        total["downloaded"] += stats["downloaded"]
        total["skipped"] += stats["skipped"]
        total["failed"] += stats["failed"]
        if stats["downloaded"] or stats["skipped"] or stats["failed"]:
            mark = "✅" if not stats["failed"] else "⚠️"
            print(f"  {mark} {pid} ({title}): "
                  f"⬇{stats['downloaded']} ⏭{stats['skipped']} ✗{stats['failed']}")

    print(f"\n총합: 신규 {total['downloaded']} / 스킵 {total['skipped']} / 실패 {total['failed']}")
    print(f"📂 저장 위치: {RAW_ATTACH_DIR.relative_to(SCRIPT_DIR.parent)}")
    print("👉 다음 단계: python build_html_md.py 실행해서 HTML/MD 갱신\n")


if __name__ == "__main__":
    main()
