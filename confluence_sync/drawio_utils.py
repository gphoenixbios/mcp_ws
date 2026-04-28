"""
drawio (mxfile XML) ↔ mermaid (flowchart) 변환 + Confluence storage HTML 안에서
draw.io 매크로 검출 유틸.

지원 범위:
  - drawio 매크로 두 종류:
      * static/inc-drawio  (Embed draw.io Diagram)  — `diagram-display-name`
      * static/drawio      (draw.io Diagram)        — `diagram-name`
  - drawio → mermaid: 단순 flowchart 변환 (vertex + edge).
    복잡한 도형/스타일은 손실됨 → 노트 주석으로 남김.
  - mermaid → drawio: 단순 flowchart 변환 (자동 격자 레이아웃).
    완전한 1:1 매핑이 아니며, 손실이 있음.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


# ─── 1) Confluence storage HTML 안에서 drawio 매크로 추출 ─────────────

@dataclass
class DrawioMacro:
    """페이지 안의 drawio 임베드 한 개."""
    kind: str               # "drawio" | "inc-drawio"
    diagram_name: str       # 첨부 파일 이름 (확장자 포함된 경우 그대로)
    width: str | None = None
    height: str | None = None
    raw_block: str = ""     # 매크로 블록 원본 — replace 시 사용


_PARAM_RE = re.compile(
    r'<ac:adf-parameter\s+key="([^"]+)"(?:\s+type="[^"]+")?>(.*?)</ac:adf-parameter>',
    re.DOTALL,
)
_EXT_BLOCK_RE = re.compile(r"<ac:adf-extension\b.*?</ac:adf-extension>", re.DOTALL)


def _extract_params(block: str) -> dict[str, str]:
    """adf-parameter 들을 평탄화. 중첩 태그는 텍스트만 살린다."""
    out: dict[str, str] = {}
    for m in _PARAM_RE.finditer(block):
        k = m.group(1)
        v_raw = m.group(2)
        # 중첩 태그 제거
        v = re.sub(r"<[^>]+>", " ", v_raw).strip()
        # 첫번째 값을 우선 (Confluence 가 fallback 으로 같은 블록을 한 번 더 넣어둠)
        out.setdefault(k, v)
    return out


def find_drawio_macros(storage_html: str) -> list[DrawioMacro]:
    """storage HTML 에서 drawio 임베드를 모두 찾는다."""
    out: list[DrawioMacro] = []
    seen_local_ids: set[str] = set()  # ADF 가 fallback 으로 두 번 들어있어 중복 제거
    for m in _EXT_BLOCK_RE.finditer(storage_html):
        block = m.group(0)
        if "drawio" not in block:
            continue
        params = _extract_params(block)
        # extension-id 는 adf-parameter 또는 adf-attribute(extension-key) 어느 쪽에도 올 수 있음.
        ext_marker = params.get("extension-id", "")
        if not ext_marker:
            m_attr = re.search(
                r'<ac:adf-attribute key="extension-key">([^<]+)</ac:adf-attribute>', block
            )
            if m_attr:
                ext_marker = m_attr.group(1).strip()

        if "inc-drawio" in ext_marker:
            kind = "inc-drawio"
            name = (params.get("diagram-display-name")
                    or params.get("guest-params") or "").strip()
        elif "drawio" in ext_marker:
            kind = "drawio"
            name = (params.get("diagram-name")
                    or params.get("diagram-display-name") or "").strip()
        else:
            continue

        if not name:
            continue

        local_id = params.get("local-id", "")
        # local-id 없으면 name+block_pos 로 dedupe
        dedupe_key = local_id or f"{name}@{m.start()}"
        if dedupe_key in seen_local_ids:
            continue
        seen_local_ids.add(dedupe_key)

        out.append(DrawioMacro(
            kind=kind,
            diagram_name=name,
            width=params.get("width"),
            height=params.get("height"),
            raw_block=block,
        ))
    return out


# ─── 2) drawio (mxfile XML) 파싱 ─────────────────────────────────────

@dataclass
class _Cell:
    id: str
    value: str = ""
    style: str = ""
    vertex: bool = False
    edge: bool = False
    source: str | None = None
    target: str | None = None
    parent: str | None = None


def _parse_mxcells(drawio_xml: str) -> list[_Cell]:
    """mxfile / mxGraphModel 에서 mxCell 들을 추출."""
    try:
        root = ET.fromstring(drawio_xml)
    except ET.ParseError:
        return []

    cells: list[_Cell] = []
    for c in root.iter("mxCell"):
        cells.append(_Cell(
            id=c.get("id", ""),
            value=c.get("value", "") or "",
            style=c.get("style", "") or "",
            vertex=c.get("vertex") == "1",
            edge=c.get("edge") == "1",
            source=c.get("source"),
            target=c.get("target"),
            parent=c.get("parent"),
        ))

    # UserObject 로 감싼 경우도 있음 — UserObject 의 자식 mxCell 의 label 은
    # UserObject 의 label 속성에서 옴.
    for uo in root.iter("UserObject"):
        label = uo.get("label", "") or ""
        for c in uo.iter("mxCell"):
            for cell in cells:
                if cell.id == c.get("id", "") and not cell.value:
                    cell.value = label
                    break

    return cells


def _shape_for_style(style: str) -> str:
    """drawio 스타일 문자열에서 mermaid flowchart 노드 모양을 추정."""
    s = (style or "").lower()
    if "rhombus" in s or "shape=rhombus" in s:
        return "diamond"
    if "ellipse" in s:
        return "round"
    if "cylinder" in s or "shape=cylinder" in s:
        return "cylinder"
    if "shape=document" in s:
        return "document"
    if "stadium" in s or "shape=mxgraph.flowchart.terminator" in s:
        return "stadium"
    return "rect"


def _clean_label(s: str) -> str:
    """drawio value 는 종종 HTML 단편을 포함 — 텍스트만 남긴다."""
    if not s:
        return ""
    # &nbsp; 등 엔티티 풀기
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    # <br>, <br/> 는 줄바꿈으로 — mermaid 는 \\n 또는 <br/>
    s = re.sub(r"<br\s*/?>", " / ", s, flags=re.IGNORECASE)
    # 그 외 태그 제거
    s = re.sub(r"<[^>]+>", "", s)
    # 연속 공백 정리
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _mmd_node(node_id: str, label: str, shape: str) -> str:
    clean = _clean_label(label) or node_id
    # mermaid label 안의 큰따옴표는 안전하게 작은따옴표로
    clean = clean.replace('"', "'")
    if shape == "diamond":
        return f'{node_id}{{"{clean}"}}'
    if shape == "round":
        return f'{node_id}(("{clean}"))'
    if shape == "stadium":
        return f'{node_id}(["{clean}"])'
    if shape == "cylinder":
        return f'{node_id}[("{clean}")]'
    return f'{node_id}["{clean}"]'


def _safe_id(s: str) -> str:
    """mermaid 안전한 노드 id."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", s or "")
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "n_" + s
    return s or "n"


