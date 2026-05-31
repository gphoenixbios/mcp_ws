#!/usr/bin/env python3
"""
로컬 MD/HTML → Confluence 페이지 업로드 (첨부 자동 업로드 포함).

기능:
  1. MD 파일에서 참조하는 이미지 (`![](attachments/xxx)` / `![](xxx)`) 추출
  2. 페이지의 기존 첨부 목록과 비교 → 새 파일은 자동 업로드
  3. MD → Confluence storage HTML 변환 (이미지는 <ri:attachment ri:filename="..."/> 형태)
  4. 페이지 버전 +1 로 PUT 업데이트
  5. **HTML 입력 모드** (`--html`) — `<div class="mermaid">` 를 mmdc 로 PNG 렌더 + 첨부 + storage 변환
  6. **새 페이지 생성** (`--create --parent <id> --title <t>`) — REST POST 후 sync_config 자동 등록

사용법:
  python push_page.py <page_id>                       # 기존 페이지 갱신 (기존 동작)
  python push_page.py --all                           # 변경된 모든 페이지
  python push_page.py --dry-run <page_id>             # 변환만 미리보기

  # HTML 직접 업로드 (mermaid 자동 변환)
  python push_page.py --html <html_path> <page_id>    # 기존 페이지 갱신 (HTML 입력)
  python push_page.py --html <html_path> \\
      --create --parent <parent_page_id> --title "<title>"   # 새 페이지 생성

  # 옵션 조합
  python push_page.py --html <html_path> --create --parent 70025328 \\
      --title "Follow — 추종" --dry-run             # 새 페이지 생성 dry-run
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
from html_push import html_to_storage

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


def upload_attachment(
    session: requests.Session, base: str, page_id: str, filepath: Path,
    *, existing_id: str | None = None,
) -> dict:
    """페이지에 첨부 업로드. existing_id 주어지면 새 버전으로 PUT (data endpoint), 아니면 POST 신규.

    Confluence Cloud 는 같은 이름의 첨부에 POST 하면 400 BadRequest — 새 버전을 만들려면
    /child/attachment/{att_id}/data 로 POST 해야 함.
    """
    headers = {"X-Atlassian-Token": "nocheck"}
    if existing_id:
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment/{existing_id}/data"
        with open(filepath, "rb") as f:
            files = {"file": (filepath.name, f)}
            data = {"minorEdit": "true"}
            r = session.post(url, headers=headers, files=files, data=data)
    else:
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment"
        with open(filepath, "rb") as f:
            files = {"file": (filepath.name, f)}
            data = {"minorEdit": "true"}
            r = session.post(url, headers=headers, files=files, data=data)
    r.raise_for_status()
    res = r.json()
    return res.get("results", [res])[0] if "results" in res else res


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


def create_page(
    session: requests.Session, base: str, *,
    space_id: str, parent_id: str, title: str, storage_html: str,
) -> dict:
    """REST POST /wiki/api/v2/pages — 새 페이지 생성. 응답 dict 반환 (id 포함)."""
    url = f"{base}/wiki/api/v2/pages"
    payload = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "parentId": parent_id,
        "body": {"representation": "storage", "value": storage_html},
    }
    r = session.post(url, json=payload, headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        raise requests.HTTPError(
            f"create_page failed: {r.status_code} {r.text[:500]}", response=r,
        )
    return r.json()


def register_page_in_sync_config(
    page_id: str, *, title: str, parent_id: str, local_path: str,
    is_folder: bool = False,
) -> None:
    """sync_config.json 의 page_tree 에 새 page 등록 후 저장."""
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    page_tree = config.setdefault("page_tree", {})
    entry: dict = {
        "title": title,
        "parent_id": parent_id,
        "local_path": local_path,
    }
    if is_folder:
        entry["is_folder"] = True
    page_tree[page_id] = entry
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def find_parent_local_path(parent_id: str, pages: dict, space_root: str) -> str:
    """parent page 의 local_path 를 반환. 없으면 space_root (최상위 root)."""
    info = pages.get(parent_id)
    if info and "local_path" in info:
        return info["local_path"]
    return space_root


def push_html_one(
    session: requests.Session, base: str, html_path: Path,
    *,
    page_id: str | None = None,
    parent_id: str | None = None,
    title: str | None = None,
    space_id: str | None = None,
    pages: dict | None = None,
    space_root: str = "",
    dry_run: bool = False,
) -> bool:
    """HTML 파일 → mermaid PNG 렌더 → 새 페이지 생성 또는 기존 페이지 갱신.

    page_id 주어지면 PUT 갱신, parent_id+title 주어지면 POST 새 생성.
    """
    # 첨부 (mermaid PNG) 저장 위치 — html 파일과 같은 디렉토리의 .mermaid_out/
    attachments_dir = html_path.parent / ".mermaid_out"
    print(f"  🎨 HTML → storage 변환 ({html_path.name})")
    storage_html, attached_pngs = html_to_storage(
        html_path, attachments_dir=attachments_dir,
    )
    print(f"    mermaid PNG: {len(attached_pngs)}개")
    print(f"    storage HTML: {len(storage_html)} bytes")

    if dry_run:
        print("\n  --- dry-run preview ---")
        print(f"  attached PNGs: {[p.name for p in attached_pngs]}")
        print(f"  storage HTML 첫 500자:\n{storage_html[:500]}")
        if page_id:
            print(f"  → page_id {page_id} 갱신 예정")
        elif parent_id and title:
            print(f"  → parent {parent_id} 아래 새 페이지 '{title}' 생성 예정")
        return True

    # 새 페이지 생성 path
    if page_id is None:
        if not (parent_id and title and space_id):
            print("❌ --create 모드: --parent, --title, space_id 필수")
            return False
        try:
            page = create_page(
                session, base, space_id=space_id, parent_id=parent_id,
                title=title, storage_html=storage_html,
            )
        except requests.HTTPError as e:
            print(f"    ❌ 페이지 생성 실패: {e}")
            return False
        page_id = page["id"]
        print(f"    ✨ 새 페이지 생성: id={page_id}")

        # sync_config 등록 — parent 의 local_path 기반
        if pages is not None:
            parent_local = find_parent_local_path(parent_id, pages, space_root)
            new_local_path = f"{parent_local}/{title}"
            register_page_in_sync_config(
                page_id, title=title, parent_id=parent_id, local_path=new_local_path,
            )
            print(f"    📝 sync_config.json 등록: {new_local_path}")

    # mermaid PNG 첨부 업로드 — existing 첨부면 새 버전 PUT, 없으면 신규 POST
    server_atts = list_server_attachments(session, base, page_id)
    for png in attached_pngs:
        existing_id = server_atts.get(png.name)
        try:
            upload_attachment(session, base, page_id, png, existing_id=existing_id)
            verb = "갱신" if existing_id else "업로드"
            print(f"    📎 첨부 {verb}: {png.name}")
        except requests.HTTPError as e:
            print(f"    ❌ 첨부 업로드 실패 {png.name}: {e.response.status_code} {e.response.text[:200]}")
            return False

    # 첨부 업로드 후 storage HTML 의 mermaid ri:attachment 가 활성화됨 → 새 페이지면 본문 갱신 PUT
    # (POST 시점엔 첨부가 아직 없어 본문의 ri:attachment 가 broken 일 수 있음)
    page = get_page(session, base, page_id)
    cur_ver = page["version"]["number"]
    actual_title = title or page.get("title", "")
    try:
        update_page(session, base, page_id, actual_title, storage_html, cur_ver + 1)
        print(f"    ✅ 본문 갱신: v{cur_ver} → v{cur_ver+1}")
    except requests.HTTPError as e:
        print(f"    ❌ 본문 갱신 실패: {e.response.status_code} {e.response.text[:300]}")
        return False
    return True


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


def _pop_kv(args: list[str], key: str) -> str | None:
    """--key value 형태에서 value 만 추출. 없으면 None. args 리스트 인플레이스 변경."""
    if key not in args:
        return None
    i = args.index(key)
    if i + 1 >= len(args):
        print(f"❌ {key} 옵션에 값이 필요합니다.")
        sys.exit(1)
    value = args[i + 1]
    del args[i:i + 2]
    return value


def _pop_flag(args: list[str], key: str) -> bool:
    if key in args:
        args.remove(key)
        return True
    return False


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    dry_run = _pop_flag(args, "--dry-run")

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    pages = {pid: info for pid, info in config.get("page_tree", {}).items()
             if not pid.endswith("_children")}
    space_root = config.get("space_name", "최종2팀")
    space_id = config.get("space_id", "")

    # HTML 입력 모드 분기 — 새 페이지 생성 또는 기존 페이지 갱신
    html_path_arg = _pop_kv(args, "--html")
    if html_path_arg:
        html_path = Path(html_path_arg).resolve()
        if not html_path.exists():
            print(f"❌ HTML 파일 없음: {html_path}")
            sys.exit(1)

        is_create = _pop_flag(args, "--create")
        parent_id = _pop_kv(args, "--parent")
        title = _pop_kv(args, "--title")

        email, token, base = load_credentials()
        session = requests.Session()
        session.auth = (email, token)
        session.headers.update({"Accept": "application/json"})

        if is_create:
            if not parent_id or not title:
                print("❌ --create 모드: --parent <page_id> --title <title> 필수")
                sys.exit(1)
            print(f"\n✨ 새 페이지 생성 ({title}){' [DRY RUN]' if dry_run else ''}\n")
            ok = push_html_one(
                session, base, html_path,
                parent_id=parent_id, title=title, space_id=space_id,
                pages=pages, space_root=space_root, dry_run=dry_run,
            )
        else:
            # 기존 페이지 갱신 (HTML 입력) — args 에 page_id 가 남아 있어야 함
            if not args:
                print("❌ --html 모드: page_id 또는 --create 둘 중 하나 필요")
                sys.exit(1)
            page_id = args[0]
            print(f"\n📤 HTML push 시작 (page_id={page_id}){' [DRY RUN]' if dry_run else ''}\n")
            ok = push_html_one(
                session, base, html_path, page_id=page_id, dry_run=dry_run,
            )
        sys.exit(0 if ok else 1)

    # 기존 MD 입력 모드
    if args[0] == "--all":
        changed, _ = check_status()
        target_ids = []
        # check_status returns paths relative to confluence_content; map to page_ids
        for rel in changed:
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
