#!/usr/bin/env python3
"""
Confluence ↔ Local 양방향 싱크 스크립트
======================================

기능:
  1. pull  : Confluence → 로컬로 전체 페이지 구조/콘텐츠를 다운로드 (마크다운 변환)
  2. push  : 로컬 마크다운 파일 → Confluence 페이지로 업로드 (특정 페이지)
  3. status: 로컬 vs Confluence 간 변경 사항 확인

사용법:
  python confluence_sync.py pull              # Confluence → 로컬 전체 다운로드
  python confluence_sync.py push <page_id>    # 특정 페이지 로컬 → Confluence 업로드
  python confluence_sync.py push --all        # 변경된 모든 페이지 업로드
  python confluence_sync.py status            # 변경 사항 확인
"""

import json
import os
import re
import sys
import hashlib
import datetime
from html.parser import HTMLParser
from pathlib import Path

# ─── 설정 ───────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "sync_config.json"
CONTENT_ROOT = SCRIPT_DIR.parent / "confluence_content"
HASH_STORE_PATH = SCRIPT_DIR / ".sync_hashes.json"
META_DIR = SCRIPT_DIR / ".page_meta"


# ─── HTML → Markdown 변환기 ─────────────────────────────────────────
class ConfluenceHTMLToMarkdown(HTMLParser):
    """Confluence storage format HTML을 Markdown으로 변환하는 파서"""
    
    def __init__(self):
        super().__init__()
        self.output = []
        self.list_stack = []      # 'ul' or 'ol' 스택
        self.list_counters = []   # ol 카운터
        self.in_code = False
        self.in_strong = False
        self.in_em = False
        self.in_link = False
        self.link_href = ""
        self.link_text = ""
        self.current_heading = ""
        self.heading_level = 0
        self.in_table = False
        self.table_rows = []
        self.current_row = []
        self.current_cell = ""
        self.in_cell = False
        self.skip_macro = 0
        
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        
        # Confluence 매크로는 무시
        if tag.startswith('ac:'):
            if tag == 'ac:structured-macro':
                self.skip_macro += 1
            return
            
        if self.skip_macro > 0:
            return
            
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.heading_level = int(tag[1])
            self.current_heading = ""
        elif tag == 'p':
            pass
        elif tag == 'strong' or tag == 'b':
            self.in_strong = True
            self.output.append('**')
        elif tag == 'em' or tag == 'i':
            self.in_em = True
            self.output.append('*')
        elif tag == 'code':
            self.in_code = True
            self.output.append('`')
        elif tag == 'a':
            self.in_link = True
            self.link_href = attrs_dict.get('href', '')
            self.link_text = ""
        elif tag == 'ul':
            self.list_stack.append('ul')
            self.list_counters.append(0)
        elif tag == 'ol':
            self.list_stack.append('ol')
            self.list_counters.append(0)
        elif tag == 'li':
            indent = "  " * max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1] == 'ol':
                self.list_counters[-1] += 1
                self.output.append(f"\n{indent}{self.list_counters[-1]}. ")
            else:
                self.output.append(f"\n{indent}- ")
        elif tag == 'br':
            self.output.append('  \n')
        elif tag == 'table':
            self.in_table = True
            self.table_rows = []
        elif tag in ('td', 'th'):
            self.in_cell = True
            self.current_cell = ""
        elif tag == 'tr':
            self.current_row = []
        elif tag == 'img':
            src = attrs_dict.get('src', '')
            alt = attrs_dict.get('alt', '')
            if src:
                self.output.append(f'![{alt}]({src})')
                
    def handle_endtag(self, tag):
        if tag.startswith('ac:'):
            if tag == 'ac:structured-macro':
                self.skip_macro = max(0, self.skip_macro - 1)
            return
            
        if self.skip_macro > 0:
            return
            
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            prefix = '#' * self.heading_level
            self.output.append(f"\n\n{prefix} {self.current_heading.strip()}\n")
            self.heading_level = 0
        elif tag == 'p':
            self.output.append('\n\n')
        elif tag == 'strong' or tag == 'b':
            self.in_strong = False
            self.output.append('**')
        elif tag == 'em' or tag == 'i':
            self.in_em = False
            self.output.append('*')
        elif tag == 'code':
            self.in_code = False
            self.output.append('`')
        elif tag == 'a':
            self.in_link = False
            if self.link_href:
                self.output.append(f'[{self.link_text}]({self.link_href})')
            else:
                self.output.append(self.link_text)
        elif tag == 'ul' or tag == 'ol':
            if self.list_stack:
                self.list_stack.pop()
            if self.list_counters:
                self.list_counters.pop()
            if not self.list_stack:
                self.output.append('\n')
        elif tag == 'li':
            pass
        elif tag in ('td', 'th'):
            self.in_cell = False
            self.current_row.append(self.current_cell.strip())
        elif tag == 'tr':
            self.table_rows.append(self.current_row)
        elif tag == 'table':
            self.in_table = False
            self._render_table()
    
    def handle_data(self, data):
        if self.skip_macro > 0:
            return
            
        # Zero-width joiner 등 제거
        clean = data.replace('\u200d', '').replace('\u200b', '')
        
        if self.heading_level > 0:
            self.current_heading += clean
        elif self.in_link:
            self.link_text += clean
        elif self.in_cell:
            self.current_cell += clean
        else:
            self.output.append(clean)
    
    def handle_entityref(self, name):
        entities = {'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'zwj': ''}
        char = entities.get(name, f'&{name};')
        if self.heading_level > 0:
            self.current_heading += char
        elif self.in_link:
            self.link_text += char
        elif self.in_cell:
            self.current_cell += char
        else:
            self.output.append(char)
    
    def _render_table(self):
        if not self.table_rows:
            return
        # 마크다운 테이블 생성
        max_cols = max(len(row) for row in self.table_rows)
        self.output.append('\n\n')
        for i, row in enumerate(self.table_rows):
            while len(row) < max_cols:
                row.append('')
            self.output.append('| ' + ' | '.join(row) + ' |\n')
            if i == 0:
                self.output.append('| ' + ' | '.join(['---'] * max_cols) + ' |\n')
        self.output.append('\n')
    
    def get_markdown(self):
        text = ''.join(self.output)
        # 연속 빈 줄 정리
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


def html_to_markdown(html_content):
    """Confluence storage format HTML을 Markdown으로 변환"""
    if not html_content or html_content.strip() in ('', '<p>&zwj;</p>', '<p local-id=""></p>'):
        return ""
    parser = ConfluenceHTMLToMarkdown()
    try:
        parser.feed(html_content)
        return parser.get_markdown()
    except Exception as e:
        # 파싱 실패 시 원본 HTML 반환
        return f"<!-- HTML 파싱 실패: {e} -->\n\n{html_content}"


def markdown_to_confluence_html(markdown_text):
    """Markdown을 Confluence storage format HTML로 변환"""
    lines = markdown_text.split('\n')
    html_parts = []
    in_list = False
    in_code_block = False
    code_lang = ""
    code_lines = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # 코드 블록 처리
        if line.strip().startswith('```'):
            if in_code_block:
                code_content = '\n'.join(code_lines)
                html_parts.append(
                    f'<ac:structured-macro ac:name="code" ac:schema-version="1">'
                    f'<ac:parameter ac:name="language">{code_lang}</ac:parameter>'
                    f'<ac:plain-text-body><![CDATA[{code_content}]]></ac:plain-text-body>'
                    f'</ac:structured-macro>'
                )
                in_code_block = False
                code_lines = []
            else:
                in_code_block = True
                code_lang = line.strip()[3:].strip()
            i += 1
            continue
        
        if in_code_block:
            code_lines.append(line)
            i += 1
            continue
        
        # 헤딩 처리
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            text = _inline_md_to_html(text)
            html_parts.append(f'<h{level}>{text}</h{level}>')
            i += 1
            continue
        
        # 리스트 처리
        list_match = re.match(r'^(\s*)([-*]|\d+\.)\s+(.+)$', line)
        if list_match:
            indent = len(list_match.group(1))
            marker = list_match.group(2)
            content = list_match.group(3)
            content = _inline_md_to_html(content)
            tag = 'ol' if re.match(r'\d+\.', marker) else 'ul'
            
            if not in_list:
                html_parts.append(f'<{tag}>')
                in_list = True
            html_parts.append(f'<li><p>{content}</p></li>')
            
            # 다음 줄이 리스트가 아니면 닫기
            if i + 1 >= len(lines) or not re.match(r'^\s*([-*]|\d+\.)\s+', lines[i + 1]):
                html_parts.append(f'</{tag}>')
                in_list = False
            i += 1
            continue
        
        # 빈 줄
        if not line.strip():
            i += 1
            continue
        
        # 일반 단락
        content = _inline_md_to_html(line)
        html_parts.append(f'<p>{content}</p>')
        i += 1
    
    return '\n'.join(html_parts)


def _inline_md_to_html(text):
    """인라인 마크다운을 Confluence storage HTML로 변환."""
    # 이미지 (링크보다 먼저 처리; ![alt](path) → <ac:image>)
    def img_repl(m):
        alt = m.group(1)
        path = m.group(2).strip()
        # 외부 URL
        if path.startswith(("http://", "https://")):
            return (f'<ac:image ac:alt="{_xml_attr(alt)}">'
                    f'<ri:url ri:value="{_xml_attr(path)}" />'
                    f'</ac:image>')
        # 로컬 첨부 — "attachments/foo.png" 또는 "foo.png" 모두 파일명만 추출
        filename = path.rsplit("/", 1)[-1]
        return (f'<ac:image ac:alt="{_xml_attr(alt)}">'
                f'<ri:attachment ri:filename="{_xml_attr(filename)}" />'
                f'</ac:image>')
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', img_repl, text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Inline code
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    # Links
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    return text


def _xml_attr(s: str) -> str:
    """XML 속성용 이스케이프."""
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;"))


# ─── 파일 해시 관리 ──────────────────────────────────────────────────
def load_hashes():
    """저장된 해시 로드"""
    if HASH_STORE_PATH.exists():
        with open(HASH_STORE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_hashes(hashes):
    """해시 저장"""
    with open(HASH_STORE_PATH, 'w', encoding='utf-8') as f:
        json.dump(hashes, f, indent=2, ensure_ascii=False)


def file_hash(filepath):
    """파일의 MD5 해시 계산"""
    if not Path(filepath).exists():
        return None
    with open(filepath, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


# ─── 메타데이터 관리 ─────────────────────────────────────────────────
def save_page_meta(page_id, version, title, local_path):
    """페이지 메타데이터 저장"""
    META_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "page_id": page_id,
        "version": version,
        "title": title,
        "local_path": str(local_path),
        "last_sync": datetime.datetime.now().isoformat()
    }
    meta_file = META_DIR / f"{page_id}.json"
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def load_page_meta(page_id):
    """페이지 메타데이터 로드"""
    meta_file = META_DIR / f"{page_id}.json"
    if meta_file.exists():
        with open(meta_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


# ─── 설정 로드 ───────────────────────────────────────────────────────
def load_config():
    """설정 파일 로드"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


# ─── PULL: Confluence → 로컬 ─────────────────────────────────────────
def create_page_file(page_id, title, local_path, content_html, version=1):
    """개별 페이지를 로컬 마크다운 파일로 생성"""
    full_path = CONTENT_ROOT / f"{local_path}.md"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    
    md_content = html_to_markdown(content_html)
    
    # 프론트매터 + 콘텐츠
    file_content = f"""---
confluence_page_id: "{page_id}"
title: "{title}"
confluence_version: {version}
last_synced: "{datetime.datetime.now().isoformat()}"
---

# {title}

{md_content}
"""
    
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(file_content)
    
    # 메타데이터 저장
    save_page_meta(page_id, version, title, str(full_path.relative_to(CONTENT_ROOT)))
    
    print(f"  ✅ {full_path.relative_to(CONTENT_ROOT)}")
    return str(full_path)


def create_folder_index(page_id, title, local_path, content_html="", version=1):
    """폴더와 index.md 생성"""
    folder_path = CONTENT_ROOT / local_path
    folder_path.mkdir(parents=True, exist_ok=True)
    
    md_content = html_to_markdown(content_html)
    
    index_path = folder_path / "_index.md"
    file_content = f"""---
confluence_page_id: "{page_id}"
title: "{title}"
confluence_version: {version}
last_synced: "{datetime.datetime.now().isoformat()}"
is_folder: true
---

# {title}

{md_content}
"""
    
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(file_content)
    
    save_page_meta(page_id, version, title, str(index_path.relative_to(CONTENT_ROOT)))
    print(f"  📁 {folder_path.relative_to(CONTENT_ROOT)}/")
    return str(folder_path)


def pull_page_content(page_id):
    """
    MCP를 통해 페이지 콘텐츠 가져오기
    (이 함수는 MCP 호출이 필요하므로, 실제로는 외부에서 호출됨)
    """
    # 이 함수는 sync_config.json의 정보를 기반으로 동작합니다
    # 실제 Confluence API 호출은 MCP를 통해 수행됩니다
    pass


# ─── PUSH: 로컬 → Confluence ─────────────────────────────────────────
def read_local_page(filepath):
    """로컬 마크다운 파일을 읽고 메타데이터 파싱"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 프론트매터 파싱
    meta = {}
    body = content
    
    fm_match = re.match(r'^---\n(.+?)\n---\n(.*)$', content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                meta[key.strip()] = val.strip().strip('"')
        body = fm_match.group(2).strip()
    
    # 본문에서 첫 번째 h1 제거 (타이틀과 중복)
    body = re.sub(r'^#\s+.+\n*', '', body, count=1)
    
    return meta, body.strip()


def prepare_push_content(filepath):
    """로컬 파일을 Confluence 업로드용으로 준비"""
    meta, body = read_local_page(filepath)
    
    page_id = meta.get('confluence_page_id', '')
    title = meta.get('title', '')
    version = int(meta.get('confluence_version', '1'))
    
    # 마크다운 → Confluence HTML 변환
    html_content = markdown_to_confluence_html(body)
    
    return {
        'page_id': page_id,
        'title': title,
        'current_version': version,
        'new_version': version + 1,
        'html_content': html_content,
        'local_path': filepath
    }


# ─── STATUS: 변경 사항 확인 ──────────────────────────────────────────
def check_status():
    """로컬 파일의 변경 사항 확인"""
    hashes = load_hashes()
    changed = []
    new_files = []
    
    for md_file in CONTENT_ROOT.rglob('*.md'):
        rel = str(md_file.relative_to(CONTENT_ROOT))
        current_hash = file_hash(md_file)
        
        if rel not in hashes:
            new_files.append(rel)
        elif hashes[rel] != current_hash:
            changed.append(rel)
    
    return changed, new_files


def update_hash_store():
    """모든 파일의 해시를 업데이트"""
    hashes = {}
    for md_file in CONTENT_ROOT.rglob('*.md'):
        rel = str(md_file.relative_to(CONTENT_ROOT))
        hashes[rel] = file_hash(md_file)
    save_hashes(hashes)


# ─── 트리 구조 출력 ──────────────────────────────────────────────────
def print_tree(directory, prefix=""):
    """폴더 구조를 트리 형태로 출력"""
    entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    for i, entry in enumerate(entries):
        is_last = (i == len(entries) - 1)
        connector = "└── " if is_last else "├── "
        if entry.is_dir():
            print(f"{prefix}{connector}📁 {entry.name}/")
            extension = "    " if is_last else "│   "
            print_tree(entry, prefix + extension)
        else:
            icon = "📄" if entry.suffix == '.md' else "📎"
            print(f"{prefix}{connector}{icon} {entry.name}")


# ─── 메인 ────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("""
╔══════════════════════════════════════════════════════════╗
║       Confluence ↔ Local 양방향 싱크 도구               ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  사용법:                                                 ║
║    python confluence_sync.py pull     # Confluence → 로컬║
║    python confluence_sync.py push ID  # 로컬 → Confluence║
║    python confluence_sync.py status   # 변경 사항 확인   ║
║    python confluence_sync.py tree     # 폴더 구조 출력   ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
        """)
        return
    
    command = sys.argv[1].lower()
    
    if command == 'pull':
        print("\n📥 Confluence → 로컬 동기화는 MCP를 통해 수행됩니다.")
        print("   이 스크립트의 pull 기능은 이미 초기 다운로드 시 실행되었습니다.")
        print("   다시 pull하려면 Antigravity에게 요청하세요.\n")
        
    elif command == 'push':
        if len(sys.argv) < 3:
            print("❌ 페이지 ID를 지정하세요: python confluence_sync.py push <page_id>")
            return
        page_id = sys.argv[2]
        
        # 메타에서 로컬 파일 찾기
        meta = load_page_meta(page_id)
        if not meta:
            print(f"❌ 페이지 ID {page_id}에 대한 메타데이터를 찾을 수 없습니다.")
            return
        
        filepath = CONTENT_ROOT / meta['local_path']
        if not filepath.exists():
            print(f"❌ 로컬 파일을 찾을 수 없습니다: {filepath}")
            return
        
        push_data = prepare_push_content(str(filepath))
        print(f"\n📤 Push 준비 완료:")
        print(f"   페이지: {push_data['title']}")
        print(f"   ID: {push_data['page_id']}")
        print(f"   현재 버전: {push_data['current_version']} → 새 버전: {push_data['new_version']}")
        print(f"\n   생성된 HTML을 MCP를 통해 Confluence에 업로드합니다...")
        
        # Push 데이터를 임시 파일에 저장 (MCP가 읽을 수 있도록)
        push_file = SCRIPT_DIR / f".push_queue_{page_id}.json"
        with open(push_file, 'w', encoding='utf-8') as f:
            json.dump(push_data, f, indent=2, ensure_ascii=False)
        print(f"   📋 Push 데이터 저장: {push_file}")
        
    elif command == 'status':
        print("\n📊 변경 사항 확인 중...\n")
        changed, new_files = check_status()
        
        if changed:
            print("🔄 변경된 파일:")
            for f in changed:
                print(f"   - {f}")
        else:
            print("✅ 변경된 파일 없음")
        
        if new_files:
            print(f"\n🆕 새 파일 ({len(new_files)}개):")
            for f in new_files:
                print(f"   + {f}")
        print()
        
    elif command == 'tree':
        if CONTENT_ROOT.exists():
            print(f"\n📂 {CONTENT_ROOT.name}/")
            print_tree(CONTENT_ROOT)
            print()
        else:
            print("❌ 콘텐츠 디렉토리가 없습니다. 먼저 pull을 실행하세요.")
    
    elif command == 'update-hashes':
        update_hash_store()
        print("✅ 해시 저장소 업데이트 완료")
    
    else:
        print(f"❌ 알 수 없는 명령: {command}")


if __name__ == '__main__':
    main()