def drawio_to_mermaid(drawio_xml: str, diagram_name: str = "") -> str:
    """단순 flowchart 로 변환. 보존 손실은 헤더 주석에 명시."""
    cells = _parse_mxcells(drawio_xml)
    vertices = [c for c in cells if c.vertex]
    edges = [c for c in cells if c.edge]

    if not vertices:
        return (
            f"%% AUTO-GENERATED from {diagram_name or 'drawio'}.\n"
            f"%% drawio 안에서 vertex 를 발견하지 못했습니다.\n"
            f"%% 원본은 ../drawio/{diagram_name} 참조.\n"
            "flowchart TD\n"
        )

    lines: list[str] = []
    lines.append(f"%% AUTO-GENERATED from {diagram_name or 'drawio'}.")
    lines.append("%% drawio → mermaid 변환은 lossy 합니다.")
    lines.append(f"%% 원본 편집은 drawio/{diagram_name} 또는")
    lines.append(f"%% diagrams.net 에서 진행하고, 푸시는 push_diagram.py 사용.")
    lines.append("flowchart TD")

    # 노드 id 는 라벨 기반이면 노이즈가 많아짐. 짧은 일관 id (n1, n2...) 를 부여하되
    # 라벨에서 의미있는 영숫자 prefix 가 있으면 그걸 활용한다.
    id_map: dict[str, str] = {}
    used: set[str] = set()
    for i, c in enumerate(vertices, 1):
        clean = _clean_label(c.value)
        prefix = _safe_id(clean)[:24] if clean else ""
        if prefix and prefix not in ("n", "n_"):
            cand = f"{prefix}"
            suf = 2
            while cand in used:
                cand = f"{prefix}_{suf}"
                suf += 1
            nid = cand
        else:
            nid = f"n{i}"
            suf = 2
            while nid in used:
                nid = f"n{i}_{suf}"
                suf += 1
        used.add(nid)
        id_map[c.id] = nid
        shape = _shape_for_style(c.style)
        lines.append(f"    {_mmd_node(nid, c.value, shape)}")

    for e in edges:
        if not e.source or not e.target:
            continue
        s = id_map.get(e.source)
        t = id_map.get(e.target)
        if not s or not t:
            continue
        label = _clean_label(e.value)
        if label:
            label_e = label.replace("|", "/").replace('"', "'")
            lines.append(f"    {s} -->|{label_e}| {t}")
        else:
            lines.append(f"    {s} --> {t}")

    return "\n".join(lines) + "\n"


