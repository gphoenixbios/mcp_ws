import json
import os
import re
import datetime
from pathlib import Path
from html.parser import HTMLParser

SCRIPT_DIR = Path(__file__).parent.resolve()
CONTENT_ROOT = SCRIPT_DIR.parent / "confluence_content"
RAW_DIR = SCRIPT_DIR / "raw_html"
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"

class H2M(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out=[];self.ls=[];self.lc=[];self.hl=0;self.ht="";self.sk=0
        self.ins=False;self.ine=False;self.inc=False;self.inl=False;self.lh="";self.lt=""
        self.in_table = False; self.table_rows = []; self.current_row = []; self.current_cell = ""; self.in_cell = False
    def handle_starttag(self,t,a):
        ad=dict(a)
        if t.startswith('ac:'):
            if t=='ac:structured-macro':self.sk+=1
            return
        if self.sk>0:return
        if t in('h1','h2','h3','h4','h5','h6'):self.hl=int(t[1]);self.ht=""
        elif t in('strong','b'):self.ins=True;self.out.append('**')
        elif t in('em','i'):self.ine=True;self.out.append('*')
        elif t=='code':self.inc=True;self.out.append('`')
        elif t=='a':self.inl=True;self.lh=ad.get('href','');self.lt=""
        elif t=='ul':self.ls.append('ul');self.lc.append(0)
        elif t=='ol':self.ls.append('ol');self.lc.append(0)
        elif t=='li':
            ind="  "*max(0,len(self.ls)-1)
            if self.ls and self.ls[-1]=='ol':self.lc[-1]+=1;self.out.append(f"\n{ind}{self.lc[-1]}. ")
            else:self.out.append(f"\n{ind}- ")
        elif t=='br':self.out.append('  \n')
        elif t == 'table': self.in_table = True; self.table_rows = []
        elif t in ('td', 'th'): self.in_cell = True; self.current_cell = ""
        elif t == 'tr': self.current_row = []
        elif t == 'img':
            src = ad.get('src', '')
            alt = ad.get('alt', '')
            if src: self.out.append(f'![{alt}]({src})')
            elif ad.get('ac:alt'): self.out.append(f'*(Confluence 첨부 이미지: {ad.get("ac:alt")})*')

    def handle_endtag(self,t):
        if t.startswith('ac:'):
            if t=='ac:structured-macro':self.sk=max(0,self.sk-1)
            return
        if self.sk>0:return
        if t in('h1','h2','h3','h4','h5','h6'):
            self.out.append(f"\n\n{'#'*self.hl} {self.ht.strip()}\n");self.hl=0
        elif t=='p':self.out.append('\n\n')
        elif t in('strong','b'):self.ins=False;self.out.append('**')
        elif t in('em','i'):self.ine=False;self.out.append('*')
        elif t=='code':self.inc=False;self.out.append('`')
        elif t=='a':
            self.inl=False
            self.out.append(f'[{self.lt}]({self.lh})' if self.lh else self.lt)
        elif t in('ul','ol'):
            if self.ls:self.ls.pop()
            if self.lc:self.lc.pop()
            if not self.ls:self.out.append('\n')
        elif t in ('td', 'th'): self.in_cell = False; self.current_row.append(self.current_cell.strip())
        elif t == 'tr': self.table_rows.append(self.current_row)
        elif t == 'table': self.in_table = False; self._render_table()

    def handle_data(self,d):
        if self.sk>0:return
        c=d.replace('\u200d','').replace('\u200b','')
        if self.hl>0:self.ht+=c
        elif self.inl:self.lt+=c
        elif self.in_cell: self.current_cell += c
        else:self.out.append(c)

    def handle_entityref(self,n):
        e={'amp':'&','lt':'<','gt':'>','quot':'"','zwj':''}
        c=e.get(n,f'&{n};')
        if self.hl>0:self.ht+=c
        elif self.inl:self.lt+=c
        elif self.in_cell: self.current_cell += c
        else:self.out.append(c)
        
    def _render_table(self):
        if not self.table_rows: return
        max_cols = max(len(row) for row in self.table_rows)
        self.out.append('\n\n')
        for i, row in enumerate(self.table_rows):
            while len(row) < max_cols: row.append('')
            self.out.append('| ' + ' | '.join(row) + ' |\n')
            if i == 0: self.out.append('| ' + ' | '.join(['---'] * max_cols) + ' |\n')
        self.out.append('\n')

    def md(self):
        t=''.join(self.out)
        t = re.sub(r'π\*', 'π\*', t) # 이스케이프 처리 추가
        return re.sub(r'\n{3,}','\n\n',t).strip()

def h2m(html):
    if not html or html.strip() in('','<p>&zwj;</p>'):return ""
    p=H2M();p.feed(html);return p.md()

def main():
    print("\n🚀 Confluence 로컬 구조 재생성 시작...\n")
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
        
    pages = config.get('page_tree', {})
    
    for pid, info in pages.items():
        if pid.endswith('_children'): continue
        
        html = ""
        ver = 1
        raw_file = RAW_DIR / f"{pid}.json"
        
        if raw_file.exists():
            try:
                with open(raw_file, 'r') as f:
                    data = json.load(f)
                    html = data.get('html', '')
                    ver = data.get('ver', 1)
            except Exception as e:
                print(f"Error loading {raw_file}: {e}")
                
        is_folder = info.get('is_folder', False)
        lpath = info['local_path']
        title = info['title']
        
        if is_folder:
            fp=CONTENT_ROOT/lpath;fp.mkdir(parents=True,exist_ok=True)
            fp=fp/"_index.md"
        else:
            fp=CONTENT_ROOT/f"{lpath}.md";fp.parent.mkdir(parents=True,exist_ok=True)
            
        md=h2m(html)
        now=datetime.datetime.now().isoformat()
        with open(fp,'w',encoding='utf-8') as f:
            f.write(f'---\nconfluence_page_id: "{pid}"\ntitle: "{title}"\nconfluence_version: {ver}\nlast_synced: "{now}"\n---\n\n# {title}\n\n{md}\n')
            
        icon="📁" if is_folder else "📄"
        print(f"  {icon} {fp.relative_to(CONTENT_ROOT)}")

if __name__=='__main__':
    main()
