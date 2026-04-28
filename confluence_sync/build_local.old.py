#!/usr/bin/env python3
"""Confluence 페이지 구조를 로컬에 마크다운 파일로 생성"""
import os, json, re, datetime
from pathlib import Path
from html.parser import HTMLParser

SCRIPT_DIR = Path(__file__).parent.resolve()
CONTENT_ROOT = SCRIPT_DIR.parent / "confluence_content"

class H2M(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out=[];self.ls=[];self.lc=[];self.hl=0;self.ht="";self.sk=0
        self.ins=False;self.ine=False;self.inc=False;self.inl=False;self.lh="";self.lt=""
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
    def handle_data(self,d):
        if self.sk>0:return
        c=d.replace('\u200d','').replace('\u200b','')
        if self.hl>0:self.ht+=c
        elif self.inl:self.lt+=c
        else:self.out.append(c)
    def handle_entityref(self,n):
        e={'amp':'&','lt':'<','gt':'>','quot':'"','zwj':''}
        c=e.get(n,f'&{n};')
        if self.hl>0:self.ht+=c
        elif self.inl:self.lt+=c
        else:self.out.append(c)
    def md(self):
        t=''.join(self.out);return re.sub(r'\n{3,}','\n\n',t).strip()

def h2m(html):
    if not html or html.strip() in('','<p>&zwj;</p>'):return ""
    p=H2M();p.feed(html);return p.md()

def mkpage(pid,title,lpath,html,ver=1,is_folder=False):
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

# 페이지 데이터 (Confluence에서 가져온 콘텐츠)
PAGES = {
    "40829168": {"t":"최종2팀","p":"최종2팀","f":True,"v":1,"h":'<h2>Description</h2><p>In a sentence or two, describe the purpose of this space.</p><h2>Project Tracker</h2><h2>Recently updated content</h2><h2>Contributors</h2>'},
    "40992771": {"t":"Meeting Notes","p":"최종2팀/Meeting Notes","f":True,"v":1,"h":""},
    "40992828": {"t":"Standup Meeting","p":"최종2팀/Meeting Notes/Standup Meeting","f":True,"v":1,"h":""},
    "41091178": {"t":"26-xx-xx Stand Up Meeting","p":"최종2팀/Meeting Notes/Standup Meeting/26-xx-xx Stand Up Meeting","f":False,"v":1,"h":""},
    "41517330": {"t":"26-04-24 Stand Up Meeting","p":"최종2팀/Meeting Notes/Standup Meeting/26-04-24 Stand Up Meeting","f":False,"v":1,"h":""},
    "43548673": {"t":"26-04-27 Stand Up Meeting","p":"최종2팀/Meeting Notes/Standup Meeting/26-04-27 Stand Up Meeting","f":False,"v":1,"h":""},
    "41058309": {"t":"Sprint Meeting","p":"최종2팀/Meeting Notes/Sprint Meeting","f":True,"v":1,"h":""},
    "41189582": {"t":"Sprint #1","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #1","f":False,"v":1,"h":""},
    "42041345": {"t":"Sprint #2","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #2","f":False,"v":1,"h":""},
    "41615614": {"t":"Sprint #3","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #3","f":False,"v":1,"h":""},
    "42041354": {"t":"Sprint #4","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #4","f":False,"v":1,"h":""},
    "42041363": {"t":"Sprint #5","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #5","f":False,"v":1,"h":""},
    "41615623": {"t":"Sprint #6","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #6","f":False,"v":1,"h":""},
    "42041372": {"t":"Sprint #7","p":"최종2팀/Meeting Notes/Sprint Meeting/Sprint #7","f":False,"v":1,"h":""},
    "40796182": {"t":"Mentoring","p":"최종2팀/Meeting Notes/Mentoring","f":True,"v":1,"h":""},
    "41189454": {"t":"26-xx-xx Mentoring","p":"최종2팀/Meeting Notes/Mentoring/26-xx-xx Mentoring","f":False,"v":1,"h":""},
    "42467329": {"t":"26-04-24-2 Mentoring","p":"최종2팀/Meeting Notes/Mentoring/26-04-24-2 Mentoring","f":False,"v":1,"h":""},
    "40927234": {"t":"Concepts","p":"최종2팀/Concepts","f":True,"v":1,"h":""},
    "41156729": {"t":"About Project","p":"최종2팀/Concepts/About Project","f":False,"v":1,"h":'<h1>사용가능한 로봇</h1><p>Vic Pinky</p><p><a href="https://github.com/pinklab-art/vic_pinky">https://github.com/pinklab-art/vic_pinky</a></p>'},
    "41222230": {"t":"프로젝트 주제 선정 회의","p":"최종2팀/Concepts/프로젝트 주제 선정 회의","f":False,"v":1,"h":""},
    "41156610": {"t":"Design","p":"최종2팀/Design","f":True,"v":1,"h":""},
    "41058328": {"t":"User Requirements","p":"최종2팀/Design/User Requirements","f":False,"v":1,"h":""},
    "40763394": {"t":"Directory Structure","p":"최종2팀/Design/Directory Structure","f":False,"v":1,"h":""},
    "40763414": {"t":"System Requirements","p":"최종2팀/Design/System Requirements","f":False,"v":1,"h":""},
    "40796220": {"t":"ERD","p":"최종2팀/Design/ERD","f":False,"v":1,"h":""},
    "40927253": {"t":"Interface Specification","p":"최종2팀/Design/Interface Specification","f":False,"v":1,"h":""},
    "40927273": {"t":"Map","p":"최종2팀/Design/Map","f":False,"v":1,"h":""},
    "40927292": {"t":"State Diagram","p":"최종2팀/Design/State Diagram","f":False,"v":1,"h":""},
    "40992866": {"t":"Sequence Diagram","p":"최종2팀/Design/Sequence Diagram","f":False,"v":1,"h":""},
    "41189397": {"t":"GUI","p":"최종2팀/Design/GUI","f":False,"v":1,"h":""},
    "41189416": {"t":"System Architecture","p":"최종2팀/Design/System Architecture","f":False,"v":1,"h":""},
    "40829224": {"t":"Implementation","p":"최종2팀/Implementation","f":True,"v":1,"h":""},
    "40960005": {"t":"Basic Project Setup","p":"최종2팀/Implementation/Basic Project Setup","f":False,"v":1,"h":""},
    "40992790": {"t":"Simulation","p":"최종2팀/Implementation/Simulation","f":False,"v":1,"h":""},
    "41189378": {"t":"Tech Research","p":"최종2팀/Implementation/Tech Research","f":True,"v":1,"h":""},
    "42893322": {"t":"3D 뎁스 카메라 (Depth Camera)","p":"최종2팀/Implementation/Tech Research/3D 뎁스 카메라 (Depth Camera)","f":True,"v":1,"h":""},
    "42827821": {"t":"듀얼 웹캠을 이용한 뎁스 카메라 (Stereo Vision) 구현 가이드","p":"최종2팀/Implementation/Tech Research/3D 뎁스 카메라 (Depth Camera)/듀얼 웹캠을 이용한 뎁스 카메라 (Stereo Vision) 구현 가이드","f":False,"v":2,"h":"DEPTH_CAMERA_PLACEHOLDER"},
    "42893385": {"t":"모방학습 논문 조사 리스트","p":"최종2팀/Implementation/Tech Research/모방학습 논문 조사 리스트","f":True,"v":1,"h":""},
    "42827861": {"t":"π0.6 논문 리뷰 Experience-aware VLA","p":"최종2팀/Implementation/Tech Research/모방학습 논문 조사 리스트/π0.6 논문 리뷰 Experience-aware VLA","f":False,"v":1,"h":""},
    "42893411": {"t":"상호작용 모방 학습 (IIL) 및 RLIF 기술조사 (이정우)","p":"최종2팀/Implementation/Tech Research/모방학습 논문 조사 리스트/상호작용 모방 학습 (IIL) 및 RLIF 기술조사 (이정우)","f":False,"v":1,"h":""},
    "43122698": {"t":"ACT 구조와 성능 분석 논문 리뷰","p":"최종2팀/Implementation/Tech Research/모방학습 논문 조사 리스트/ACT 구조와 성능 분석 논문 리뷰","f":False,"v":1,"h":""},
    "43220993": {"t":"VLA 의 역사와 발전사에 관한 개요","p":"최종2팀/Implementation/Tech Research/모방학습 논문 조사 리스트/VLA 의 역사와 발전사에 관한 개요","f":False,"v":1,"h":'<p><a href="https://woolimi.github.io/ko/blog/AI/VLA/intro">https://woolimi.github.io/ko/blog/AI/VLA/intro</a></p>'},
    "41582628": {"t":"OMX (OpenMANIPULATOR-X) 로 LeRobot 양팔 학습하기","p":"최종2팀/Implementation/Tech Research/OMX (OpenMANIPULATOR-X) 로 LeRobot 양팔 학습하기","f":False,"v":1,"h":""},
    "40796163": {"t":"Validation","p":"최종2팀/Validation","f":True,"v":1,"h":""},
    "41123852": {"t":"Presentation","p":"최종2팀/Presentation","f":True,"v":1,"h":""},
    "41156630": {"t":"Final Report","p":"최종2팀/Final Report","f":True,"v":1,"h":""},
    "41222145": {"t":"📝 테스트 및 검증","p":"최종2팀/📝 테스트 및 검증","f":True,"v":1,"h":""},
    "40927460": {"t":"Sprint X Test Plan","p":"최종2팀/📝 테스트 및 검증/Sprint X Test Plan","f":False,"v":1,"h":""},
    "41123943": {"t":"Sprint X Test Report","p":"최종2팀/📝 테스트 및 검증/Sprint X Test Report","f":False,"v":1,"h":""},
}

# 듀얼 웹캠 페이지 전체 콘텐츠
DEPTH_CAMERA_HTML = """<p>듀얼 웹캠으로 뎁스 카메라 구현은 가능하나, 한계 / 단점으로 인해 가능은 하나 비추천합니다.</p>
<h1>듀얼 웹캠을 이용한 뎁스 카메라 (Stereo Vision) 구현 가이드</h1>
<h2>1. 구현 원리 (Principles)</h2>
<p>두 개의 일반 웹캠으로 뎁스(깊이) 카메라를 구현하는 기술을 <strong>스테레오 비전(Stereo Vision)</strong>이라고 합니다. 이는 사람의 두 눈이 거리를 인지하는 방식을 그대로 모방한 것입니다.</p>
<ul>
<li><p><strong>에피폴라 기하학 (Epipolar Geometry):</strong> 두 카메라의 시점 차이를 기하학적으로 모델링하여, 한 이미지의 특정 점이 다른 이미지의 어느 '선(Epipolar Line)' 위에 존재하는지 수치화합니다.</p></li>
<li><p><strong>시차 (Disparity):</strong> 동일한 물체가 왼쪽 카메라 영상과 오른쪽 카메라 영상에서 나타나는 위치(픽셀)의 차이를 의미합니다. 물체가 가까울수록 두 영상 간의 위치 차이가 크고, 멀수록 위치 차이가 작아집니다.</p></li>
<li><p><strong>삼각 측량 (Triangulation):</strong> 두 카메라의 초점 거리(Focal length), 카메라 간의 물리적 거리(Baseline), 그리고 계산된 시차(Disparity)를 이용해 삼각함수 비례식으로 실제 3D 거리(Z값)를 계산해냅니다.</p></li>
</ul>
<h2>2. 필요 항목 및 개발 환경 (Requirements)</h2>
<ul>
<li><p><strong>하드웨어:</strong></p>
<ul>
<li><p>동일한 모델의 웹캠 2개 (해상도와 화각이 완전히 같아야 유리함)</p></li>
<li><p>두 웹캠을 흔들림 없이 수평으로 고정할 수 있는 <strong>단단한 지지대</strong> (Rig, 아크릴 판, 3D 프린팅 거치대 등)</p></li>
<li><p>체스보드 패턴 인쇄물 (카메라 캘리브레이션, 즉 영점 조절용)</p></li>
</ul></li>
<li><p><strong>소프트웨어:</strong></p>
<ul>
<li><p>프로그래밍 언어: Python 또는 C++</p></li>
<li><p>핵심 라이브러리: <strong>OpenCV</strong> (오픈소스 컴퓨터 비전 라이브러리), NumPy</p></li>
</ul></li>
</ul>
<h2>3. 구현 순서 (Implementation Steps)</h2>
<h3>Step 1: 하드웨어 세팅 및 영상 동기화</h3>
<ul>
<li><p>두 웹캠을 나란히 수평으로 고정합니다. 두 렌즈 중앙 사이의 거리를 베이스라인(Baseline)이라고 하며, 6~10cm 정도로 설정하는 것이 일반적입니다.</p></li>
<li><p>OpenCV의 <code>cv2.VideoCapture</code>를 이용해 두 카메라의 영상을 동시에 스트리밍하여 컴퓨터로 불러옵니다.</p></li>
</ul>
<h3>Step 2: 단일 카메라 캘리브레이션 (Camera Calibration)</h3>
<ul>
<li><p>저가형 웹캠 렌즈의 볼록한 왜곡(Distortion)을 평평하게 펴고, 각 렌즈의 고유한 내부 파라미터(초점 거리 등)를 구하는 과정입니다.</p></li>
<li><p>다양한 각도에서 체스보드 종이를 두 카메라로 촬영한 뒤, <code>cv2.calibrateCamera</code> 함수를 사용하여 내부 파라미터 행렬을 계산합니다.</p></li>
</ul>
<h3>Step 3: 스테레오 캘리브레이션 (Stereo Calibration)</h3>
<ul>
<li><p>왼쪽 카메라와 오른쪽 카메라 사이의 상대적인 물리적 위치 관계(회전 행렬, 병진 이동 벡터)를 정확히 알아냅니다.</p></li>
<li><p><code>cv2.stereoCalibrate</code> 함수를 사용합니다.</p></li>
</ul>
<h3>Step 4: 스테레오 평행화 (Image Rectification)</h3>
<ul>
<li><p>캘리브레이션 결과를 바탕으로, 두 카메라의 영상 높이(Y축)가 완벽히 일치하도록 이미지를 기하학적으로 변형시킵니다.</p></li>
<li><p>이 과정을 거치면 왼쪽 이미지의 픽셀을 오른쪽 이미지에서 찾을 때, <strong>같은 가로줄(수평선) 위에서만 탐색</strong>하면 되므로 연산 속도와 정확도가 매우 높아집니다. (<code>cv2.stereoRectify</code> 사용)</p></li>
</ul>
<h3>Step 5: 스테레오 매칭 및 시차 맵(Disparity Map) 생성</h3>
<ul>
<li><p>평행화된 두 이미지에서 동일한 픽셀 패턴(특징점)을 찾아 매칭합니다.</p></li>
<li><p>왼쪽과 오른쪽의 픽셀 위치 차이(Disparity)를 계산하여 흑백 이미지 형태의 <strong>시차 맵(Disparity Map)</strong>으로 표현합니다.</p></li>
<li><p>OpenCV 추천 알고리즘: <strong>StereoSGBM</strong> (Semi-Global Block Matching)</p></li>
</ul>
<h3>Step 6: 3D 깊이 맵(Point Cloud) 생성</h3>
<ul>
<li><p>얻어낸 시차 맵과 Q 매트릭스를 이용해 픽셀 단위로 실제 X, Y, Z 물리적 좌표(거리)를 계산해냅니다. (<code>cv2.reprojectImageTo3D</code> 함수 사용)</p></li>
</ul>
<h2>4. 듀얼 웹캠 방식의 한계 및 단점 (Limitations)</h2>
<p>일반 웹캠 두 대를 소프트웨어적으로 합쳐 스테레오 비전을 구현할 경우, 전용 3D 뎁스 카메라에 비해 다음과 같은 뚜렷한 한계가 존재합니다.</p>
<h3>1) 높은 시스템 자원 사용률</h3>
<ul>
<li><p><strong>심각한 연산 부하:</strong> 두 개의 영상 스트림을 실시간으로 가져와 왜곡을 펴고, 이미지 내 거의 모든 픽셀의 패턴을 비교하여 매칭하는 과정은 CPU 및 GPU 자원을 매우 심하게 소모합니다.</p></li>
<li><p><strong>프레임 속도(FPS) 저하:</strong> 하드웨어 가속이나 해상도 타협 등의 최적화 작업이 없다면 실시간 반응이 필수적인 자율주행이나 로봇 제어에 활용하기 어렵습니다.</p></li>
</ul>
<h3>2) 영상 깨짐 및 노이즈 발생</h3>
<ul>
<li><p><strong>특징점 매칭 실패:</strong> 질감이 뚜렷하지 않은 하얀 벽면이나 유리, 반복적인 패턴이 있는 표면에서는 좌우 픽셀 짝을 찾지 못합니다.</p></li>
<li><p><strong>하드웨어 비동기화:</strong> 일반 USB 웹캠 2대는 캡처 타이밍이 미세하게 엇갈리므로, 빠르게 움직이는 물체를 촬영하면 심각한 깊이 왜곡이 발생합니다.</p></li>
</ul>
<h3>3) 캘리브레이션 유지의 취약성</h3>
<ul>
<li><p>일반 웹캠은 단단한 일체형 하우징으로 묶여있지 않아, 약간의 충격이나 진동에도 두 카메라의 각도가 미세하게 틀어집니다.</p></li>
</ul>
<h2>5. 참고 영상 및 튜토리얼 링크 (References &amp; Tutorials)</h2>
<ul>
<li><p><a href="https://youtu.be/k_QSqbj_bYo">YouTube: 2개의 웹캠으로 뎁스 카메라 구현 - python</a></p></li>
<li><p><a href="https://www.youtube.com/watch?v=h_SxE2UWeE8">YouTube: OpenCV Stereo Vision Tutorial - Python</a></p></li>
<li><p><a href="https://www.youtube.com/watch?v=yIEizS0v4wQ">YouTube: Real-time Depth Map from Stereo Webcams</a></p></li>
<li><p><a href="https://docs.opencv.org/4.x/dd/d53/tutorial_py_depthmap.html">OpenCV 공식 문서: Depth Map from Stereo Images</a></p></li>
</ul>"""

def main():
    print("\n🚀 Confluence 로컬 구조 생성 시작...\n")
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    
    for pid, info in PAGES.items():
        html = info["h"]
        if html == "DEPTH_CAMERA_PLACEHOLDER":
            html = DEPTH_CAMERA_HTML
        mkpage(pid, info["t"], info["p"], html, info["v"], info["f"])
    
    # 메타 디렉토리 생성
    meta_dir = SCRIPT_DIR / ".page_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    for pid, info in PAGES.items():
        meta = {"page_id":pid,"version":info["v"],"title":info["t"],
                "local_path":info["p"],"is_folder":info["f"]}
        with open(meta_dir/f"{pid}.json",'w',encoding='utf-8') as f:
            json.dump(meta,f,indent=2,ensure_ascii=False)
    
    print(f"\n✅ 총 {len(PAGES)}개 페이지 로컬에 생성 완료!")
    print(f"📂 콘텐츠 루트: {CONTENT_ROOT}\n")

if __name__=='__main__':
    main()
