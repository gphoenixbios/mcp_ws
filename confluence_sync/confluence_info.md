# Confluence Sync 모듈

Atlassian Confluence Cloud의 스페이스 전체를 REST API v2로 가져와 로컬 HTML / `.md` 스냅샷으로 관리하는 모듈. 첨부파일까지 함께 동기화.

## 구조

```
confluence_sync/
  sync_config.json            # base_url, space_key, space_id, homepage_id, page_tree
  .env                        # ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN
  pull_space.py               # /wiki/api/v2/spaces, /pages → 트리 + storage HTML
  download_attachments.py     # /wiki/api/v2/pages/{id}/attachments → 첨부 다운로드
  build_html_md.py            # storage HTML → 브라우저용 HTML + 마크다운 빌드
  confluence_sync.py          # push (로컬 → Confluence), status, tree 등 보조 명령
  drawio_utils.py             # draw.io 매크로 → mermaid 변환 헬퍼
  raw_html/<page_id>.json     # 페이지 원본 storage HTML 캐시 (51 파일)
  raw_attachments/<page_id>/  # 첨부파일 원본

confluence_content/최종2팀/
  html/                       # 브라우저로 보는 진입점 (index.html + 페이지 트리)
  md/                         # 마크다운 진입점 (index.md + 페이지 트리)
```

## 초기 설정 (.env 만들기)

이미 `jira_sync/.env`가 있다면 폴백으로 자동 사용되므로 **건너뛰어도 됩니다**.
독립적으로 또는 별도 토큰을 쓰고 싶을 때만 다음을 수행:

1. **API 토큰 발급** — https://id.atlassian.com/manage-profile/security/api-tokens 접속 → "Create API token"
2. **`.env` 파일 생성**:
   ```bash
   cd confluence_sync
   cp .env.example .env
   ```
3. **`.env` 편집** — 다음 두 값 채우기:
   ```
   ATLASSIAN_EMAIL=본인이메일@example.com
   ATLASSIAN_API_TOKEN=ATATT3xFfGF0... (방금 발급한 토큰)
   ```
4. `.env`는 [.gitignore](.gitignore)에 의해 git 추적 제외됨 (커밋되지 않음).

## 현재 동기화 대상

진입점:
- HTML: [confluence_content/최종2팀/html/index.html](confluence_content/최종2팀/html/index.html)
- MD: [confluence_content/최종2팀/md/index.md](confluence_content/최종2팀/md/index.md)

- 인스턴스: `https://woolimi.atlassian.net`
- 스페이스: `FN` (최종2팀, space_id=40828932, homepage_id=40829168)
- 페이지: 51개 (Validation / Implementation / Concepts / Meeting Notes / Design / Presentation / Final Report / 테스트 및 검증 등)
- 첨부파일: 9개 신규 다운로드

## 핵심 디자인 포인트

- **인증**: `.env`의 `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN` (Atlassian Cloud API 토큰). `confluence_sync/.env`가 없으면 `jira_sync/.env`로 자동 폴백 — Jira 모듈과 양방향으로 토큰 공유.
- **3단계 파이프라인**:
  1. `pull_space.py` — 스페이스 정보·페이지 트리·storage HTML 수집 → `sync_config.json` + `raw_html/`
  2. `download_attachments.py` — 페이지별 첨부파일 다운로드 → `raw_attachments/<page_id>/`
  3. `build_html_md.py` — 캐시된 HTML/첨부 → 최종 HTML/MD 산출물 빌드
- **storage format → MD**: Confluence의 `ac:*` 매크로 태그를 인식하는 `ConfluenceHTMLToMarkdown` 파서 내장 (`confluence_sync.py`).
- **draw.io 통합**: `drawio_utils.py`가 draw.io 매크로를 mermaid로 변환 시도하고, 원본 미리보기는 그대로 첨부 처리.
- **양방향 sync**: `confluence_sync.py push <page_id>`로 로컬 마크다운 변경분을 Confluence에 업로드 가능 (각 페이지에 frontmatter로 `confluence_page_id` / `confluence_version` 저장).
- **타겟 전환 안전성**: `sync_config.json`의 `_last_synced_target` 핑거프린트(`<base_url>|<space_key>`)와 현재 타겟을 매 pull 시 비교 → 다르면 `raw_html/`, `raw_attachments/` 자동 정리 후 fresh pull. 이전 인스턴스의 page_id 캐시가 새 스페이스와 섞이는 사고 방지. 자세한 사용법은 아래 "URL/타겟 전환 방법" 참조.

## 재실행 방법

```bash
cd confluence_sync
python pull_space.py            # 트리 + HTML 갱신
python download_attachments.py  # 첨부 동기화
python build_html_md.py         # HTML/MD 빌드
```

상태 확인 / 트리 보기:
```bash
python confluence_sync.py status   # 로컬 변경 사항 확인
python confluence_sync.py tree     # 폴더 구조 출력
```

## URL/타겟 전환 방법

컨플로언스 URL 변경은 **딱 2단계**입니다.

### Step 1) `confluence_sync/sync_config.json` 편집

현재 상태:
```json
{
  "confluence_base_url": "https://woolimi.atlassian.net",
  "space_key": "FN",
  ...
}
```

바꾸고 싶은 2개 필드 (어떤 조합이든 가능):

| 필드 | 어디서 찾는가 |
|---|---|
| `confluence_base_url` | 브라우저 주소창의 `https://○○○.atlassian.net` 부분 |
| `space_key` | URL의 `/wiki/spaces/<여기>/` (예: `FN`, `ADP`) |

**예시: 다른 회사의 ABC 스페이스로 전환**
```json
{
  "confluence_base_url": "https://newcorp.atlassian.net",
  "space_key": "ABC",
  ...
}
```

> `space_id`, `space_name`, `homepage_id`, `page_tree`, `_last_synced_target` 같은 메타 필드는 **건드리지 마세요** — `pull_space.py`가 API 응답으로 자동 갱신합니다.

### Step 2) sync 실행

```bash
cd confluence_sync
python pull_space.py
python download_attachments.py
python build_html_md.py
```

### 자동으로 일어나는 일

`pull_space.py` 시작 화면 예시:
```
🎯 Target: https://newcorp.atlassian.net / space=ABC
📜 Last synced: https://woolimi.atlassian.net / space=FN
⚠ 타겟 변경 감지 → 이전 캐시 정리
   - 53 항목 삭제
```

→ 이전 인스턴스 데이터(`raw_html/*.json`, `raw_attachments/`, `.missing_attachments.json`)가 자동 정리되고, 새 타겟에서 fresh pull 진행.

**핑거프린트 형식**: `<base_url>|<space_key>` — 둘 중 하나라도 바뀌면 캐시 정리 트리거.

### 한 가지만 수동 작업

이전 빌드 산출물 폴더 `confluence_content/<이전 space_name>/`는 **안전상 자동 삭제하지 않습니다**. 필요 없으면 직접:
```bash
rm -rf confluence_content/최종2팀
```

### 토큰(.env)도 바꿔야 하는 경우

같은 Atlassian 계정 토큰이 다른 회사 인스턴스에 접근 권한이 없을 수 있습니다. 그땐 새 토큰 발급 후 `confluence_sync/.env`(없으면 `jira_sync/.env`)의 `ATLASSIAN_API_TOKEN`만 갱신.
토큰 발급: https://id.atlassian.com/manage-profile/security/api-tokens

---

**요약**: `sync_config.json` 2줄 수정 → `pull_space.py && download_attachments.py && build_html_md.py` 한 번 → 끝.
