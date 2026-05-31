#!/usr/bin/env python3
"""
HTML → Confluence storage format 변환 + mermaid PNG 자동 렌더링 헬퍼.

push_page.py 의 HTML 입력 모드에서 호출. 단독 실행 시 dry-run 변환 결과를 stdout 출력.

핵심:
  1. `<div class="mermaid">` 블록 추출 → mmdc 호출로 PNG 렌더 → 첨부 PNG 목록 반환
  2. HTML body 안의 element 정리 (script / style / nav.crumbs / footer 제거)
  3. mermaid 자리에 `<ac:image><ri:attachment ri:filename="..."/></ac:image>` 삽입
  4. 일부 커스텀 박스 (.tldr / .info / .note / .warning / .tip) → Confluence info/note/warning macro
  5. `<details>` 는 그대로 두면 storage 가 깨지므로 본문에 인라인 (summary + 내용)

호출자 (push_page.py) 가 PNG 파일들을 page 에 첨부한 뒤 storage HTML 을 PUT/POST 한다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from html import escape as html_escape
from pathlib import Path
from typing import Iterable

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:  # pragma: no cover — 의존성 안내
    print("❌ beautifulsoup4 (bs4) 가 필요합니다. pip install beautifulsoup4", file=sys.stderr)
    raise


# ---------- mmdc (mermaid-cli) 위치 검색 ----------

def find_mmdc() -> Path:
    """PATH 우선, 없으면 ~/.npm-global/bin/mmdc fallback."""
    candidate = shutil.which("mmdc")
    if candidate:
        return Path(candidate)
    home_global = Path.home() / ".npm-global" / "bin" / "mmdc"
    if home_global.exists():
        return home_global
    raise RuntimeError(
        "mmdc (mermaid-cli) 를 찾을 수 없습니다. 설치:\n"
        "  npm config set prefix ~/.npm-global\n"
        "  npm install -g @mermaid-js/mermaid-cli\n"
        "  export PATH=$HOME/.npm-global/bin:$PATH"
    )


# ---------- mermaid 추출 + PNG 렌더 ----------

def extract_mermaid_blocks(soup: BeautifulSoup) -> list[Tag]:
    """body 안의 <div class="mermaid"> 또는 <pre><code class="language-mermaid"> 블록."""
    out: list[Tag] = []
    for div in soup.select("div.mermaid"):
        out.append(div)
    for pre in soup.select("pre > code.language-mermaid"):
        # markdown rendering 패턴 — pre 전체를 mermaid 블록으로 취급
        parent_pre = pre.parent
        if isinstance(parent_pre, Tag):
            # code 의 text 를 div 처럼 다루기 위해 임시 div 생성 (replace_with 시 사용)
            tmp = soup.new_tag("div", **{"class": "mermaid"})
            tmp.string = pre.get_text()
            parent_pre.replace_with(tmp)
            out.append(tmp)
    return out


def render_mermaid_png(
    mermaid_code: str,
    out_path: Path,
    *,
    width: int = 2000,
    background: str = "white",
    mmdc_bin: Path | None = None,
) -> Path:
    """mmdc CLI 호출 — mermaid 텍스트 → PNG 파일.

    out_path 디렉토리가 없으면 만든다. 실패 시 RuntimeError.
    """
    mmdc = mmdc_bin or find_mmdc()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False, encoding="utf-8") as src:
        src.write(mermaid_code)
        src_path = src.name
    try:
        # puppeteer 의 sandbox 비활성화 (root/CI 환경 호환). 일반 사용자는 영향 X.
        env = {**os.environ, "PUPPETEER_DISABLE_HEADLESS_WARNING": "true"}
        result = subprocess.run(
            [
                str(mmdc),
                "-i", src_path,
                "-o", str(out_path),
                "-w", str(width),
                "-b", background,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if result.returncode != 0 or not out_path.exists():
            raise RuntimeError(
                f"mmdc 실패 (code={result.returncode}):\n"
                f"  stdout: {result.stdout[:500]}\n  stderr: {result.stderr[:500]}"
            )
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass
    return out_path


# ---------- HTML → Confluence storage 정리 ----------

# Confluence storage 가 무시하거나 깨지는 element / attribute 제거 대상.
_REMOVE_TAGS = ("script", "style", "meta", "link")
_REMOVE_BODY_TOP_SELECTORS = ("nav.crumbs", "footer", "div.toc")  # 페이지 내비/푸터/목차 — Confluence 가 자체 처리
_REMOVE_ATTRS = ("style", "onclick", "onload", "id")  # style 인라인 / 이벤트 / 우리쪽 id (앵커는 어차피 storage 에서 깨짐)


def _strip_unsafe(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_REMOVE_TAGS):
        tag.decompose()
    for sel in _REMOVE_BODY_TOP_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr in _REMOVE_ATTRS:
                del tag.attrs[attr]


# Confluence info / note / warning macro 매핑 (간단 변환)
_BOX_CLASS_TO_MACRO = {
    "tldr": "info",
    "info": "info",
    "note": "note",
    "tip": "tip",
    "warning": "warning",
    "panel": "panel",
}


def _convert_boxes_to_macros(soup: BeautifulSoup) -> None:
    """`<div class="info">...</div>` 등을 Confluence info/note/warning macro 로 변환."""
    for cls, macro_name in _BOX_CLASS_TO_MACRO.items():
        for div in list(soup.select(f"div.{cls}")):
            macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": macro_name})
            body = soup.new_tag("ac:rich-text-body")
            for child in list(div.children):
                if isinstance(child, NavigableString) and not child.strip():
                    continue
                body.append(child.extract() if isinstance(child, Tag) else NavigableString(str(child)))
            macro.append(body)
            div.replace_with(macro)


def _replace_mermaid_with_attachments(
    soup: BeautifulSoup, mermaid_divs: Iterable[Tag], filenames: list[str],
    *,
    display_width: int = 700,
) -> None:
    """`<div class="mermaid">` 들을 ac:image (첨부 참조) 로 교체.

    filenames 는 mermaid_divs 와 1:1 매칭 순서.
    display_width 는 Confluence 본문 표시 폭 (PNG native 보다 작아야 자연스러움).
    """
    for div, fn in zip(mermaid_divs, filenames):
        ac_image = soup.new_tag(
            "ac:image",
            attrs={
                "ac:width": str(display_width),
                "ac:align": "center",
                "ac:layout": "center",
            },
        )
        ri_att = soup.new_tag("ri:attachment", attrs={"ri:filename": fn})
        ac_image.append(ri_att)
        # figure 안에 있는 경우 figure 까지 함께 교체 (caption 포함)
        ancestor = div.find_parent("figure")
        if ancestor is not None:
            caption_text = ""
            cap = ancestor.find("figcaption")
            if cap:
                caption_text = cap.get_text(" ", strip=True)
            wrapper = soup.new_tag("p")
            wrapper.append(ac_image)
            ancestor.replace_with(wrapper)
            if caption_text:
                em = soup.new_tag("em")
                em.string = caption_text
                cap_p = soup.new_tag("p")
                cap_p.append(em)
                wrapper.insert_after(cap_p)
        else:
            div.replace_with(ac_image)


def html_to_storage(
    html_path: Path,
    *,
    attachments_dir: Path,
    mermaid_prefix: str = "mermaid",
    mmdc_bin: Path | None = None,
) -> tuple[str, list[Path]]:
    """HTML 파일 → (storage_html, attached_png_paths).

    attachments_dir 에 mermaid_<n>.png 들을 생성한다 (호출자가 이 경로를 page 에 첨부).
    """
    raw = Path(html_path).read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "html.parser")

    # 1. body 만 추출 (body 가 없으면 root 전체)
    body = soup.body if soup.body else soup
    # Storage 는 body 자체 root 안 — body 의 children 을 새 soup 으로 복사
    new_soup = BeautifulSoup("", "html.parser")
    container = new_soup.new_tag("div")
    for child in list(body.children):
        container.append(child.extract() if isinstance(child, Tag) else NavigableString(str(child)))
    new_soup.append(container)

    # 2. 미지원 element / 속성 제거
    _strip_unsafe(new_soup)

    # 3. mermaid 블록 추출 + PNG 렌더
    mermaid_divs = extract_mermaid_blocks(new_soup)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    attached_pngs: list[Path] = []
    filenames: list[str] = []
    for idx, div in enumerate(mermaid_divs, start=1):
        code = div.get_text("\n", strip=False)
        fn = f"{mermaid_prefix}_{idx}.png"
        png_path = attachments_dir / fn
        try:
            render_mermaid_png(code, png_path, mmdc_bin=mmdc_bin)
        except Exception as exc:
            # 실패 시 mermaid 블록을 plain code block 으로 fallback (페이지 작성은 계속)
            print(f"⚠️ mermaid 블록 {idx} 렌더 실패 — code block 으로 대체: {exc}", file=sys.stderr)
            pre = new_soup.new_tag("pre")
            code_tag = new_soup.new_tag("code")
            code_tag.string = code
            pre.append(code_tag)
            div.replace_with(pre)
            filenames.append("")  # placeholder
            continue
        attached_pngs.append(png_path)
        filenames.append(fn)

    # 실패한 블록은 이미 pre 로 교체됨. 성공한 것만 ac:image 로.
    success_pairs = [
        (d, f) for d, f in zip(mermaid_divs, filenames)
        if f and d.parent is not None  # 아직 soup 안에 있음
    ]
    _replace_mermaid_with_attachments(
        new_soup,
        [d for d, _ in success_pairs],
        [f for _, f in success_pairs],
    )

    # 4. 커스텀 박스 → Confluence macro
    _convert_boxes_to_macros(new_soup)

    # 5. 최종 storage HTML (container div 의 inner)
    storage_html = container.decode_contents()
    return storage_html, attached_pngs


# ---------- CLI (단독 dry-run) ----------

def _main_cli() -> None:
    if len(sys.argv) < 2:
        print(
            "사용법: python html_push.py <html_path> [--out-dir <dir>]\n"
            "  → storage HTML 을 stdout, mermaid PNG 들을 out-dir (기본: ./mermaid_out) 에 생성"
        )
        sys.exit(0)
    html_path = Path(sys.argv[1])
    out_dir = Path("./mermaid_out")
    if "--out-dir" in sys.argv:
        out_dir = Path(sys.argv[sys.argv.index("--out-dir") + 1])
    storage, pngs = html_to_storage(html_path, attachments_dir=out_dir)
    print(f"# storage HTML ({len(storage)} bytes) — PNG {len(pngs)} 개 생성: {out_dir}", file=sys.stderr)
    for p in pngs:
        print(f"  - {p.name}", file=sys.stderr)
    sys.stdout.write(storage)


if __name__ == "__main__":
    _main_cli()
