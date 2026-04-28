#!/usr/bin/env python3
"""
Confluence storage-format JSON → 브라우저로 보는 HTML + 변환 MD 빌드.

출력 구조:
  confluence_content/최종2팀/html/<페이지경로>/index.html
  confluence_content/최종2팀/html/<페이지경로>/attachments/<filename>
  confluence_content/최종2팀/md/<페이지경로>/index.md
  confluence_content/최종2팀/md/<페이지경로>/attachments/<filename>

첨부 원본은 confluence_sync/raw_attachments/<page_id>/<filename>에서 찾아 복사합니다.
누락된 첨부는 confluence_sync/.missing_attachments.json에 기록됩니다.
"""

import datetime
import html as html_lib
import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

from drawio_utils import (
    find_drawio_macros,
    drawio_to_mermaid,
    find_drawio_source,
    find_drawio_preview,
)

SCRIPT_DIR = Path(__file__).parent.resolve()
WS_ROOT = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
RAW_HTML_DIR = SCRIPT_DIR / "raw_html"
RAW_ATTACH_DIR = SCRIPT_DIR / "raw_attachments"
CONTENT_ROOT = WS_ROOT / "confluence_content"
MISSING_LOG = SCRIPT_DIR / ".missing_attachments.json"

PAGE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
       max-width: 980px; margin: 2em auto; padding: 0 2em; line-height: 1.6; color: #172b4d; }
