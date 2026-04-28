# Jira Sync 모듈

Jira Cloud의 보드/스프린트/이슈를 직접 REST API로 가져와 로컬 `.md` 스냅샷으로 관리하는 모듈.

## 구조

```
jira_sync/
  sync_config.json       # base_url, project_key=FN, board_id=134
  .env                   # ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN (gitignore됨)
  .env.example           # 템플릿 (실제 토큰 없음)
  pull_jira.py           # /rest/agile/1.0/board, /sprint, /issue + /api/3/issue/{key}
  build_md.py            # JSON → MD 빌드 (HTMLToMarkdown 내장)
  raw_board.json
  raw_sprints.json
  raw_issues/<KEY>.json  # 13 파일 (이슈 원본 캐시)

jira_content/FN/
  index.md               # 개요·통계·스프린트 목록
  board.md               # 칸반 뷰 (To Do / In Progress / Done)
  sprints/<id>_<name>.md # 스프린트 7개
  issues/<KEY>.md        # 이슈 13개 (프런트매터 포함)
```

## 초기 설정 (.env 만들기)

이미 `confluence_sync/.env`가 있다면 폴백으로 자동 사용되므로 **건너뛰어도 됩니다**.
독립적으로 또는 별도 토큰을 쓰고 싶을 때만 다음을 수행:

1. **API 토큰 발급** — https://id.atlassian.com/manage-profile/security/api-tokens 접속 → "Create API token"
2. **`.env` 파일 생성**:
   ```bash
   cd jira_sync
   cp .env.example .env
   ```
3. **`.env` 편집** — 다음 두 값 채우기:
   ```
   ATLASSIAN_EMAIL=본인이메일@example.com
   ATLASSIAN_API_TOKEN=ATATT3xFfGF0... (방금 발급한 토큰)
   ```
4. `.env`는 [.gitignore](.gitignore)에 의해 git 추적 제외됨 (커밋되지 않음).

## 첫 스냅샷 요약

진입점: [jira_content/FN/index.md](jira_content/FN/index.md)

- 보드: `FN board` (simple / 칸반)
- 이슈: 13 (해야 할 일 12 / 진행 중 1)
- 스프린트: 7 (active 1 / future 6)
- 빠른 이동: [board.md](jira_content/FN/board.md), [issues/](jira_content/FN/issues/), [sprints/](jira_content/FN/sprints/)

## 핵심 디자인 포인트

- **인증**: `jira_sync/.env` 우선, 없으면 `confluence_sync/.env`로 자동 폴백 (반대도 동일) — 두 모듈이 동일 Atlassian API 토큰을 양방향으로 공유 (`ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`).
- **로케일 무관 컬럼 분류**: `statusCategory.key` (`new` / `indeterminate` / `done`) 기반으로 칸반 컬럼을 정렬 → 한국어("해야 할 일", "진행 중") 로케일에서도 정상 동작.
- **본문 변환**: 이슈 description / comments는 `expand=renderedFields`로 받은 HTML을 자체 `HTMLToMarkdown` 파서로 마크다운 변환.
- **스프린트 매핑**: 이슈의 `customfield_*` 중 `{id, name, state}` 모양의 배열을 자동 탐색 → Jira 인스턴스마다 다른 sprint 필드 ID를 하드코딩하지 않음.
- **이슈 프런트매터**: key, summary, status, status_category, issue_type, priority, assignee, reporter, created, updated, duedate, parent, labels, sprints, url 포함.
- **타겟 전환 안전성**: `sync_config.json`의 `_last_synced_target` 핑거프린트(`<base_url>|<project_key>|<board_id>`)와 현재 타겟을 매 pull 시 비교 → 다르면 raw 캐시 자동 정리 후 fresh pull. 이전 인스턴스 데이터가 새 타겟과 섞이는 사고 방지. 자세한 사용법은 아래 "URL/타겟 전환 방법" 참조.

## 재실행 방법

```bash
cd jira_sync
python pull_jira.py    # API → raw_*
python build_md.py     # raw_* → ../jira_content/FN/
```

## URL/타겟 전환 방법

지라 URL 변경은 **딱 2단계**입니다.

### Step 1) `jira_sync/sync_config.json` 편집

현재 상태:
```json
{
  "jira_base_url": "https://woolimi.atlassian.net",
  "project_key": "FN",
  "board_id": 134,
  ...
}
```

바꾸고 싶은 3개 필드 (어떤 조합이든 가능):

| 필드 | 어디서 찾는가 |
|---|---|
| `jira_base_url` | 브라우저 주소창의 `https://○○○.atlassian.net` 부분 |
| `project_key` | URL의 `/projects/<여기>/` (예: `FN`, `ABC`) |
| `board_id` | 보드 URL 끝의 숫자 (예: `/boards/134` → `134`) |

**예시: 다른 회사의 ABC 프로젝트 / 보드 200으로 전환**
```json
{
  "jira_base_url": "https://newcorp.atlassian.net",
  "project_key": "ABC",
  "board_id": 200,
  ...
}
```

> `board_name`, `board_type`, `stats`, `_last_synced_target` 같은 메타 필드는 **건드리지 마세요** — `pull_jira.py`가 자동으로 갱신합니다.

### Step 2) sync 실행

```bash
cd jira_sync
python pull_jira.py
python build_md.py
```

### 자동으로 일어나는 일

`pull_jira.py` 시작 화면 예시:
```
🎯 Target: https://newcorp.atlassian.net / project=ABC / board=200
📜 Last synced: https://woolimi.atlassian.net / project=FN / board=134
⚠ 타겟 변경 감지 → 이전 캐시 정리
   - 15 항목 삭제
```

→ 이전 인스턴스 데이터(`raw_board.json`, `raw_sprints.json`, `raw_issues/*`)가 자동 정리되고, 새 타겟에서 fresh pull 진행.

**핑거프린트 형식**: `<base_url>|<project_key>|<board_id>` — 셋 중 하나라도 바뀌면 캐시 정리 트리거.

### 한 가지만 수동 작업

이전 빌드 산출물 폴더 `jira_content/<이전 project_key>/`는 **안전상 자동 삭제하지 않습니다**. 필요 없으면 직접:
```bash
rm -rf jira_content/FN
```

### 토큰(.env)도 바꿔야 하는 경우

같은 Atlassian 계정 토큰이 다른 회사 인스턴스에 접근 권한이 없을 수 있습니다. 그땐 새 토큰 발급 후 `jira_sync/.env`(없으면 `confluence_sync/.env`)의 `ATLASSIAN_API_TOKEN`만 갱신.
토큰 발급: https://id.atlassian.com/manage-profile/security/api-tokens

---

**요약**: `sync_config.json` 3줄 수정 → `pull_jira.py && build_md.py` 한 번 → 끝.