# ─── 3) mermaid (flowchart) → drawio mxfile XML ──────────────────────

_MMD_NODE_RE = re.compile(
    r"""
    (?P<id>[A-Za-z][A-Za-z0-9_]*)\s*
    (?:
        \[\s*"?(?P<rect>[^\]"]+?)"?\s*\]                  # [text] / ["text"]
      | \(\(\s*"?(?P<round>[^\)"]+?)"?\s*\)\)             # ((text))
      | \(\s*"?(?P<paren>[^\)"]+?)"?\s*\)                 # (text)
      | \[\(\s*"?(?P<cyl>[^\)"]+?)"?\s*\)\]               # [(text)]
      | \{\s*"?(?P<diam>[^\}"]+?)"?\s*\}                  # {text}
      | \[\s*\/\s*"?(?P<para>[^\/"]+?)"?\s*\/\s*\]        # [/text/]
    )
    """,
    re.VERBOSE,
)
_MMD_EDGE_RE = re.compile(
    r"""
    (?P<src>[A-Za-z][A-Za-z0-9_]*)\s*
    (?:-->|---|--)
    (?:\|\s*(?P<label>[^|]+?)\s*\|)?\s*
    (?P<dst>[A-Za-z][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


def _xml_attr_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;")
             .replace("\n", "&#10;"))


def mermaid_to_drawio(mmd_text: str) -> str:
    """단순 flowchart 만 처리. 자동 격자 레이아웃."""
    nodes: dict[str, dict] = {}  # id -> {label, shape}
    edges: list[tuple[str, str, str]] = []  # (src, dst, label)

    for line in mmd_text.splitlines():
        line = line.strip()
        if not line or line.startswith("%%") or line.startswith("flowchart"):
            continue
        # edge
        em = _MMD_EDGE_RE.search(line)
        if em:
            edges.append((em.group("src"), em.group("dst"), (em.group("label") or "").strip()))
            # 양 끝이 노드 정의도 겸할 수 있으므로 노드 검색도 계속 진행
        # node 정의 (한 줄에 노드 정의 + 엣지 가능)
        for nm in _MMD_NODE_RE.finditer(line):
            nid = nm.group("id")
            label = (nm.group("rect") or nm.group("round")
                     or nm.group("paren") or nm.group("cyl")
                     or nm.group("diam") or nm.group("para"))
            if nm.group("diam"):
                shape = "rhombus"
            elif nm.group("round") or nm.group("paren"):
                shape = "ellipse"
            elif nm.group("cyl"):
                shape = "cylinder"
            else:
                shape = "rect"
            nodes.setdefault(nid, {"label": label or nid, "shape": shape})
        # edge 양 끝의 노드를 정의 없이 등장하는 경우
        if em:
            for nid in (em.group("src"), em.group("dst")):
                nodes.setdefault(nid, {"label": nid, "shape": "rect"})

    # 자동 레이아웃: 격자
    ids = list(nodes.keys())
    cols = max(1, int(len(ids) ** 0.5 + 0.5))
    cell_w, cell_h = 160, 80
    gap_x, gap_y = 40, 60
    pos: dict[str, tuple[int, int]] = {}
    for idx, nid in enumerate(ids):
        cx = idx % cols
        cy = idx // cols
        x = cx * (cell_w + gap_x) + 40
        y = cy * (cell_h + gap_y) + 40
        pos[nid] = (x, y)

    parts: list[str] = []
    parts.append('<mxfile host="confluence-sync" version="24.0.0">')
    parts.append('  <diagram id="auto" name="페이지-1">')
    parts.append('    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" '
                 'guides="1" tooltips="1" connect="1" arrows="1" fold="1" '
                 'page="1" pageScale="1" pageWidth="850" pageHeight="1100" '
                 'math="0" shadow="0">')
    parts.append("      <root>")
    parts.append('        <mxCell id="0" />')
    parts.append('        <mxCell id="1" parent="0" />')

    cell_id_map: dict[str, str] = {}
    next_id = 2
    for nid in ids:
        cid = str(next_id); next_id += 1
        cell_id_map[nid] = cid
        x, y = pos[nid]
        n = nodes[nid]
        if n["shape"] == "rhombus":
            style = "rhombus;whiteSpace=wrap;html=1;"
        elif n["shape"] == "ellipse":
            style = "ellipse;whiteSpace=wrap;html=1;"
        elif n["shape"] == "cylinder":
            style = "shape=cylinder3;whiteSpace=wrap;html=1;"
        else:
            style = "rounded=0;whiteSpace=wrap;html=1;"
        label_e = _xml_attr_escape(n["label"])
        parts.append(
            f'        <mxCell id="{cid}" value="{label_e}" '
            f'style="{style}" vertex="1" parent="1">'
        )
        parts.append(
            f'          <mxGeometry x="{x}" y="{y}" '
            f'width="{cell_w}" height="{cell_h}" as="geometry" />'
        )
        parts.append("        </mxCell>")

    for src, dst, label in edges:
        sid = cell_id_map.get(src)
        tid = cell_id_map.get(dst)
        if not sid or not tid:
            continue
        cid = str(next_id); next_id += 1
        label_e = _xml_attr_escape(label) if label else ""
        parts.append(
            f'        <mxCell id="{cid}" value="{label_e}" '
            f'style="endArrow=classic;html=1;" edge="1" parent="1" '
            f'source="{sid}" target="{tid}">'
        )
        parts.append(
            '          <mxGeometry relative="1" as="geometry" />'
        )
        parts.append("        </mxCell>")

    parts.append("      </root>")
    parts.append("    </mxGraphModel>")
    parts.append("  </diagram>")
    parts.append("</mxfile>")
    return "\n".join(parts) + "\n"


# ─── 4) 파일 헬퍼 ───────────────────────────────────────────────────

def find_drawio_source(attach_dir: Path, diagram_name: str) -> Path | None:
    """페이지 첨부 디렉토리에서 diagram 의 본체 (mxfile) 파일을 찾는다.

    drawio 본체는 다음 형태로 저장됨:
      - "<name>"                (확장자 없음, inc-drawio)
      - "<name>.drawio"         (drawio)
    프리뷰 png 와 임시 ~tmp 는 제외한다.
    """
    if not attach_dir.exists():
        return None

    candidates: list[Path] = []
    # 1) 정확한 이름
    direct = attach_dir / diagram_name
    if direct.exists() and direct.is_file():
        candidates.append(direct)
    # 2) 확장자 없는 베이스명 (inc-drawio 의 경우)
    if not diagram_name.endswith(".drawio"):
        # inc-drawio 는 보통 확장자 없이 저장됨 — direct 가 그것
        pass
    else:
        # .drawio 케이스 — direct 가 그것
        pass

    # mxfile 인지 검증 → 첫 줄 확인
    for p in candidates:
        try:
            head = p.read_bytes()[:200]
            if b"<mxfile" in head or b"<mxGraphModel" in head:
                return p
        except Exception:
            continue
    return None


def find_drawio_preview(attach_dir: Path, diagram_name: str) -> Path | None:
    """diagram 의 PNG 프리뷰 파일.

    drawio 첨부 패턴:
      - <base>.png            — 종종 88B placeholder
      - <base>-<hash>.png     — 실제 렌더링된 PNG
      - <name>.png            — drawio 원본이 .drawio 로 끝날 때
    크기가 가장 큰 것을 진짜 프리뷰로 선택한다.
    """
    if not attach_dir.exists():
        return None
    base = diagram_name
    if base.endswith(".drawio"):
        base = base[: -len(".drawio")]

    candidates: list[Path] = []
    # 정확 매치
    for c in (attach_dir / f"{base}.png", attach_dir / f"{diagram_name}.png"):
        if c.exists() and c.is_file():
            candidates.append(c)
    # 해시 접미사
    for p in attach_dir.glob(f"{base}-*.png"):
        candidates.append(p)
    if base != diagram_name:
        for p in attach_dir.glob(f"{diagram_name}-*.png"):
            candidates.append(p)

    if not candidates:
        return None
    # 동일한 파일이 중복으로 들어갈 수 있음 → 경로로 dedupe
    seen, uniq = set(), []
    for c in candidates:
        rp = c.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(c)
    # 100B 미만은 placeholder 로 간주하고 후보에서 제외 (모두 placeholder 면 fallback)
    real = [c for c in uniq if c.stat().st_size > 200]
    pool = real or uniq
    return max(pool, key=lambda p: p.stat().st_size)