h1, h2, h3, h4, h5, h6 { color: #172b4d; margin-top: 1.5em; line-height: 1.3; }
h1 { border-bottom: 2px solid #dfe1e6; padding-bottom: 0.3em; }
img { max-width: 100%; height: auto; display: inline-block; margin: 8px 0; }
video { max-width: 100%; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #dfe1e6; padding: 8px 12px; text-align: left; vertical-align: top; }
th { background: #f4f5f7; font-weight: 600; }
pre { background: #f4f5f7; padding: 12px; border-radius: 4px; overflow-x: auto; }
code { background: #f4f5f7; padding: 2px 4px; border-radius: 3px;
       font-family: 'SFMono-Regular', Consolas, monospace; font-size: 0.9em; }
pre code { background: transparent; padding: 0; font-size: 0.875em; }
.info, .note, .warning, .tip, .panel { padding: 12px 16px; margin: 1em 0;
                                       border-radius: 4px; border-left: 4px solid; }
.info { background: #deebff; border-color: #4c9aff; }
.note { background: #f4f5f7; border-color: #6b778c; }
.warning { background: #fffae6; border-color: #ffab00; }
.tip { background: #e3fcef; border-color: #36b37e; }
.panel { background: #f4f5f7; border-color: #6b778c; }
a { color: #0052cc; }
ul, ol { padding-left: 1.5em; }
.attachment-missing { background: #ffebe6; border: 1px dashed #ff5630;
                      padding: 8px 12px; border-radius: 4px; color: #bf2600;
                      display: inline-block; margin: 4px 0; font-size: 0.9em; }
.children-list { background: #f4f5f7; padding: 12px 20px; border-radius: 4px; margin: 1em 0; }
nav.crumbs { font-size: 0.9em; color: #6b778c; margin-bottom: 1em; }
nav.crumbs a { color: #6b778c; text-decoration: none; }
nav.crumbs a:hover { text-decoration: underline; }
figure.drawio { border: 1px solid #dfe1e6; border-radius: 6px;
                margin: 1.5em 0; padding: 12px; background: #fafbfc; }
figure.drawio img { max-width: 100%; display: block; margin: 0 auto 8px; }
figure.drawio figcaption { color: #6b778c; font-size: 0.9em;
                           display: flex; flex-wrap: wrap; gap: 12px;
                           align-items: center; justify-content: space-between; }
figure.drawio figcaption .name { font-weight: 600; color: #172b4d; }
figure.drawio figcaption a { color: #0052cc; text-decoration: none; }
figure.drawio figcaption a:hover { text-decoration: underline; }
figure.drawio details { margin-top: 12px; }
figure.drawio details summary { cursor: pointer; color: #0052cc; font-size: 0.9em; }
figure.drawio .mermaid { background: #fff; border: 1px dashed #c1c7d0;
                         border-radius: 4px; padding: 10px; margin-top: 8px; }
"""

MERMAID_CDN_TAG = (
    '<script type="module">\n'
    '  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";\n'
    '  mermaid.initialize({ startOnLoad: true, securityLevel: "loose" });\n'
    "</script>"
)


# ─── 1) Confluence storage HTML → 일반 HTML ───────────────────────────

def transform_storage_html(storage_html: str, attachment_map: dict, missing: set) -> str:
    """ac:* / ri:* 태그를 일반 HTML로 치환."""
    html = storage_html

    # ac:image (paired) → <img>
    def repl_image(m):
        attrs = m.group(1)
        inner = m.group(2)
        fn_m = re.search(r'ri:filename="([^"]+)"', inner)
        ri_url_m = re.search(r'<ri:url\s+ri:value="([^"]+)"', inner)

        alt_m = re.search(r'ac:alt="([^"]+)"', attrs)
        alt = html_lib.escape(alt_m.group(1)) if alt_m else ""

        w_m = re.search(r'ac:width="([^"]+)"', attrs)
        width_attr = f' width="{w_m.group(1)}"' if w_m else ""

        align_m = re.search(r'ac:align="([^"]+)"', attrs)
        align = align_m.group(1) if align_m else None

        if fn_m:
            filename = fn_m.group(1)
            if filename in attachment_map:
                src = attachment_map[filename]
                tag = f'<img src="{src}" alt="{alt}"{width_attr} />'
            else:
                missing.add(filename)
                tag = (f'<span class="attachment-missing">📎 {html_lib.escape(filename)} '
                       f'(첨부 다운로드 필요)</span>')
        elif ri_url_m:
            tag = f'<img src="{ri_url_m.group(1)}" alt="{alt}"{width_attr} />'
        else:
            return ""

        if align in ("center", "right", "left"):
            return f'<div style="text-align:{align}">{tag}</div>'
        return tag

    html = re.sub(r"<ac:image([^>]*?)(?<!/)>(.*?)</ac:image>",
                  repl_image, html, flags=re.DOTALL)
    html = re.sub(r"<ac:image[^>]*/>", "", html)

    # ac:link with ri:attachment / ri:page → <a>
    def repl_link(m):
        inner = m.group(1)
        fn_m = re.search(r'ri:filename="([^"]+)"', inner)
        page_m = re.search(r'<ri:page[^>]*ri:content-title="([^"]+)"', inner)
        body_m = re.search(
            r"<ac:plain-text-link-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-link-body>",
            inner, re.DOTALL,
        )
        rich_m = re.search(r"<ac:link-body>(.*?)</ac:link-body>", inner, re.DOTALL)
        text = body_m.group(1) if body_m else (rich_m.group(1) if rich_m else "")

        if fn_m:
            filename = fn_m.group(1)
            if filename in attachment_map:
                href = attachment_map[filename]
            else:
                missing.add(filename)
                href = f"attachments/{filename}"
            return f'<a href="{href}">{html_lib.escape(text or filename)}</a>'
        if page_m:
            title = page_m.group(1)
            return f'<a href="#{html_lib.escape(title)}">{html_lib.escape(text or title)}</a>'
        return text

    html = re.sub(r"<ac:link[^>]*>(.*?)</ac:link>",
                  repl_link, html, flags=re.DOTALL)

    # ac:structured-macro: code → <pre><code>
    def repl_code(m):
        block = m.group(0)
        lang_m = re.search(r'<ac:parameter ac:name="language">([^<]*)</ac:parameter>', block)
        body_m = re.search(
            r"<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>",
            block, re.DOTALL,
        )
        lang = lang_m.group(1) if lang_m else ""
        body = body_m.group(1) if body_m else ""
        body_esc = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{body_esc}</code></pre>"

    html = re.sub(
        r'<ac:structured-macro\s+ac:name="code"[^>]*>.*?</ac:structured-macro>',
        repl_code, html, flags=re.DOTALL,
    )

    # info / note / warning / tip / panel → <div class="...">
    for name in ("info", "note", "warning", "tip", "panel"):
        def make_repl(n):
            def repl(m):
                rich = re.search(r"<ac:rich-text-body>(.*?)</ac:rich-text-body>",
                                 m.group(0), re.DOTALL)
                body = rich.group(1) if rich else ""
                return f'<div class="{n}">{body}</div>'
            return repl
        html = re.sub(
            rf'<ac:structured-macro\s+ac:name="{name}"[^>]*>.*?</ac:structured-macro>',
            make_repl(name), html, flags=re.DOTALL,
        )

    # 그 외 매크로는 rich/plain body 만 살리고 껍데기 제거
    def repl_macro(m):
        block = m.group(0)
        rich = re.search(r"<ac:rich-text-body>(.*?)</ac:rich-text-body>",
                         block, re.DOTALL)
        if rich:
            return rich.group(1)
        plain = re.search(r"<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>",
                          block, re.DOTALL)
        if plain:
            esc = plain.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f"<pre>{esc}</pre>"
        return ""

    html = re.sub(
        r"<ac:structured-macro[^>]*>.*?</ac:structured-macro>",
        repl_macro, html, flags=re.DOTALL,
    )
    html = re.sub(r"<ac:structured-macro[^>]*/>", "", html)

    # 인라인 ac:* / ri:* 잔여 제거
    html = re.sub(r"<ac:[a-z-]+[^>]*/>", "", html)
    html = re.sub(r"<ac:[a-z-]+[^>]*>(.*?)</ac:[a-z-]+>", r"\1", html, flags=re.DOTALL)
    html = re.sub(r"<ri:[^>]*/>", "", html)
    html = re.sub(r"</?ri:[^>]+>", "", html)

    # Confluence 전용 속성 제거
    html = re.sub(
        r'\s+(?:local-id|ac:[\w-]+|ri:[\w-]+|data-(?:layout|highlight-colour|card-appearance))="[^"]*"',
        "", html,
    )

    # 빈 self-closing <p /> 같은 것들 정리
    html = re.sub(r"<p\s*/>", "", html)
    html = re.sub(r"<p>\s*</p>", "", html)

    return html.strip()


# ─── 2) 일반 HTML → Markdown ─────────────────────────────────────────

class HTMLToMarkdown(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []
        self.list_stack = []
        self.list_counters = []
        self.heading_level = 0
        self.heading_text = ""
        self.in_link = False
        self.link_href = ""
        self.link_text = ""
        self.in_pre = False
        self.pre_text = ""
        self.in_pre_code_lang = ""
        self.in_table = False
        self.table_rows = []
        self.current_row = []
        self.current_cell = ""
        self.in_cell = False

    def _emit(self, s):
        if self.in_pre:
            self.pre_text += s
        elif self.heading_level > 0:
            self.heading_text += s
        elif self.in_link:
            self.link_text += s
        elif self.in_cell:
            self.current_cell += s
        else:
            self.out.append(s)

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading_level = int(tag[1]); self.heading_text = ""
        elif tag in ("strong", "b"): self._emit("**")
        elif tag in ("em", "i"): self._emit("*")
        elif tag == "code" and not self.in_pre: self._emit("`")
        elif tag == "pre":
            self.in_pre = True; self.pre_text = ""; self.in_pre_code_lang = ""
        elif tag == "a":
            self.in_link = True
            self.link_href = ad.get("href", ""); self.link_text = ""
        elif tag == "ul":
            self.list_stack.append("ul"); self.list_counters.append(0)
        elif tag == "ol":
            self.list_stack.append("ol"); self.list_counters.append(0)
        elif tag == "li":
            indent = "  " * max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.list_counters[-1] += 1
                self.out.append(f"\n{indent}{self.list_counters[-1]}. ")
            else:
                self.out.append(f"\n{indent}- ")
        elif tag == "br": self._emit("  \n")
        elif tag == "table":
            self.in_table = True; self.table_rows = []
        elif tag in ("td", "th"):
            self.in_cell = True; self.current_cell = ""
        elif tag == "tr": self.current_row = []
        elif tag == "img":
            src = ad.get("src", ""); alt = ad.get("alt", "")
            self._emit(f"![{alt}]({src})")
        elif tag == "video":
            src = ad.get("src", "")
            self._emit(f"\n\n[📹 video: {src}]({src})\n\n")
        elif tag == "div":
            cls = ad.get("class", "")
            if cls in ("info", "note", "warning", "tip", "panel"):
                self.out.append(f"\n\n> **[{cls.upper()}]** ")
        elif tag == "p":
            pass

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append(f'\n\n{"#" * self.heading_level} {self.heading_text.strip()}\n')
            self.heading_level = 0
        elif tag == "p": self.out.append("\n\n")
        elif tag in ("strong", "b"): self._emit("**")
        elif tag in ("em", "i"): self._emit("*")
        elif tag == "code" and not self.in_pre: self._emit("`")
        elif tag == "pre":
            lang = self.in_pre_code_lang or ""
            self.out.append(f"\n```{lang}\n{self.pre_text}\n```\n\n")
            self.in_pre = False; self.pre_text = ""; self.in_pre_code_lang = ""
        elif tag == "a":
            self.in_link = False
            if self.link_href:
                self._emit(f"[{self.link_text}]({self.link_href})")
            else:
                self._emit(self.link_text)
        elif tag in ("ul", "ol"):
            if self.list_stack: self.list_stack.pop()
            if self.list_counters: self.list_counters.pop()
            if not self.list_stack: self.out.append("\n")
        elif tag in ("td", "th"):
            self.in_cell = False
            self.current_row.append(self.current_cell.strip().replace("\n", " "))
        elif tag == "tr": self.table_rows.append(self.current_row)
        elif tag == "table":
            self.in_table = False; self._render_table()
        elif tag == "div":
            self.out.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        # <img />, <br />
        ad = dict(attrs)
        if tag == "img":
            src = ad.get("src", ""); alt = ad.get("alt", "")
            self._emit(f"![{alt}]({src})")
        elif tag == "br":
            self._emit("  \n")

    def handle_data(self, data):
        clean = data.replace("‍", "").replace("​", "")
        self._emit(clean)

    def handle_entityref(self, name):
        ents = {"amp": "&", "lt": "<", "gt": ">", "quot": '"',
                "apos": "'", "nbsp": " ", "zwj": ""}
        self._emit(ents.get(name, f"&{name};"))

    def handle_charref(self, name):
        try:
            n = int(name[1:], 16) if name.startswith(("x", "X")) else int(name)
            self._emit(chr(n))
        except Exception:
            self._emit(f"&#{name};")

    def _render_table(self):
        if not self.table_rows: return
        max_cols = max(len(r) for r in self.table_rows)
        self.out.append("\n\n")
        for i, row in enumerate(self.table_rows):
            while len(row) < max_cols: row.append("")
            self.out.append("| " + " | ".join(row) + " |\n")
            if i == 0:
                self.out.append("| " + " | ".join(["---"] * max_cols) + " |\n")
        self.out.append("\n")

    def get_md(self):
        text = "".join(self.out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_md(plain_html: str) -> str:
    parser = HTMLToMarkdown()
    parser.feed(plain_html)
    return parser.get_md()


# ─── 3) HTML 페이지 렌더링 ──────────────────────────────────────────

def render_full_html(title: str, body: str, page_id: str,
                     crumbs_html: str, children_html: str = "",
                     include_mermaid: bool = False) -> str:
    title_e = html_lib.escape(title)
    mermaid_tag = MERMAID_CDN_TAG if include_mermaid else ""
    return (
        '<!DOCTYPE html>\n'
        '<html lang="ko">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>{title_e}</title>\n'
        f'<style>{PAGE_CSS}</style>\n'
        '</head>\n'
        '<body>\n'
        f'<nav class="crumbs">{crumbs_html}</nav>\n'
        f'<h1>{title_e}</h1>\n'
        f'{body}\n'
        f'{children_html}\n'
        '<hr style="margin-top:3em;border:0;border-top:1px solid #dfe1e6">\n'
        '<footer style="color:#6b778c;font-size:0.85em">\n'
        f'  Confluence page id: {page_id} · Generated: '
        f'{datetime.datetime.now().isoformat(timespec="seconds")}\n'
        '</footer>\n'
        f'{mermaid_tag}\n'
        '</body>\n'
        '</html>\n'
    )


# ─── 4) 첨부 검색 ───────────────────────────────────────────────────

def find_attachments(page_id: str) -> dict:
    page_dir = RAW_ATTACH_DIR / page_id
    if not page_dir.exists():
        return {}
    return {f.name: f for f in page_dir.iterdir() if f.is_file()}


# ─── 4.5) drawio / mermaid 처리 ─────────────────────────────────────

_DRAWIO_PLACEHOLDER = "<!--DRAWIO_PLACEHOLDER_{n}-->"


def _safe_filename(s: str) -> str:
    """파일시스템 안전 + .drawio 확장자 부착."""
    base = s
    if base.endswith(".drawio"):
        base = base[: -len(".drawio")]
    base = re.sub(r"[\\/:*?\"<>|]+", "_", base).strip()
    return base or "diagram"


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if (not dst.exists()
            or dst.stat().st_size != src.stat().st_size
            or dst.stat().st_mtime < src.stat().st_mtime):
        shutil.copy2(src, dst)
        return True
    return False


def process_drawio_macros(
    storage_html: str,
    page_id: str,
    page_html_dir: Path,
    page_md_dir: Path,
) -> tuple[str, list[dict]]:
    """
    storage_html 안에서 drawio 매크로를 검출하고
      - drawio/<base>.drawio  (mxfile XML 원본)
      - drawio/<base>.png     (프리뷰)
      - mermaid/<base>.mmd    (자동 생성, 이미 있으면 보존)
    를 페이지 폴더에 작성한다.

    매크로 raw_block 은 placeholder 로 치환한 storage_html 을 반환.
    diagrams 메타데이터 리스트도 함께 반환 — 빌더가 placeholder 를 figure 로
    교체할 때 사용한다.
    """
    macros = find_drawio_macros(storage_html)
    if not macros:
        return storage_html, []

    raw_attach_dir = RAW_ATTACH_DIR / page_id
    diagrams: list[dict] = []

    for idx, mac in enumerate(macros):
        base = _safe_filename(mac.diagram_name)
        src = find_drawio_source(raw_attach_dir, mac.diagram_name)
        preview = find_drawio_preview(raw_attach_dir, mac.diagram_name)

        # drawio/ 와 mermaid/ 디렉토리는 html/md 양쪽에 동일하게 복제
        for out_dir in (page_html_dir, page_md_dir):
            (out_dir / "drawio").mkdir(parents=True, exist_ok=True)
            (out_dir / "mermaid").mkdir(parents=True, exist_ok=True)
            if src:
                _copy_if_changed(src, out_dir / "drawio" / f"{base}.drawio")
            if preview:
                _copy_if_changed(preview, out_dir / "drawio" / f"{base}.png")

        # mermaid 파일 — 사용자 수정본 보존
        mmd_target = page_md_dir / "mermaid" / f"{base}.mmd"
        mmd_html_target = page_html_dir / "mermaid" / f"{base}.mmd"
        if not mmd_target.exists():
            xml = ""
            if src:
                try:
                    xml = src.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    xml = ""
            mmd_text = drawio_to_mermaid(xml, mac.diagram_name) if xml else (
                f"%% Source drawio not downloaded yet — placeholder.\n"
                f"flowchart TD\n"
                f"    todo[\"{mac.diagram_name}\"]\n"
            )
            mmd_target.write_text(mmd_text, encoding="utf-8")
        # md 쪽 mmd 가 진실의 원천 — html 쪽은 동기화 사본
        shutil.copy2(mmd_target, mmd_html_target)

        # storage_html 안의 매크로 블록을 placeholder 로 치환 (1회만)
        ph = _DRAWIO_PLACEHOLDER.format(n=idx)
        storage_html = storage_html.replace(mac.raw_block, ph, 1)

        diagrams.append({
            "idx": idx,
            "name": mac.diagram_name,
            "base": base,
            "has_source": src is not None,
            "has_preview": preview is not None,
            "mermaid_text": (page_md_dir / "mermaid" / f"{base}.mmd").read_text(encoding="utf-8"),
        })

    return storage_html, diagrams


def render_drawio_html(diagrams: list[dict]) -> dict[int, str]:
    """각 placeholder idx → HTML 문자열."""
    out: dict[int, str] = {}
    for d in diagrams:
        base = d["base"]
        name_e = html_lib.escape(d["name"])
        img_html = (
            f'<img src="drawio/{html_lib.escape(base)}.png" alt="{name_e}" />'
            if d["has_preview"] else
            f'<div class="attachment-missing">📎 {name_e} (프리뷰 없음)</div>'
        )
        drawio_link = (
            f'<a href="drawio/{html_lib.escape(base)}.drawio" download>📐 {html_lib.escape(base)}.drawio</a>'
            if d["has_source"] else ""
        )
        # diagrams.net 으로 열기 — 로컬 파일 URL 은 브라우저 보안 정책상 동작이 제한적이므로
        # 다운로드 후 직접 import 하는 안내를 표시.
        viewer_link = (
            f'<a href="https://app.diagrams.net/?ui=atlas&splash=0" target="_blank" '
            f'rel="noopener">🌐 diagrams.net 열기</a>'
        )
        mmd_link = (
            f'<a href="mermaid/{html_lib.escape(base)}.mmd">✏️ mermaid 편집</a>'
        )
        # 인라인 mermaid 코드 — 클라이언트에서 mermaid.js 로 렌더링
        mmd_text = d["mermaid_text"]
        mmd_text_e = html_lib.escape(mmd_text)
        figure = (
            '<figure class="drawio">\n'
            f'  {img_html}\n'
            f'  <figcaption>\n'
            f'    <span class="name">📐 {name_e}</span>\n'
            f'    <span>{drawio_link} · {viewer_link} · {mmd_link}</span>\n'
            f'  </figcaption>\n'
            f'  <details>\n'
            f'    <summary>Mermaid 코드 (자동 생성, lossy)</summary>\n'
            f'    <div class="mermaid">{mmd_text_e}</div>\n'
            f'  </details>\n'
            '</figure>'
        )
        out[d["idx"]] = figure
    return out


def render_drawio_md(diagrams: list[dict]) -> dict[int, str]:
    """각 placeholder idx → MD 문자열."""
    out: dict[int, str] = {}
    for d in diagrams:
        base = d["base"]
        name = d["name"]
        img = f"![{name}](drawio/{base}.png)" if d["has_preview"] else \
              f"_(프리뷰 없음: {name})_"
        drawio_ref = (
            f"[📐 {base}.drawio](drawio/{base}.drawio)"
            if d["has_source"] else "(원본 다운로드 필요)"
        )
        mmd_ref = f"[✏️ {base}.mmd](mermaid/{base}.mmd)"
        # mermaid 인라인 — 코드블록은 Github/VSCode 에서 자동 렌더
        mmd_block = "```mermaid\n" + d["mermaid_text"].rstrip() + "\n```"
        block = (
            f"\n\n{img}\n\n"
            f"📐 **{name}** — {drawio_ref} · {mmd_ref}\n\n"
            f"{mmd_block}\n\n"
        )
        out[d["idx"]] = block
    return out


# ─── 5) 메인 빌드 ───────────────────────────────────────────────────

def build_breadcrumbs(pages: dict, info: dict, space_root: str) -> str:
    parents = []
    cur = info.get("parent_id")
    while cur and cur in pages:
        p_info = pages[cur]
        if p_info.get("parent_id") is None:
            break  # space root는 아래에서 한 번만 표시
        parents.insert(0, p_info["title"])
        cur = p_info.get("parent_id")
    parts = [space_root] + parents
    return " / ".join(html_lib.escape(p) for p in parts)


def main():
    print("\n🔄 HTML & MD 빌드 시작...\n")

    if not CONFIG_FILE.exists():
        print(f"❌ {CONFIG_FILE} 없음")
        return
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    pages = {pid: info for pid, info in config.get("page_tree", {}).items()
             if not pid.endswith("_children")}
    space_root = config.get("space_name", "최종2팀")

    # parent_id → children
    children_by_parent: dict[str, list] = {}
    for pid, info in pages.items():
        parent = info.get("parent_id")
        if parent:
            children_by_parent.setdefault(parent, []).append((pid, info))

    html_root = CONTENT_ROOT / space_root / "html"
    md_root = CONTENT_ROOT / space_root / "md"
    html_root.mkdir(parents=True, exist_ok=True)
    md_root.mkdir(parents=True, exist_ok=True)

    missing_log: dict[str, dict] = {}

    for pid, info in pages.items():
        local_path = info["local_path"]
        title = info["title"]
        is_folder = info.get("is_folder", False)

        rel = local_path
        if rel.startswith(space_root + "/"):
            rel = rel[len(space_root) + 1 :]
        elif rel == space_root:
            rel = ""

        page_html_dir = (html_root / rel) if rel else html_root
        page_md_dir = (md_root / rel) if rel else md_root
        page_html_dir.mkdir(parents=True, exist_ok=True)
        page_md_dir.mkdir(parents=True, exist_ok=True)

        # raw storage HTML 로드
        raw_file = RAW_HTML_DIR / f"{pid}.json"
        storage_html = ""
        version = 1
        if raw_file.exists():
            try:
                data = json.loads(raw_file.read_text(encoding="utf-8"))
                storage_html = data.get("html", "") or ""
                version = data.get("ver", 1)
            except Exception as e:
                print(f"  ⚠️ {pid} 로드 실패: {e}")

        # 첨부 복사
        atts = find_attachments(pid)
        attach_map: dict[str, str] = {}
        if atts:
            (page_html_dir / "attachments").mkdir(exist_ok=True)
            (page_md_dir / "attachments").mkdir(exist_ok=True)
            for name, src in atts.items():
                for dst_dir in (page_html_dir / "attachments",
                                page_md_dir / "attachments"):
                    dst = dst_dir / name
                    if (not dst.exists()
                        or dst.stat().st_size != src.stat().st_size
                        or dst.stat().st_mtime < src.stat().st_mtime):
                        shutil.copy2(src, dst)
                attach_map[name] = f"attachments/{name}"

        # drawio 매크로 검출 → drawio/mermaid 파일 생성, storage_html 의 매크로는 placeholder 로 치환
        storage_html, diagrams = process_drawio_macros(
            storage_html, pid, page_html_dir, page_md_dir,
        )
        drawio_html_map = render_drawio_html(diagrams)
        drawio_md_map = render_drawio_md(diagrams)

        # storage → 일반 HTML (placeholder 는 그대로 살아있음)
        missing: set = set()
        plain_html_raw = transform_storage_html(storage_html, attach_map, missing)

        # HTML 출력용: placeholder 를 figure 로 치환
        plain_html = plain_html_raw
        for idx, fig in drawio_html_map.items():
            ph = _DRAWIO_PLACEHOLDER.format(n=idx)
            plain_html = plain_html.replace(ph, fig)

        # MD 변환용: html_to_md 의 HTMLParser 는 코멘트를 무시하므로
        # placeholder 를 일반 텍스트 토큰으로 바꿔 두었다가 변환 후 치환.
        plain_html_for_md = plain_html_raw
        md_tokens: dict[int, str] = {}
        for idx in drawio_md_map:
            ph = _DRAWIO_PLACEHOLDER.format(n=idx)
            tok = f"☄DRAWIO_TOKEN_{idx}☄"
            md_tokens[idx] = tok
            plain_html_for_md = plain_html_for_md.replace(
                ph, f"<p>{tok}</p>"
            )

        # 빵 부스러기
        crumbs_html = build_breadcrumbs(pages, info, space_root)

        # 하위 페이지 목록 (폴더용) — html/md 별도 생성 (index.html vs index.md)
        children_html_for_html = ""
        children_md = ""
        if is_folder and pid in children_by_parent:
            children = sorted(children_by_parent[pid], key=lambda x: x[1]["title"])
            html_items, md_items = [], []
            for cid, cinfo in children:
                cpath = cinfo["local_path"]
                if cpath.startswith(space_root + "/"):
                    cpath = cpath[len(space_root) + 1 :]
                if rel and cpath.startswith(rel + "/"):
                    link = cpath[len(rel) + 1 :]
                else:
                    link = cpath
                icon = "📁" if cinfo.get("is_folder") else "📄"
                ctitle_e = html_lib.escape(cinfo["title"])
                html_items.append(
                    f'<li>{icon} <a href="{link}/index.html">{ctitle_e}</a></li>'
                )
                md_items.append(
                    f"- {icon} [{cinfo['title']}]({link}/index.md)"
                )
            if html_items:
                children_html_for_html = (
                    '<div class="children-list">'
                    "<strong>📂 하위 페이지</strong>"
                    f'<ul>{"".join(html_items)}</ul></div>'
                )
                children_md = "\n\n## 📂 하위 페이지\n\n" + "\n".join(md_items) + "\n"

        # HTML 출력 (페이지에 drawio 매크로가 있으면 mermaid.js 포함)
        full_html = render_full_html(
            title, plain_html, pid, crumbs_html,
            children_html_for_html,
            include_mermaid=bool(diagrams),
        )
        (page_html_dir / "index.html").write_text(full_html, encoding="utf-8")

        # MD 출력 — drawio token 들을 mermaid block 으로 치환
        md_body = html_to_md(plain_html_for_md)
        for idx, block in drawio_md_map.items():
            md_body = md_body.replace(md_tokens[idx], block)
        md_body += children_md
        md_full = (
            "---\n"
            f'confluence_page_id: "{pid}"\n'
            f'title: "{title}"\n'
            f"confluence_version: {version}\n"
            f'last_synced: "{datetime.datetime.now().isoformat()}"\n'
            f"is_folder: {str(is_folder).lower()}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"{md_body}\n"
        )
        (page_md_dir / "index.md").write_text(md_full, encoding="utf-8")

        if missing:
            missing_log[pid] = {"title": title, "files": sorted(missing)}

        icon = "📁" if is_folder else "📄"
        miss_note = f"  ⚠️ 첨부 누락 {len(missing)}건" if missing else ""
        print(f"  {icon} {rel or '(root)'}{miss_note}")

    # 최상위 인덱스
    top_pages = sorted(
        [(pid, info) for pid, info in pages.items()
         if info.get("parent_id") == "40829168"],
        key=lambda x: (not x[1].get("is_folder"), x[1]["title"]),
    )
    items = []
    for cid, cinfo in top_pages:
        cpath = cinfo["local_path"]
        if cpath.startswith(space_root + "/"):
            cpath = cpath[len(space_root) + 1 :]
        icon = "📁" if cinfo.get("is_folder") else "📄"
        items.append(
            f'<li>{icon} <a href="{cpath}/index.html">'
            f"{html_lib.escape(cinfo['title'])}</a></li>"
        )
    top_html = (
        '<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
        '<meta charset="UTF-8">\n'
        f"<title>{html_lib.escape(space_root)}</title>\n"
        f"<style>{PAGE_CSS}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{html_lib.escape(space_root)}</h1>\n"
        f'<div class="children-list"><ul>{"".join(items)}</ul></div>\n'
        "</body>\n</html>\n"
    )
    (html_root / "index.html").write_text(top_html, encoding="utf-8")

    md_top = "# " + space_root + "\n\n" + "\n".join(
        f"- [{cinfo['title']}]({cinfo['local_path'].split(space_root + '/', 1)[-1]}/index.md)"
        for _, cinfo in top_pages
    ) + "\n"
    (md_root / "index.md").write_text(md_top, encoding="utf-8")

    # 누락 로그
    if missing_log:
        MISSING_LOG.write_text(
            json.dumps(missing_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        total = sum(len(v["files"]) for v in missing_log.values())
        print(f"\n⚠️ 누락 첨부 총 {total}개 → {MISSING_LOG.relative_to(WS_ROOT)}")
    else:
        if MISSING_LOG.exists():
            MISSING_LOG.unlink()
        print("\n✅ 모든 첨부 OK")

    print(f"\n📂 HTML 진입점: {html_root.relative_to(WS_ROOT)}/index.html")
    print(f"📂 MD  진입점: {md_root.relative_to(WS_ROOT)}/index.md\n")


if __name__ == "__main__":
    main()
