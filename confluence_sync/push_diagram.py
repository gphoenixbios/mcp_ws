#!/usr/bin/env python3
"""
로컬 다이어그램을 Confluence 페이지에 푸시.

두 가지 모드를 지원한다:

  1) mermaid 모드 (기본) — 텍스트 편집용
     mermaid/<base>.mmd  →  drawio/<base>.drawio (자동 격자 레이아웃)
                         →  Confluence 첨부 + cust-content + 페이지 매크로 갱신
     ⚠️ 자동 변환은 lossy: 노드/엣지 관계는 보존되지만 원본 레이아웃·색·스타일은 손실.

  2) drawio 모드 (--from-drawio) — 원본 레이아웃 유지용
     drawio/<base>.drawio (사용자가 diagrams.net 에서 편집·저장한 결과)
                         →  변환 없이 그대로 Confluence 첨부에 푸시
     원본 레이아웃이 그대로 유지됨.

전체 흐름 (두 모드 공통, mermaid 변환 단계만 차이):
  a. 첨부 새 버전 업로드 (Confluence Cloud 의 /child/attachment/{id}/data)
  b. drawio Forge 앱의 cust-content body.revision 을 +1 → PUT
     (페이지 매크로가 가리키는 첨부 버전을 결정하는 진실의 원천)
  c. 페이지 storage HTML 안 매크로의 revision/content-ver 도 같은 값으로 PUT
     (다중 인스턴스가 있으면 모두 갱신)

사용법:
  python push_diagram.py <page_id> <base>                    # mermaid → drawio 변환 후 푸시
  python push_diagram.py <page_id> <base> --from-drawio      # .drawio 파일 그대로 푸시
  python push_diagram.py <page_id> --all                     # 페이지의 모든 mermaid 푸시
  python push_diagram.py <page_id> --all --from-drawio       # 페이지의 모든 drawio 푸시
  python push_diagram.py <page_id> <base> --dry-run          # 미리보기만 (서버·로컬 변경 없음)

참고:
  - PNG 프리뷰는 drawio 클라이언트가 만들기 때문에 푸시 직후엔 옛 PNG 가 잠깐 남을 수 있음.
    페이지에서 다이어그램을 클릭해 한 번 열면 새 PNG 가 생성됨.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from drawio_utils import mermaid_to_drawio

SCRIPT_DIR = Path(__file__).parent.resolve()
WS_ROOT = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
CONTENT_ROOT = WS_ROOT / "confluence_content"
ENV_FILE = SCRIPT_DIR / ".env"


def load_credentials() -> tuple[str, str, str]:
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    base = os.environ.get("CONFLUENCE_BASE_URL", "").strip()
    if not base:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        base = cfg.get("confluence_base_url", "").rstrip("/")
    if not email or not token:
        print("❌ ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN 가 .env 에 없습니다.")
        sys.exit(1)
    return email, token, base.rstrip("/")


def page_dirs(page_id: str, pages: dict, space_root: str) -> tuple[Path, Path]:
    info = pages.get(page_id)
    if not info:
        print(f"❌ page_tree 에 {page_id} 없음")
        sys.exit(1)
    rel = info["local_path"]
    if rel.startswith(space_root + "/"):
        rel = rel[len(space_root) + 1:]
    elif rel == space_root:
        rel = ""
    page_html_dir = (CONTENT_ROOT / space_root / "html" / rel) if rel else CONTENT_ROOT / space_root / "html"
    page_md_dir = (CONTENT_ROOT / space_root / "md" / rel) if rel else CONTENT_ROOT / space_root / "md"
    return page_html_dir, page_md_dir


def list_attachments(session: requests.Session, base: str, page_id: str) -> dict:
    """{filename: {id, version}} 반환."""
    out: dict[str, dict] = {}
    start, limit = 0, 50
    while True:
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment"
        r = session.get(url, params={"start": start, "limit": limit, "expand": "version"})
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        data = r.json()
        for a in data.get("results", []):
            out[a["title"]] = {
                "id": a["id"],
                "version": a.get("version", {}).get("number", 1),
            }
        if data.get("size", 0) < limit:
            break
        start += limit
    return out


_EXT_BLOCK_RE = re.compile(r"<ac:adf-extension>.*?</ac:adf-extension>", re.DOTALL)
_PARAM_RE_TPL = (
    r'<ac:adf-parameter\s+key="{key}"(?:\s+type="[^"]+")?>([^<]*)</ac:adf-parameter>'
)


def _macro_param(block: str, key: str) -> str | None:
    m = re.search(_PARAM_RE_TPL.format(key=re.escape(key)), block)
    return m.group(1).strip() if m else None


def _macro_matches_diagram(block: str, target_name: str) -> bool:
    """매크로 블록이 target_name 다이어그램을 가리키는지."""
    for k in ("diagram-name", "diagram-display-name", "guest-params"):
        v = _macro_param(block, k)
        if v and (v == target_name
                  or v == target_name + ".drawio"
                  or v.removesuffix(".drawio") == target_name.removesuffix(".drawio")):
            return True
    return False


def _extract_search_from_mermaid(mmd_text: str) -> str:
    """mermaid 라벨(따옴표 안 텍스트)에서 검색 인덱스용 텍스트 추출."""
    labels = [m.group(1) for m in re.finditer(r'"([^"]+)"', mmd_text)]
    return " ".join(labels)[:512]


def _extract_search_from_drawio(drawio_xml: str) -> str:
    """drawio XML 의 mxCell value / UserObject label 에서 라벨 추출."""
    raw_labels = re.findall(r'value="([^"]*)"', drawio_xml)
    raw_labels += re.findall(r'\blabel="([^"]*)"', drawio_xml)
    cleaned: list[str] = []
    for s in raw_labels:
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"&[a-z]+;", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            cleaned.append(s)
    return " ".join(cleaned)[:512]


def update_cust_content(
    session: requests.Session, base: str, cust_id: str,
    search_text: str | None = None,
) -> int | None:
    """drawio cust-content 의 revision 을 +1 하고 (search_text 있으면) search 갱신.
    새 revision 반환."""
    r = session.get(
        f"{base}/wiki/rest/api/content/{cust_id}",
        params={"expand": "body.raw,version,container,space"},
    )
    if r.status_code >= 400:
        print(f"    ❌ cust-content GET 실패 {r.status_code}: {r.text[:200]}")
        return None
    d = r.json()
    try:
        body_obj = json.loads(d["body"]["raw"]["value"])
    except Exception:
        body_obj = {}
    new_rev = int(body_obj.get("revision", 0)) + 1
    body_obj["revision"] = new_rev
    if search_text:
        body_obj["search"] = search_text
    space_key = (d.get("space") or {}).get("key") or (d.get("container") or {}).get("space", {}).get("key")

    payload = {
        "id": cust_id,
        "type": d["type"],
        "title": d["title"],
        "version": {"number": d["version"]["number"] + 1},
        "body": {"raw": {"value": json.dumps(body_obj, ensure_ascii=False),
                          "representation": "raw"}},
        "container": {"id": d["container"]["id"], "type": d["container"]["type"]},
    }
    if space_key:
        payload["space"] = {"key": space_key}

    r = session.put(
        f"{base}/wiki/rest/api/content/{cust_id}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    if r.status_code >= 400:
        print(f"    ❌ cust-content PUT 실패 {r.status_code}: {r.text[:300]}")
        return None
    return new_rev


def bump_page_macros(
    session: requests.Session, base: str, page_id: str,
    diagram_name: str, new_revision: int,
) -> bool:
    """페이지 storage HTML 안에서 diagram_name 매크로들의 revision/content-ver 를
    new_revision 으로 맞춘다. 변경이 있으면 페이지 PUT.
    """
    r = session.get(f"{base}/wiki/api/v2/pages/{page_id}",
                    params={"body-format": "storage"})
    if r.status_code >= 400:
        print(f"    ❌ page GET 실패 {r.status_code}: {r.text[:200]}")
        return False
    page = r.json()
    html = page["body"]["storage"]["value"]
    cur_ver = page["version"]["number"]
    title = page["title"]

    new_html = html
    changed = 0
    for m in _EXT_BLOCK_RE.finditer(html):
        block = m.group(0)
        if not _macro_matches_diagram(block, diagram_name):
            continue
        bumped = re.sub(
            _PARAM_RE_TPL.format(key="revision"),
            f'<ac:adf-parameter key="revision" type="integer">{new_revision}</ac:adf-parameter>',
            block, count=1,
        )
        bumped = re.sub(
            _PARAM_RE_TPL.format(key="content-ver"),
            f'<ac:adf-parameter key="content-ver" type="integer">{new_revision}</ac:adf-parameter>',
            bumped, count=1,
        )
        if bumped != block:
            new_html = new_html.replace(block, bumped, 1)
            changed += 1

    if changed == 0:
        print(f"    ℹ️ 페이지에서 일치하는 매크로 없음 (이미 최신일 수 있음)")
        return True
    if new_html == html:
        return True

    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": cur_ver + 1,
                    "message": f"Bump drawio macro revision to {new_revision}"},
        "body": {"storage": {"value": new_html, "representation": "storage"}},
    }
    r = session.put(
        f"{base}/wiki/rest/api/content/{page_id}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    if r.status_code >= 400:
        print(f"    ❌ page PUT 실패 {r.status_code}: {r.text[:300]}")
        return False
    print(f"    📄 페이지 매크로 revision={new_revision} (인스턴스 {changed}개), "
          f"page v{cur_ver} → v{cur_ver+1}")
    return True


def upload_or_update_attachment(
    session: requests.Session, base: str, page_id: str,
    filepath: Path, target_filename: str,
    server_atts: dict, dry_run: bool = False,
) -> bool:
    """target_filename 로 페이지에 첨부 업로드.
    이미 같은 이름이 있으면 해당 첨부의 새 버전(/data 엔드포인트)으로 올린다.
    """
    headers = {"X-Atlassian-Token": "nocheck"}
    exists = target_filename in server_atts
    if dry_run:
        action = "새 버전 업로드" if exists else "새 첨부 생성"
        print(f"    [dry-run] {action}: {target_filename} ({filepath})")
        return True

    if exists:
        att_id = server_atts[target_filename]["id"]
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment/{att_id}/data"
    else:
        url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment"

    with open(filepath, "rb") as f:
        # mxfile XML — Confluence 의 drawio 매크로는 .drawio 확장자 + xml 본문을 잘 처리
        files = {"file": (target_filename, f, "application/xml")}
        data = {"minorEdit": "true",
                "comment": "Updated from local mermaid via push_diagram.py"}
        r = session.post(url, headers=headers, files=files, data=data)
    if r.status_code >= 400:
        print(f"    ❌ {r.status_code}: {r.text[:300]}")
        return False
    return True


def push_one(
    session: requests.Session, base: str, page_id: str, base_name: str,
    pages: dict, space_root: str, dry_run: bool = False,
    mode: str = "mermaid",
) -> bool:
    """
    mode:
      - "mermaid": mermaid/<base>.mmd 를 drawio 로 자동 변환 (격자 레이아웃) 후 푸시
      - "drawio":  drawio/<base>.drawio 를 그대로 푸시 (원본 레이아웃 유지)
    """
    page_html_dir, page_md_dir = page_dirs(page_id, pages, space_root)
    search_text: str | None = None

    if mode == "mermaid":
        mmd_path = page_md_dir / "mermaid" / f"{base_name}.mmd"
        if not mmd_path.exists():
            print(f"❌ {mmd_path} 없음")
            return False
        mmd_text = mmd_path.read_text(encoding="utf-8")
        drawio_xml = mermaid_to_drawio(mmd_text)
        search_text = _extract_search_from_mermaid(mmd_text)

        md_drawio = page_md_dir / "drawio" / f"{base_name}.drawio"
        html_drawio = page_html_dir / "drawio" / f"{base_name}.drawio"
        if dry_run:
            tmp = SCRIPT_DIR / f".dryrun_{page_id}_{base_name}.drawio"
            tmp.write_text(drawio_xml, encoding="utf-8")
            upload_src = tmp
            print(f"    📝 [dry-run] mermaid→drawio 변환 결과: "
                  f"{tmp.relative_to(SCRIPT_DIR)} ({len(drawio_xml)}B)")
        else:
            md_drawio.parent.mkdir(parents=True, exist_ok=True)
            html_drawio.parent.mkdir(parents=True, exist_ok=True)
            md_drawio.write_text(drawio_xml, encoding="utf-8")
            html_drawio.write_text(drawio_xml, encoding="utf-8")
            upload_src = md_drawio
            print(f"    📝 mermaid→drawio 변환 후 로컬 갱신: drawio/{base_name}.drawio")

    elif mode == "drawio":
        # 사용자가 diagrams.net 에서 편집한 .drawio 파일을 그대로 푸시.
        md_drawio = page_md_dir / "drawio" / f"{base_name}.drawio"
        html_drawio = page_html_dir / "drawio" / f"{base_name}.drawio"
        if not md_drawio.exists():
            print(f"❌ {md_drawio} 없음 — diagrams.net 에서 편집·저장 후 다시 시도하세요.")
            return False
        drawio_xml = md_drawio.read_text(encoding="utf-8", errors="replace")
        search_text = _extract_search_from_drawio(drawio_xml)
        # html 쪽 사본 동기화 (md 가 진실의 원천)
        if not dry_run:
            html_drawio.parent.mkdir(parents=True, exist_ok=True)
            html_drawio.write_text(drawio_xml, encoding="utf-8")
        upload_src = md_drawio
        size = md_drawio.stat().st_size
        prefix = "[dry-run] " if dry_run else ""
        print(f"    📝 {prefix}drawio 파일 그대로 사용: drawio/{base_name}.drawio ({size}B)")

    else:
        print(f"❌ 알 수 없는 모드: {mode}")
        return False

    # Confluence 첨부에 어떤 이름으로 올릴지 결정 — 이미 같은 이름의 첨부가 있는지 확인
    server_atts = list_attachments(session, base, page_id)
    # 후보: "<base>", "<base>.drawio". 둘 다 있을 수 있고 (inc-drawio 는 보통 확장자 없음).
    target_name = None
    if base_name in server_atts:
        target_name = base_name
    elif f"{base_name}.drawio" in server_atts:
        target_name = f"{base_name}.drawio"
    else:
        # 새로 만드는 경우 — .drawio 확장자로 올리는 게 안전
        target_name = f"{base_name}.drawio"
        print(f"    ⚠️ 서버에 동일 이름 첨부 없음 → '{target_name}' 으로 새로 생성")

    ok = upload_or_update_attachment(
        session, base, page_id, upload_src, target_name, server_atts, dry_run=dry_run,
    )
    if not ok or dry_run:
        return ok

    # drawio Forge 앱은 매크로가 가리키는 cust-content 의 revision 으로 첨부 버전을
    # 결정한다. 첨부만 갱신해도 페이지에는 반영되지 않으므로 다음을 수행:
    #   1) 페이지 매크로에서 cust-content-id 추출
    #   2) cust-content body.revision 을 +1 → PUT
    #   3) 페이지 storage HTML 안 매크로의 revision/content-ver 를 같은 값으로 PUT
    r = session.get(f"{base}/wiki/api/v2/pages/{page_id}",
                    params={"body-format": "storage"})
    if r.status_code >= 400:
        print(f"    ⚠️ 페이지 매크로 검사 실패: 첨부만 갱신됨")
        return True
    storage_html = r.json()["body"]["storage"]["value"]

    cust_ids: set[str] = set()
    for m in _EXT_BLOCK_RE.finditer(storage_html):
        block = m.group(0)
        if not _macro_matches_diagram(block, base_name):
            continue
        cci = _macro_param(block, "cust-content-id")
        if cci:
            cust_ids.add(cci)
    if not cust_ids:
        print(f"    ⚠️ 페이지에서 매크로(diagram={base_name})를 못 찾음")
        return True

    for cci in cust_ids:
        new_rev = update_cust_content(session, base, cci, search_text)
        if new_rev is None:
            return False
        print(f"    🧩 cust-content {cci} → revision={new_rev}")
        if not bump_page_macros(session, base, page_id, base_name, new_rev):
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

    # 모드 플래그 — 둘 다 지정되면 에러
    from_drawio = "--from-drawio" in args
    if from_drawio:
        args.remove("--from-drawio")
    from_mermaid_explicit = "--from-mermaid" in args
    if from_mermaid_explicit:
        args.remove("--from-mermaid")
    if from_drawio and from_mermaid_explicit:
        print("❌ --from-drawio 와 --from-mermaid 는 함께 쓸 수 없습니다.")
        return
    mode = "drawio" if from_drawio else "mermaid"

    if len(args) < 2:
        print("사용법: python push_diagram.py <page_id> <base|--all> "
              "[--from-drawio] [--dry-run]")
        return

    page_id = args[0]
    target = args[1]

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    pages = {pid: info for pid, info in cfg.get("page_tree", {}).items()
             if not pid.endswith("_children")}
    space_root = cfg.get("space_name", "")
    page_html_dir, page_md_dir = page_dirs(page_id, pages, space_root)

    if target == "--all":
        # 모드별로 source 디렉토리에서 base 목록 수집
        src_dir = (page_md_dir / "drawio") if mode == "drawio" else (page_md_dir / "mermaid")
        ext = ".drawio" if mode == "drawio" else ".mmd"
        if not src_dir.exists():
            print(f"❌ {src_dir} 없음")
            return
        bases = [p.stem for p in src_dir.glob(f"*{ext}")]
        if not bases:
            print(f"ℹ️ 푸시할 {ext} 파일 없음")
            return
    else:
        bases = [target]

    email, token, base = load_credentials()
    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json"})

    mode_label = "drawio 직접 푸시 (원본 레이아웃)" if mode == "drawio" else "mermaid → drawio 변환"
    print(f"\n📤 다이어그램 푸시 — page {page_id} ({len(bases)}개) [{mode_label}]"
          f"{' [DRY RUN]' if dry_run else ''}\n")
    ok = 0
    for b in bases:
        print(f"  📐 {b}")
        if push_one(session, base, page_id, b, pages, space_root,
                    dry_run=dry_run, mode=mode):
            ok += 1
    print(f"\n결과: {ok}/{len(bases)} 성공")
    if not dry_run and ok:
        print("👉 첨부 새 버전 + cust-content revision + 페이지 매크로 revision 까지 갱신됨.")
        print("   브라우저에서 페이지 새로고침 (Ctrl+F5) 으로 확인.")
    print()


if __name__ == "__main__":
    main()
