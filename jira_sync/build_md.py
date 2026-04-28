#!/usr/bin/env python3
"""
raw_issues/*.json + raw_sprints.json + raw_board.json → 마크다운 빌드.

산출물 (../jira_content/<PROJECT>/):
  index.md                   # 프로젝트 개요 (보드/스프린트/통계)
  board.md                   # 칸반 뷰 (상태별 그룹)
  sprints/<id>_<name>.md     # 스프린트별 리포트
  issues/<KEY>.md            # 이슈별 상세 (프런트매터 포함)
"""

import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
WS_ROOT = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
RAW_BOARD = SCRIPT_DIR / "raw_board.json"
RAW_SPRINTS = SCRIPT_DIR / "raw_sprints.json"
RAW_ISSUES_DIR = SCRIPT_DIR / "raw_issues"


# ─── HTML → Markdown 변환기 ─────────────────────────────────────────
class HTMLToMarkdown(HTMLParser):
    """Jira renderedFields HTML 을 마크다운으로 변환."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.list_stack: list[str] = []  # 'ul' / 'ol'
        self.list_counters: list[int] = []
        self.heading_level = 0
        self.heading_buf = ""
        self.in_link = False
        self.link_href = ""
        self.link_buf = ""
        self.in_code_inline = False
        self.in_pre = False
        self.pre_lang = ""
        self.pre_buf = ""
        self.in_table = False
        self.table_rows: list[list[str]] = []
        self.cur_row: list[str] = []
        self.cur_cell = ""
        self.in_cell = False
        self.in_blockquote = False

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading_level = int(tag[1])
            self.heading_buf = ""
        elif tag == "p":
            pass
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a":
            self.in_link = True
            self.link_href = ad.get("href", "")
            self.link_buf = ""
        elif tag == "br":
            self._emit("  \n")
        elif tag == "hr":
            self._emit("\n---\n")
        elif tag == "ul":
            self.list_stack.append("ul")
            self.list_counters.append(0)
        elif tag == "ol":
            self.list_stack.append("ol")
            self.list_counters.append(0)
        elif tag == "li":
            indent = "  " * max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.list_counters[-1] += 1
                self._emit(f"\n{indent}{self.list_counters[-1]}. ")
            else:
                self._emit(f"\n{indent}- ")
        elif tag == "code" and not self.in_pre:
            self.in_code_inline = True
            self._emit("`")
        elif tag == "pre":
            self.in_pre = True
            self.pre_lang = ""
            self.pre_buf = ""
            cls = ad.get("class", "")
            m = re.search(r"language-(\w+)", cls)
            if m:
                self.pre_lang = m.group(1)
        elif tag == "blockquote":
            self.in_blockquote = True
            self._emit("\n> ")
        elif tag == "img":
            src = ad.get("src", "")
            alt = ad.get("alt", "")
            if src:
                self._emit(f"![{alt}]({src})")
        elif tag == "table":
            self.in_table = True
            self.table_rows = []
        elif tag == "tr":
            self.cur_row = []
        elif tag in ("td", "th"):
            self.in_cell = True
            self.cur_cell = ""

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            prefix = "#" * self.heading_level
            self.parts.append(f"\n\n{prefix} {self.heading_buf.strip()}\n")
            self.heading_level = 0
            self.heading_buf = ""
        elif tag == "p":
            self._emit("\n\n")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a":
            text = self.link_buf or self.link_href
            if self.link_href:
                self._emit(f"[{text}]({self.link_href})")
            else:
                self._emit(text)
            self.in_link = False
            self.link_href = ""
            self.link_buf = ""
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            if self.list_counters:
                self.list_counters.pop()
            if not self.list_stack:
                self._emit("\n")
        elif tag == "code" and self.in_code_inline:
            self.in_code_inline = False
            self._emit("`")
        elif tag == "pre":
            self.in_pre = False
            content = self.pre_buf.rstrip("\n")
            self.parts.append(f"\n\n```{self.pre_lang}\n{content}\n```\n\n")
            self.pre_buf = ""
            self.pre_lang = ""
        elif tag == "blockquote":
            self.in_blockquote = False
            self._emit("\n")
        elif tag in ("td", "th"):
            self.in_cell = False
            self.cur_row.append(self.cur_cell.strip().replace("\n", " "))
        elif tag == "tr":
            if self.cur_row:
                self.table_rows.append(self.cur_row)
        elif tag == "table":
            self.in_table = False
            self._render_table()

    def handle_data(self, data):
        clean = data.replace("‍", "").replace("​", "")
        if self.in_pre:
            self.pre_buf += clean
        elif self.heading_level > 0:
            self.heading_buf += clean
        elif self.in_link:
            self.link_buf += clean
        elif self.in_cell:
            self.cur_cell += clean
        else:
            self.parts.append(clean)

    def handle_entityref(self, name):
        ents = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "nbsp": " ", "zwj": ""}
        ch = ents.get(name, f"&{name};")
        self.handle_data(ch)

    def handle_charref(self, name):
        try:
            ch = chr(int(name[1:], 16) if name.startswith("x") else int(name))
        except Exception:
            ch = ""
        self.handle_data(ch)

    def _emit(self, s: str):
        if self.in_cell:
            self.cur_cell += s
        elif self.heading_level > 0:
            self.heading_buf += s
        elif self.in_link:
            self.link_buf += s
        else:
            self.parts.append(s)

    def _render_table(self):
        if not self.table_rows:
            return
        cols = max(len(r) for r in self.table_rows)
        self.parts.append("\n\n")
        for i, row in enumerate(self.table_rows):
            while len(row) < cols:
                row.append("")
            self.parts.append("| " + " | ".join(row) + " |\n")
            if i == 0:
                self.parts.append("| " + " | ".join(["---"] * cols) + " |\n")
        self.parts.append("\n")

    def get_md(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_md(html: str) -> str:
    if not html or not html.strip():
        return ""
    p = HTMLToMarkdown()
    try:
        p.feed(html)
        return p.get_md()
    except Exception as e:
        return f"<!-- HTML 변환 실패: {e} -->\n\n{html}"


# ─── 유틸 ───────────────────────────────────────────────────────────
def safe_filename(s: str) -> str:
    # 마크다운 링크가 깨지지 않도록 공백/특수문자 모두 변환
    s = re.sub(r"[\\/:*?\"<>| ]+", "_", s).strip("_.")
    return s or "untitled"


# statusCategory.key 는 로케일과 무관 (new / indeterminate / done / undefined)
STATUS_CAT_LABEL = {
    "new": "To Do",
    "indeterminate": "In Progress",
    "done": "Done",
    "undefined": "Other",
}
STATUS_CAT_ORDER = ["new", "indeterminate", "done", "undefined"]


def status_cat_key(issue: dict) -> str:
    cat = (((issue.get("fields") or {}).get("status") or {})
           .get("statusCategory") or {})
    return cat.get("key") or "undefined"


def fmt_dt(s: str) -> str:
    if not s:
        return ""
    # "2026-04-25T14:30:00.000+0900" → "2026-04-25 14:30"
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", s)
    return f"{m.group(1)} {m.group(2)}" if m else s


def find_sprint_field(fields: dict) -> list[dict]:
    """이슈의 customfield_* 중 sprint 배열을 찾는다."""
    for k, v in fields.items():
        if not k.startswith("customfield_"):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if {"id", "name", "state"}.issubset(v[0].keys()):
                return v
    return []


def yaml_str(s: str) -> str:
    """프런트매터용 문자열 이스케이프."""
    if s is None:
        return '""'
    s = str(s).replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


# ─── 빌더 ───────────────────────────────────────────────────────────
def load_all_issues() -> list[dict]:
    issues = []
    if not RAW_ISSUES_DIR.exists():
        return issues
    for f in sorted(RAW_ISSUES_DIR.glob("*.json")):
        issues.append(json.loads(f.read_text(encoding="utf-8")))
    return issues


def render_issue_md(issue: dict, base_url: str, out_dir: Path) -> Path:
    f = issue.get("fields", {})
    rendered = issue.get("renderedFields", {}) or {}
    key = issue["key"]
    summary = f.get("summary", "")
    status = (f.get("status") or {}).get("name", "")
    status_cat = ((f.get("status") or {}).get("statusCategory") or {}).get("name", "")
    itype = (f.get("issuetype") or {}).get("name", "")
    priority = (f.get("priority") or {}).get("name", "")
    assignee = (f.get("assignee") or {}).get("displayName", "") if f.get("assignee") else ""
    reporter = (f.get("reporter") or {}).get("displayName", "") if f.get("reporter") else ""
    created = fmt_dt(f.get("created", ""))
    updated = fmt_dt(f.get("updated", ""))
    duedate = f.get("duedate", "") or ""
    parent_key = (f.get("parent") or {}).get("key", "") if f.get("parent") else ""
    labels = f.get("labels", []) or []
    sprints = find_sprint_field(f)
    sprint_names = [s.get("name", "") for s in sprints]

    desc_html = rendered.get("description") or ""
    desc_md = html_to_md(desc_html)

    # 댓글
    comments = issue.get("_comments_full") or []
    comment_blocks = []
    for c in comments:
        author = (c.get("author") or {}).get("displayName", "")
        when = fmt_dt(c.get("created", ""))
        body_md = html_to_md(c.get("renderedBody") or "")
        comment_blocks.append(f"### 💬 {author} — {when}\n\n{body_md}\n")

    # 서브태스크
    subtasks = f.get("subtasks", []) or []
    sub_lines = []
    for st in subtasks:
        sk = st.get("key", "")
        ss = (st.get("fields") or {}).get("summary", "")
        sst = ((st.get("fields") or {}).get("status") or {}).get("name", "")
        sub_lines.append(f"- [{sk}](./{sk}.md) — {ss} *({sst})*")

    # 링크
    issuelinks = f.get("issuelinks", []) or []
    link_lines = []
    for il in issuelinks:
        ltype = (il.get("type") or {}).get("name", "")
        for direction in ("outwardIssue", "inwardIssue"):
            other = il.get(direction)
            if not other:
                continue
            ok = other.get("key", "")
            os_ = ((other.get("fields") or {}).get("status") or {}).get("name", "")
            osu = (other.get("fields") or {}).get("summary", "")
            link_lines.append(f"- **{ltype}** → [{ok}](./{ok}.md): {osu} *({os_})*")

    fm_lines = [
        "---",
        f"key: {yaml_str(key)}",
        f"summary: {yaml_str(summary)}",
        f"status: {yaml_str(status)}",
        f"status_category: {yaml_str(status_cat)}",
        f"issue_type: {yaml_str(itype)}",
        f"priority: {yaml_str(priority)}",
        f"assignee: {yaml_str(assignee)}",
        f"reporter: {yaml_str(reporter)}",
        f"created: {yaml_str(created)}",
        f"updated: {yaml_str(updated)}",
        f"duedate: {yaml_str(duedate)}",
        f"parent: {yaml_str(parent_key)}",
        f"labels: [{', '.join(yaml_str(l) for l in labels)}]",
        f"sprints: [{', '.join(yaml_str(n) for n in sprint_names)}]",
        f"url: {yaml_str(f'{base_url}/browse/{key}')}",
        "---",
    ]

    body = [f"# [{key}] {summary}", ""]
    body.append("| 항목 | 값 |")
    body.append("| --- | --- |")
    body.append(f"| 상태 | {status} ({status_cat}) |")
    body.append(f"| 유형 | {itype} |")
    body.append(f"| 우선순위 | {priority} |")
    body.append(f"| 담당자 | {assignee or '_미지정_'} |")
    body.append(f"| 보고자 | {reporter} |")
    body.append(f"| 생성 | {created} |")
    body.append(f"| 수정 | {updated} |")
    body.append(f"| 마감 | {duedate or '_없음_'} |")
    if parent_key:
        body.append(f"| 상위 | [{parent_key}](./{parent_key}.md) |")
    if labels:
        body.append(f"| 레이블 | {', '.join(labels)} |")
    if sprint_names:
        body.append(f"| 스프린트 | {', '.join(sprint_names)} |")
    body.append(f"| Jira | [{key}]({base_url}/browse/{key}) |")
    body.append("")

    if desc_md:
        body.append("## 📝 Description")
        body.append("")
        body.append(desc_md)
        body.append("")

    if sub_lines:
        body.append("## 📋 Subtasks")
        body.append("")
        body.extend(sub_lines)
        body.append("")

    if link_lines:
        body.append("## 🔗 Links")
        body.append("")
        body.extend(link_lines)
        body.append("")

    if comment_blocks:
        body.append("## 💬 Comments")
        body.append("")
        body.extend(comment_blocks)

    out = out_dir / f"{key}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(fm_lines) + "\n\n" + "\n".join(body), encoding="utf-8")
    return out


def render_board_md(issues: list[dict], board: dict, base_url: str, out_path: Path):
    """상태 카테고리별 칸반 뷰 (key 기반: new/indeterminate/done)."""
    columns: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        columns[status_cat_key(issue)].append(issue)

    proj_key = (board.get("location") or {}).get("projectKey", "")
    board_url = f"{base_url}/jira/software/projects/{proj_key}/boards/{board.get('id', '')}"
    lines = [f"# 🧭 Board: {board.get('name', '')}", ""]
    lines.append(f"- 보드 타입: `{board.get('type', '')}`")
    lines.append(f"- 총 이슈: {len(issues)}")
    lines.append(f"- Jira: [{board_url}]({board_url})")
    lines.append("")

    for ck in STATUS_CAT_ORDER:
        items = columns.get(ck, [])
        if not items:
            continue
        label = STATUS_CAT_LABEL[ck]
        lines.append(f"## {label} ({len(items)})")
        lines.append("")
        lines.append("| Key | 유형 | 요약 | 담당자 | 상태 | 우선순위 | 마감 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for issue in sorted(items, key=lambda x: x["key"]):
            f = issue.get("fields", {})
            key = issue["key"]
            summary = (f.get("summary") or "").replace("|", "\\|")
            itype = (f.get("issuetype") or {}).get("name", "")
            assignee = (f.get("assignee") or {}).get("displayName", "") if f.get("assignee") else "_미지정_"
            status_name = (f.get("status") or {}).get("name", "")
            priority = (f.get("priority") or {}).get("name", "")
            duedate = f.get("duedate", "") or ""
            lines.append(f"| [{key}](./issues/{key}.md) | {itype} | {summary} | {assignee} | {status_name} | {priority} | {duedate} |")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def render_sprint_md(sprint: dict, issues: list[dict], out_path: Path):
    sid = sprint.get("id")
    name = sprint.get("name", "")
    state = sprint.get("state", "")
    start = fmt_dt(sprint.get("startDate", ""))
    end = fmt_dt(sprint.get("endDate", ""))
    completed = fmt_dt(sprint.get("completeDate", ""))
    goal = sprint.get("goal", "")

    matched = []
    for issue in issues:
        for s in find_sprint_field(issue.get("fields", {})):
            if s.get("id") == sid:
                matched.append(issue)
                break

    columns: dict[str, list[dict]] = defaultdict(list)
    for issue in matched:
        columns[status_cat_key(issue)].append(issue)

    lines = [f"# 🏃 Sprint: {name}", ""]
    lines.append(f"- 상태: **{state}**")
    if start:
        lines.append(f"- 시작: {start}")
    if end:
        lines.append(f"- 종료: {end}")
    if completed:
        lines.append(f"- 완료: {completed}")
    if goal:
        lines.append(f"- 목표: {goal}")
    lines.append(f"- 이슈 수: {len(matched)}")
    lines.append("")

    for ck in ["done", "indeterminate", "new", "undefined"]:
        items = columns.get(ck, [])
        if not items:
            continue
        label = STATUS_CAT_LABEL[ck]
        lines.append(f"## {label} ({len(items)})")
        lines.append("")
        lines.append("| Key | 유형 | 요약 | 담당자 | 상태 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for issue in sorted(items, key=lambda x: x["key"]):
            f = issue.get("fields", {})
            key = issue["key"]
            summary = (f.get("summary") or "").replace("|", "\\|")
            itype = (f.get("issuetype") or {}).get("name", "")
            assignee = (f.get("assignee") or {}).get("displayName", "") if f.get("assignee") else "_미지정_"
            status = (f.get("status") or {}).get("name", "")
            lines.append(f"| [{key}](../issues/{key}.md) | {itype} | {summary} | {assignee} | {status} |")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def render_index_md(config: dict, board: dict, sprints: list[dict],
                    issues: list[dict], project_root: Path):
    base_url = config["jira_base_url"].rstrip("/")
    project_key = config["project_key"]
    stats = config.get("stats", {})

    # 통계
    by_status: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    by_assignee: dict[str, int] = defaultdict(int)
    for issue in issues:
        f = issue.get("fields", {})
        by_status[(f.get("status") or {}).get("name", "Unknown")] += 1
        by_type[(f.get("issuetype") or {}).get("name", "Unknown")] += 1
        a = (f.get("assignee") or {}).get("displayName", "") if f.get("assignee") else "_미지정_"
        by_assignee[a] += 1

    lines = [f"# 📊 {board.get('name', project_key)} — Jira 스냅샷", ""]
    lines.append(f"- 프로젝트: `{project_key}`")
    lines.append(f"- 보드: [{board.get('name', '')}]({base_url}/jira/software/projects/{project_key}/boards/{board.get('id', '')}) (`{board.get('type', '')}`)")
    lines.append(f"- 마지막 동기화: {stats.get('last_synced', '')}")
    lines.append(f"- 총 이슈: {len(issues)} / 스프린트: {len(sprints)}")
    lines.append("")
    lines.append("## 🗂 빠른 이동")
    lines.append("")
    lines.append("- [📋 보드 (칸반 뷰)](./board.md)")
    if sprints:
        lines.append("- 🏃 스프린트:")
        for s in sorted(sprints, key=lambda x: (x.get("state", ""), x.get("startDate", "") or "")):
            sname = s.get("name", "")
            sstate = s.get("state", "")
            sfile = f"sprints/{s['id']}_{safe_filename(sname)}.md"
            lines.append(f"  - [{sname}](./{sfile}) *({sstate})*")
    lines.append("- 📁 [이슈 전체 폴더](./issues/)")
    lines.append("")

    lines.append("## 📈 상태 분포")
    lines.append("")
    lines.append("| 상태 | 개수 |")
    lines.append("| --- | --- |")
    for k, v in sorted(by_status.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")

    lines.append("## 🏷 유형 분포")
    lines.append("")
    lines.append("| 유형 | 개수 |")
    lines.append("| --- | --- |")
    for k, v in sorted(by_type.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")

    lines.append("## 👥 담당자별")
    lines.append("")
    lines.append("| 담당자 | 개수 |")
    lines.append("| --- | --- |")
    for k, v in sorted(by_assignee.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")

    (project_root / "index.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    base_url = config["jira_base_url"].rstrip("/")
    project_key = config["project_key"]

    board = json.loads(RAW_BOARD.read_text(encoding="utf-8")) if RAW_BOARD.exists() else {}
    sprints = json.loads(RAW_SPRINTS.read_text(encoding="utf-8")) if RAW_SPRINTS.exists() else []
    issues = load_all_issues()

    print(f"\n🔄 MD 빌드 시작 — 이슈 {len(issues)}, 스프린트 {len(sprints)}")

    project_root = WS_ROOT / "jira_content" / project_key
    issues_dir = project_root / "issues"
    sprints_dir = project_root / "sprints"
    project_root.mkdir(parents=True, exist_ok=True)
    issues_dir.mkdir(parents=True, exist_ok=True)
    sprints_dir.mkdir(parents=True, exist_ok=True)

    # 이슈
    for issue in issues:
        out = render_issue_md(issue, base_url, issues_dir)
        print(f"  📄 {out.relative_to(WS_ROOT)}")

    # 보드
    render_board_md(issues, board, base_url, project_root / "board.md")
    print(f"  📋 {(project_root / 'board.md').relative_to(WS_ROOT)}")

    # 스프린트
    for s in sprints:
        sname = s.get("name", "")
        sfile = sprints_dir / f"{s['id']}_{safe_filename(sname)}.md"
        render_sprint_md(s, issues, sfile)
        print(f"  🏃 {sfile.relative_to(WS_ROOT)}")

    # 인덱스
    render_index_md(config, board, sprints, issues, project_root)
    print(f"  📊 {(project_root / 'index.md').relative_to(WS_ROOT)}")

    # ─── Prune: 현재 데이터에 없는 stale .md 삭제 ────────────────────
    current_issue_keys = {issue["key"] for issue in issues}
    current_sprint_files = {f"{s['id']}_{safe_filename(s.get('name', ''))}.md" for s in sprints}
    pruned = 0
    for f in issues_dir.glob("*.md"):
        if f.stem not in current_issue_keys:
            f.unlink()
            pruned += 1
            print(f"  🗑  prune: {f.relative_to(WS_ROOT)}")
    for f in sprints_dir.glob("*.md"):
        if f.name not in current_sprint_files:
            f.unlink()
            pruned += 1
            print(f"  🗑  prune: {f.relative_to(WS_ROOT)}")
    if pruned:
        print(f"  - {pruned} stale .md 삭제")

    print(f"\n✅ 빌드 완료. 진입점: jira_content/{project_key}/index.md\n")


if __name__ == "__main__":
    main()
