#!/usr/bin/env python3
"""원클릭 전체 동기화 (pull): Confluence 스페이스 → 로컬.

기존 3단계 스크립트를 순서대로 실행하는 얇은 래퍼입니다.
sync_config.json / .env 는 전혀 수정하지 않으며, 현재 설정된 대상을 그대로 받아옵니다.

  1) pull_space.py            트리 + storage HTML 수집  → raw_html/, sync_config.json
  2) download_attachments.py  첨부(이미지 포함) 다운로드 → raw_attachments/
  3) build_html_md.py         최종 HTML/MD 빌드          → ../confluence_content/

사용법:
  python sync_all.py                  # 전체(첨부 포함) 받기
  python sync_all.py --skip-attachments   # 첨부 건너뛰고 본문만
  python sync_all.py --missing            # 누락된 첨부만 재시도 + 재빌드
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script, *script_args):
    cmd = [sys.executable, str(HERE / script), *script_args]
    label = " ".join([script, *script_args])
    print(f"\n{'=' * 60}\n▶  {label}\n{'=' * 60}", flush=True)
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print(f"\n✖  '{label}' 단계 실패 (exit {result.returncode}). 중단합니다.", file=sys.stderr)
        sys.exit(result.returncode)
    print(f"✔  {label} 완료", flush=True)


def main():
    argv = sys.argv[1:]
    skip_attachments = "--skip-attachments" in argv
    missing_only = "--missing" in argv

    if missing_only:
        # 누락 첨부만 재시도 후 재빌드 (트리 재수집 생략)
        run("download_attachments.py", "--missing")
        run("build_html_md.py")
    else:
        run("pull_space.py")
        if not skip_attachments:
            run("download_attachments.py")
        else:
            print("\n(첨부 다운로드 건너뜀: --skip-attachments)", flush=True)
        run("build_html_md.py")

    print("\n🎉 전체 동기화 완료 → ../confluence_content/ 확인하세요.", flush=True)


if __name__ == "__main__":
    main()
