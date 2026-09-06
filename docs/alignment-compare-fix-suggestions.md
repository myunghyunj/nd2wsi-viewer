# nd2wsi-viewer alignment/compare 업데이트 명세

> **접수 메모 — 2026-09-06 / 문서 추가만 수행**
> 아래는 사용자가 제공한 후속 수정 제안이다. 항목 1–14, 공통 상태 계약, 비정방 pixel·scalebar 조건, 검증 gate를 원문대로 보존했다. `v1.2.6`은 제안 버전이며 확정된 릴리스 일정이 아니다.
> 이 문서 추가 과정에서 각 문제를 최신 코드로 재현하거나 수정하지 않았다. 원문의 문제 설명과 완료 기준은 검증·구현할 요구사항이지, 구현 완료 또는 새로 확인된 결함의 증거가 아니다.
> **진행 방식:** 원문의 새 branch·Draft PR 및 PR A–D 권고는 제출된 제안으로 보관하며, 사용자의 기존 직접 main 커밋·푸시 방침을 변경하지 않는다. 실제 기능 수정의 배포 절차는 [Release completion contract](releasing.md)와 사용자의 최신 명시적 지시를 따른다. 이번 요청은 제안 추가만이므로 코드 구현·버전 변경·태그·앱 빌드·Release·appcast·로컬 설치는 수행하지 않는다.

---

**검토 기준:** 앞서 확인한 `v1.2.5`, `e459ced7`  
**목표 버전:** `v1.2.6` 제안  
**상태:** 구현 요구사항 정리. 이번 작성에서 코드 변경이나 앱·테스트 재실행은 하지 않았음.

기존 1–14번 번호를 유지하고, 이후 보완한 취소 처리, RMS, landmark provenance, plate site ownership을 반영했다. 비정방 pixel과 scalebar는 번호를 추가하지 않고 **별도 필수 정확성 조건**으로 포함한다.

## 유지할 기능과 작업 범위

**6-well grid와 트랙패드 T/Z 탐색은 유지한다.** 이번 수정은 그 UX를 바꾸는 작업이 아니라, 사용자가 조작하는 대상과 실제 정렬 상태를 일치시키는 작업이다.

Python + stdlib server + pywebview + OpenSeadragon 구조를 유지한다. 원본 ND2/SVS, raw pixel, annotation의 source 좌표를 display transform 때문에 수정하지 않는다. Cache를 줄이거나 삭제하는 방식으로 성능을 조정하지 않는다.

모든 변경은 로컬 변경을 보존한 **새 branch + Draft PR**로 진행한다. 승인 전 merge, tag, release, appcast 게시를 하지 않는다.

---

## 공통 상태 계약

개별 함수마다 임시 조건문을 붙이기보다, 아래 공통 상태와 검증 경계를 먼저 정의한다.

### Identity 구분

| 필드 | 의미 | 갱신·검증 기준 |
|---|---|---|
| `groupSessionId` | 한 번의 Compare 세션 | Compare 새 시작 또는 shell 재시작 |
| `groupEpoch` | 공간 작업과 표시 명령의 유효기간 | 그룹·anchor·source/site 변경, Cancel, Compare 종료 |
| `editId` | 한 번의 landmark 편집 | Align 시작마다 새 값 |
| `editRevision` | 현재 draft 입력 버전 | 점 추가·이동·삭제, mirror 정책 변경 |
| `landmarkSetId` / `revision` | 대응 구조의 정체성과 좌표 버전 | 같은 점 세트를 사용한 fit인지 검사 |
| `spatialContextKey` | 정렬을 소유하는 영상 공간 | WSI는 slide, plate는 source generation과 P를 포함한 site |
| `requestId` / `commandSeq` | 비동기 작업 식별과 순서 | 취소된 요청과 오래된 표시 명령 거부 |
| `paneInstanceId` / `contextEpoch` | iframe 실행 인스턴스와 로컬 context 전환 | Reload 및 P1 → P2 → P1 같은 왕복 전환 보호 |

`spatialContextKey`는 **데이터 소유권**, runtime epoch는 **늦은 작업의 실행 방지**를 담당한다. 같은 P1으로 돌아왔다고 첫 번째 방문에서 대기하던 callback까지 다시 유효해져서는 안 된다.

Toolbox를 숨기는 것만으로는 epoch를 바꾸거나 pending action을 취소하지 않는다.

### Alignment 상태 분리

```text
Compare session
├── committedGroup
│   ├── anchorContext
│   ├── memberContexts
│   ├── anchorLandmarkSet
│   └── pairs
│       ├── manualOrientation
│       ├── fitTransform
│       ├── fitMeta
│       ├── manualOffset
│       ├── effectiveTransform
│       ├── currentResidual
│       └── fitProvenance
├── edit
│   ├── editId / editRevision
│   ├── baseCommittedRevision
│   ├── capturedContexts
│   ├── draftAnchorSet
│   ├── draftPairs
│   └── candidateFits
└── pendingTransaction
    ├── requestId / groupEpoch / editId
    ├── expectedTargets
    ├── expectedContexts
    └── responses
```

Draft는 committed 배열과 행렬을 공유 참조로 수정하지 않는다. Fit과 함께 **어느 영상·어느 대응점·어느 revision에서 계산했는지** 보존한다.

---

## 1. Landmark 삭제·fit 실패 후 stale fit 제거

**우선순위: 높음**  
**관련 함수:** `fitPair()`, `receiveLandmarkPoints()`, `undoLandmark()`

### 문제

점을 삭제해 fitting 조건을 잃거나 새 fitting이 실패해도 이전 `pair.fit`과 transform이 남을 수 있다. 현재 점은 1개인데 이전 4점 fit과 RMS를 유효한 결과처럼 표시할 수 있다. 

### 적용 내용

편집 중 점과 candidate fit을 committed 상태에서 분리한다. 점이나 fitting 정책이 변경되면 이전 candidate fit을 즉시 무효화하고, 새 결과에는 현재 `editId`, `editRevision`, landmark revisions와 context를 기록한다.

실패 상태는 단순 `false`가 아니라 `incomplete`, `degenerate`, `stale-context`처럼 원인을 구분한다.

마지막 committed transform을 계속 보여줄 수는 있지만 다음처럼 표시한다.

```text
Draft incomplete
Previous alignment shown
```

이전 RMS를 현재 draft의 RMS로 표시하지 않는다. `pair.fit = null`만 실행해 실제 transform의 유래와 UI를 분리하는 수정도 피한다.

### 완료 기준

```text
4점 fit → 점을 1개까지 삭제
→ candidate fit invalid
→ committed snapshot 유지
→ 이전 4점 fit을 현재 결과처럼 표시하지 않음
```

---

## 2. 모든 확정 경로에 공통 atomic commit gate 적용

**우선순위: 높음**  
**관련 함수:** `finishLandmarks()`, Done·Enter·Align 재클릭·`landmark-done` handlers

### 문제

Done 버튼 비활성화만으로는 다른 진입점을 막을 수 없다. 일부 pair가 미완성인데 다른 pair만 확정되는 부분 commit도 피해야 한다.

### 적용 내용

모든 확정 경로를 `commitLandmarkEdit(editId)` 하나로 연결한다. 이 함수가 직접 다음을 검증한다.

- 현재 Compare 세션, epoch, edit ID가 일치한다.
- 그룹 구성과 source/site context가 편집 대상과 일치한다.
- 편집을 변경할 pending Clear, orientation, capture, refit 작업이 없다.
- Candidate fit이 현재 landmark revisions와 mirror 정책으로 계산되었다.
- 모든 pair가 점 개수, geometry, renderer 지원 정책을 통과했다.
- 편집 중 committed revision이 다른 작업으로 교체되지 않았다.

처리는 다음 순서로 고정한다.

```text
전체 검증
→ 모든 pair의 next snapshot 구성
→ committed group 단일 교체
→ memory 저장
→ UI 갱신
```

검증과 교체 사이에 `await`를 두지 않는다. 단순 layout sync는 alignment mutation과 구별해 불필요하게 Done을 계속 막지 않게 한다.

### 완료 기준

B는 valid, C는 incomplete인 상태에서 Done·Enter·Align 재클릭·메시지를 각각 실행해도 **어떤 pair도 확정되지 않는다.** 거부 이유를 표시하고 편집은 유지한다.

---

## 3. Compare 종료 시 미확정 draft 자동 commit 금지

**우선순위: 높음**  
**관련 함수:** `stopCompare()`, `toggleCompare()`, tab/group teardown

### 문제

Align 중 Compare를 종료하는 경로가 `finishLandmarks(true)`를 호출하면 사용자가 Done을 누르지 않은 변경도 확정된다. 

### 적용 내용

| 사용자 동작 | 동작 계약 |
|---|---|
| Toolbox의 × | 도구만 숨김. Draft와 pending action 유지 |
| Done / 유효한 Enter | 공통 commit gate를 통과한 경우에만 확정 |
| Cancel / Esc | 편집 작업 무효화 후 committed 상태 복원 |
| Stop Compare / `Cmd+Backslash` | 미확정 draft 취소 후 Compare 종료 |
| 비교 대상 제거·교체, source/site 전환 | 현재 draft 무효화 후 context 전환 |

암묵적 종료 경로에서 `finishLandmarks(true)`를 호출하지 않는다. 종료 전에 pending spatial command를 무효화하고, 마지막 committed alignment만 보존한다.

### 완료 기준

정상 alignment → 일부 점 편집 → Compare 종료 → 같은 pair 재개 시 **마지막 committed 결과만** 복원된다. Toolbox 숨김·재열기는 편집 결과에 영향을 주지 않는다.

---

## 4. Mirror 정책과 landmark geometry 검증 수정

**우선순위: 높음**  
**관련 함수:** `fitSimilarity()`, `fitPair()`, 편집 QC

### 문제

두 점 또는 거의 일직선인 점들만으로 mirror를 추론하면 방향을 안정적으로 결정하지 못할 수 있다. 기존 mirrored fit을 편집하면서 stale manual orientation의 parity를 사용하면 mirror가 바뀔 수도 있다.

### 적용 내용

수학적 최소와 앱의 QC 정책을 구분한다.

| 조건 | 의미 |
|---|---|
| Mirror parity 고정 + 서로 다른 두 대응점 | Similarity를 결정할 수 있음 |
| Mirror를 데이터에서 구별 | 비공선 대응점 필요 |
| 앱의 기본 Done 조건 | 4점 QC 정책 유지 |

기본 mirror parity는 **마지막 committed fit의 `reflected`**를 사용한다. 기존 fit이 없을 때만 manual orientation에서 가져온다.

UI는 `Keep mirror state`와 `Infer mirror from landmarks`를 구분한다. Reflection 옵션은 회전·scale까지 고정하지 않으므로 `Keep selected orientation`이라고 표시하지 않는다. 

점들의 비공선성·수치적 안정성과 영상 대비 coverage도 분리한다. 작은 국소 영역의 좋은 점 배치를 coverage가 작다는 이유만으로 invalid 처리하지 않는다.

점 배치 중 영상이 반복해서 회전하거나 mirror가 바뀌지 않도록, 기본은 4점 입력 후 명시적 preview 또는 Done에서 적용한다.

### 완료 기준

자동 mirrored fit의 재편집에서 mirror가 유지된다. 일직선 4점으로 mirror를 자동 확정하지 않는다. 작은 국소 점 세트는 geometry 오류와 coverage 경고를 구분한다.

---

## 5. Clear Points와 Reset Orientation의 책임 분리

**우선순위: 중간**  
**관련 함수:** `clearAlignment()`, Clear transaction, orientation Reset

### 문제

Clear가 점과 fit뿐 아니라 수동 rotation/flip까지 초기화하면, 방향을 먼저 맞춰 놓은 작업도 사라진다.

### 적용 내용

| 동작 | 변경 범위 |
|---|---|
| `Clear Points` | Draft 대응점과 candidate fit 제거 |
| `Reset Orientation` | 선택한 영상의 수동 방향 원복 |
| `Remove Fit` | 명시적으로 fit·수동 보정 제거, manual orientation 유지 |

`Clear Points`는 committed snapshot을 직접 지우지 않는다. 빈 draft의 Done을 허용해 fit 삭제를 우회 구현하지 않는다. Fit을 제거하기만 하는 의도가 필요하면 별도 `Remove Fit` transaction으로 처리한다.

Draft의 점을 비우는 데 viewport 조회가 필요하지 않으면 Clear를 동기적 상태 변경으로 단순화한다. 비동기 경로를 유지한다면 9번의 취소 검증을 적용한다.

### 완료 기준

Flip + 90° → fit → Clear Points 후 manual orientation이 유지된다. Cancel은 기존 committed fit까지 복원한다. Fit 제거는 명시적 Remove Fit에서만 확정된다.

---

## 6. Nudge 대상과 선택한 linked slide 일치

**우선순위: 중간**  
**관련 함수:** `nudgeAlignment()`, `orientationSid`, target controls

### 문제

여러 member 중 C를 선택해도 anchor에서 방향키를 누르면 첫 번째 member B가 움직일 수 있다. 

### 적용 내용

`Orient`를 **`Active linked slide`**로 바꾸고 orientation, anchor-origin nudge, residual inspector의 공통 대상으로 사용한다.

Member pane에서 시작한 nudge는 해당 pane을 대상으로 한다. Anchor에서 시작한 nudge는 active linked slide를 대상으로 한다.

요청에 target context와 epoch를 고정한다. 도중에 target이 제거되어도 다른 member로 조용히 변경하지 않는다. Nudge 완료 후에는 10번에 따라 Current RMS를 갱신한다.

### 완료 기준

A anchor, B·C members에서 C 선택 → A의 방향키 입력 시 C만 이동한다. B pane의 방향키는 B만 이동한다. 제거된 target에 대한 지연 요청은 아무것도 변경하지 않는다.

---

## 7. Grid 상태에서 모든 spatial 작업 공통 차단

**우선순위: 높음**  
**관련 함수:** relay, nudge, recapture, relink, Align, pane key handlers

### 문제

Grid에는 선택된 단일 site가 없다. Relay만 막아도 nudge, relink capture, landmark 시작이 hidden backing P0 viewport를 사용할 수 있다.

### 적용 내용

`spatialGroupReady(operation)`을 공통 진입·완료 검증으로 사용한다. 기본 정책은 spatial 작업에 참여하는 그룹 pane 중 하나라도 grid이거나 context가 없으면 그룹의 공간 동작을 일시 중지하는 것이다.

다음 경로에 모두 적용한다.

```text
Viewport relay
Nudge
Option-drag recapture
Unlink → Relink capture
Align 시작·확정
Orientation
공간 상태를 변경하는 reset transaction
```

Shell뿐 아니라 pane에서도 실제 적용 직전에 local context를 검사한다.

```text
Spatial link paused
Focus a site in each plate
```

Group과 저장 alignment는 유지한다. 단독 plate의 6-well grid와 T/Z 탐색은 그대로 동작한다.

### 완료 기준

Grid에서 방향키·Relink·Align·지연 command를 실행해도 hidden viewer를 이용한 공간 정렬 변경이 없다. Focus 후 readiness가 확인된 경우에만 재개한다.

---

## 8. Pane readiness와 stale snapshot 검증 강화

**우선순위: 중간**  
**관련 함수:** `paneCameUp()`, `normalizeViewportState()`, `requestGroup()`, control enablement

### 문제

`compare.states.has(sid)`만으로는 그 값이 현재 iframe, source/site, image-open 상태에서 온 것인지 알 수 없다.

### 적용 내용

Ready 조건에 현재 pane instance, source generation, spatial context, image-open 상태와 유효한 dimensions/span을 포함한다.

Reload 시 이전 instance의 ready 상태와 snapshot을 제거한다. 새 iframe은 자기 sequence를 새로 시작할 수 있어야 한다.

Transaction은 시작 시점의 `expectedTargets`를 고정한다. 응답 개수를 변화 중인 `groupSids().length`와 단순 비교하지 않는다.

Timeout은 해당 transaction을 무효화할 뿐, 다음 작업의 state를 손상시키지 않아야 한다.

### 완료 기준

느린 open·reload 동안 관련 버튼은 비활성화된다. 이전 iframe의 높은 sequence가 새 iframe state를 덮지 않는다. Timeout 뒤 늦은 응답을 무시하고 다음 요청은 정상 처리한다.

---

## 9. Clear → Cancel 경쟁과 늦은 응답의 state 변경 방지

**우선순위: 높음**  
**관련 함수:** `clearAlignment()`, `finishLandmarks()`, `finishGroupRequest()`, `receiveViewportState()`

### 문제

Clear가 viewport 응답을 기다리는 동안 Cancel하면 복원 후 늦은 Clear가 실행될 수 있다. Callback 실행만 막더라도 reply를 전역 `compare.states`에 먼저 저장하면 최신 viewport를 과거 값으로 되돌릴 수 있다. 

### 적용 내용

Cancel 순서를 고정한다.

```text
Edit와 pending transaction 무효화
→ draft에서 보낸 표시 명령도 만료
→ committed snapshot 복원
→ 새 epoch로 committed 표시 재전송
```

Request reply는 검증 전에 전역 state에 쓰지 않는다. 일치하는 active transaction의 `responses`에만 저장한다. Canceled·unknown request ID의 reply는 종료하고 일반 user-state 처리로 흘려보내지 않는다.

`landmark-points`, `landmark-done`, `landmark-cancel`에도 edit ID와 revision을 포함한다.

`AbortController`나 timer 삭제는 보조 수단이다. 이미 실행 대기 중인 callback도 token 검증으로 거부해야 한다.

### 완료 기준

Clear 요청 → Cancel → 모든 늦은 응답·callback 전달 후 **committed snapshot, 최신 viewport state, memory가 모두 유지된다.** 이전 edit 메시지를 새 edit에 보내도 무시한다.

---

## 10. Fit 이후 수동 이동을 Current RMS에 반영

**우선순위: 높음**  
**관련 함수:** `rematchTranslation()`, `recaptureAll()`, nudge, `formatRms()`, swap/restore

### 문제

Translation을 수동으로 변경해도 `pair.fit.rms`를 계속 표시하면 현재 transform과 품질 값이 달라진다. 

### 적용 내용

Anchor → member mapping을 기준으로 다음을 분리한다.

```text
Fitted transform F
  Landmark fitting 결과

Manual offset Δ
  Member 공간에서 추가한 translation

Effective transform E
  Translation(Δ) ∘ F

Fit RMS
  F를 fitting에 사용한 대응점에 적용한 residual

Current RMS
  E를 같은 유효한 대응점에 적용한 residual
```

화면과 relay는 effective transform을 사용한다. Nudge와 relink는 manual offset 변경이며, 새로운 landmark fitting으로 표시하지 않는다.

```text
4-point fit · manually adjusted
Fit RMS        4.2 µm
Current RMS  100.1 µm
Offset       +99.9 µm, −2.3 µm
```

Residual은 transform 또는 landmark revision이 바뀔 때 계산한다. 단순 pan·render마다 재-fitting하지 않는다.

Swap·inverse restore는 역행렬과 올바른 합성 순서로 처리한다. Offset을 단순 부호 반전하지 않고 역방향 공간에서 다시 표현한다.

### 완료 기준

RMS 0인 fit에 100 µm translation을 더하면 Current RMS는 100 µm이며 Fit RMS는 0으로 남는다. Relink와 swap 후에도 적용 transform·단위·residual이 일치한다.

---

## 11. Landmark set provenance와 memory 저장 경계 수정

**우선순위: 높음**  
**관련 함수:** `rememberAlignment()`, `restoreAlignment()`, `ensurePairTransform()`, `updateCompareControls()`

### 문제

A–B는 anchor set X, A–C는 set Y로 정렬했는데 같은 그룹으로 복원하면 전역 anchor points와 pair transform의 대응 관계가 달라질 수 있다. Render에서 memory를 저장하면 이 혼합 상태가 다시 덮어써질 수 있다.  

### 적용 내용

이번 patch는 기존 UI를 유지하는 **그룹 공통 anchor landmark set** 정책을 사용한다.

Anchor set에 ID와 revision을 부여하고 대응 구조별 point ID를 보존한다. 배열 길이나 좌표 근사값만으로 같은 세트라고 판단하지 않는다.

각 fit은 양쪽 spatial contexts, anchor set ID/revision, member point revision과 fitting 정책을 기록한다. 복원 시 현재 그룹과 provenance가 맞지 않으면 자동으로 valid fit에 합류시키지 않는다.

참고용 transform을 유지한다면 다음처럼 분리한다.

```text
Stored transform
Landmark set mismatch
```

`updateCompareControls()`에서 `rememberAlignment()`를 제거한다. 저장은 orientation 완료, landmark commit, nudge 완료 같은 **명시적 committed mutation**에서만 실행한다.

### 완료 기준

A–B/X 저장 → A–C/Y 저장 → B 추가 시 X 기반 B를 Y 기반 valid fit처럼 표시하거나 저장하지 않는다. Controls render 반복만으로 committed snapshot과 memory가 바뀌지 않는다.

---

## 12. Plate P별 alignment ownership과 context 전환

**우선순위: 높음**  
**관련 함수:** `activeFrameContext()`, `viewportSnapshot()`, `setPlateFocus()`, pair/memory keys

### 문제

Slide SID만으로 정렬을 저장하면 같은 ND2의 P1 정렬이 P2에 적용될 수 있다. `plateGrid` flag만으로는 어느 focused site인지 구분할 수 없다. 

### 적용 내용

기존 `activeFrameContext()`에서 alignment용 spatial context를 도출한다.

```text
WSI
  source + generation + slide

Plate
  source + generation + P

Grid
  spatial context 없음
```

기본 정책은 같은 P 안에서 T/Z에 XY alignment를 공유하는 것이다. Full T/P/Z request key를 그대로 alignment memory key로 쓰지 않는다. 대신 landmark가 취득된 T/Z는 provenance로 남긴다.

P/source 전환 시 pane은 local context epoch를 즉시 갱신하고 pending spatial 명령과 draft를 무효화한다. Shell 통지 전에 옛 명령이 도착해도 거부해야 한다.

새 P에 저장된 정렬이 있으면 검증 후 복원하고, 없으면 `Unaligned` 또는 명시적인 center-matched 상태로 전환한다.

### 완료 기준

```text
P1 fit → P2
→ P1 transform·landmark 적용 중단

P2 → P1
→ P1의 committed 결과 복원
→ 첫 번째 P1 방문의 지연 callback은 거부
```

파일 전체의 manual orientation 공유는 별도 정책이며, 다른 P로 fit·translation을 자동 승계하지 않는다.

---

## 13. 임의 각도 ROI의 preview·확정·export footprint 통일

**우선순위: 중간**  
**관련 함수:** `imageBoundsOfScreenRect()`, `positionRubber()`, `finishSelection()`, `roiOverlayPoints()`

### 문제

회전된 화면의 드래그 사각형을 source-axis-aligned bounding box로 바꾸면 주변 조직이 추가된다. 정사각형 기준 37°에서는 clipping·반올림을 제외한 면적 비가 약 1.96이다.

Source 좌표 손상이라기보다 **사용자가 본 preview와 실제 export 영역의 불일치**다.

### 적용 내용

이번 patch는 raw rectangular export를 유지한다. Arbitrary polygon ND2/TIFF export까지 확장하지 않는다.

공통 geometry helper가 다음을 한 번 수행한다.

```text
Screen corners
→ source rectangle
→ clipping
→ 정수 경계 확정
```

Pointermove preview도 그 최종 source rectangle을 화면 polygon으로 재투영한다. Pointerup, ROI metadata, raw export가 동일 footprint를 사용하게 한다. 단계별로 다른 반올림을 하지 않는다.

Box annotation에도 같은 선택 계약을 적용하고, UI에는 `Source-aligned rectangle`을 표시한다.

### 완료 기준

37° rotation, reflection, edge clipping, zoom 조건에서 preview polygon·확정 ROI·raw export 좌표가 일치한다. 기존 D4 방향의 ROI도 유지한다.

---

## 14. Compare 종료·전환 뒤 deferred OSD command 실행 방지

**우선순위: 높음인 상태 보호. 실기기 발현은 통합 테스트 대상**  
**관련 함수:** `applyLinkedViewport()`, `wireCompareRelay()`, OSD `open` handler

### 문제

OSD open을 기다리는 callback이 이전 group의 center/span을 캡처한 채 남으면 Compare 종료나 group/site 변경 뒤 옛 pan/zoom을 적용할 수 있다. 

### 적용 내용

Target별 `pendingViewportCommand`를 하나만 유지한다. OSD open listener도 pane당 하나만 설치하고, 실행할 때 최신 pending slot을 읽는다.

수신 시와 실제 apply 직전에 다음을 검증한다.

```text
Group session / epoch
Pane instance
Local context epoch
Spatial context
Latest command sequence
```

Stop Compare, Cancel, source/site 변경, reload, member 교체에서 pending slot을 비운다. Listener 제거 여부와 관계없이 callback 내부 guard를 유지한다.

`display-transform`, `viewport-nudge`, draft preview도 같은 수명 규칙을 사용한다. Teardown의 neutral reset은 새 epoch의 명시적 lifecycle 메시지로 처리한다.

### 완료 기준

Viewport apply 대기 → Compare 종료 → OSD open 시 옛 command가 실행되지 않는다. 여러 command가 대기하거나 같은 pane이 새 group에 들어가도 현재 세션의 최신 명령만 적용된다.

---

## 추가 필수 보완: 비정방 pixel과 scalebar

### Physical fitting 성공과 표시 가능성을 구분

Pixel-to-µm 행렬을 \(D_a\), \(D_m\), physical-space similarity의 선형 성분을 \(sQ\)라 하면 필요한 pixel-space mapping은 다음이다.

\[
M_{\mathrm{px}}=D_m^{-1}sQD_a
\]

양쪽 모두 X=0.25, Y=0.50 µm/px이고 physical 회전이 90°라면:

\[
M_{\mathrm{px}}=
\begin{bmatrix}
0 & -2\\
0.5 & 0
\end{bmatrix}
\]

이 변환은 uniform zoom + rotation + reflection만으로 표현되지 않는다. Basis vector 두 개를 메시지로 보내는 것만으로 renderer가 일반 affine을 표시하게 되는 것은 아니다.

**적용 요구사항은 다음과 같다.**

- Physical fit 성공과 renderer 표현 가능성을 별도로 검증한다.
- `MᵀM = λI` 또는 singular values의 일치로 uniform similarity 표현 여부를 판단한다.
- Tolerance는 수치오차·지원 정확도 정책으로 명시하고 테스트한다. 근거 없이 1%를 정확성 기준으로 고정하지 않는다.
- 표현 가능한 경우에도 필요한 **pixel-space mapping**에서 pose와 zoom을 계산한다.
- 표현 불가능하면 exact physical link·commit을 제한하고 이유를 표시한다.
- Relative linking은 명시적인 fallback이어야 한다. 조용히 `Linked · µm`를 유지하지 않는다.

### Rotation-aware scalebar

X pixel size 하나만 사용하는 대신 화면 수평선의 두 점을 source 좌표로 변환해 물리 길이를 계산한다.

```text
µm per CSS pixel =
  hypot(
    delta_source_x × pixel_size_x,
    delta_source_y × pixel_size_y
  )
  / delta_screen_css_pixels
```

DPR을 중복 적용하지 않는다. Rotation, flip, zoom 변경 시 갱신한다.

동일한 비정방 pixel 두 영상의 90°·37°, 서로 다른 anisotropy, 반전, calibration 없음, resize·DPR 조건을 검사한다. Pixel size를 서로 바꾼 90°처럼 단순화되는 특수 사례만으로 통과시키지 않는다.

---

## 권장 PR 구성

| PR | Branch 제안 | 포함 내용 | 완료 기준 |
|---|---|---|---|
| A | `fix/alignment-transaction-lifecycle` | 공통 계약, 1·2·3·9·14, render의 memory write 제거 | 취소·commit·지연 callback의 상태 불변성 |
| B | `fix/alignment-fit-provenance` | 4·5·10·11 | Mirror, 대응점 provenance, effective transform·RMS 일치 |
| C | `fix/compare-spatial-ownership` | 6·7·8·12 | P별 ownership과 모든 spatial action의 readiness |
| D | `fix/compare-roi-calibration` | 13, 비정방 pixel·scalebar, UI 통합 | Footprint 일치와 physical 정렬 오표시 방지 |

기존 수학·orientation 테스트는 유지한다. 추가로 fake timer와 제어 가능한 message queue를 사용해 **사용자 작업 순서와 지연 응답 순서를 섞는 테스트**를 작성한다. 실제 OSD와 DOM이 실행되는 WebKit 통합 테스트도 포함한다.

## 성능·데이터 보존 gate

원본과 연구용 annotation은 읽기 전용 또는 QA 사본으로 분리한다. Cold-cache 검사가 필요하면 별도 QA cache를 사용하고 연구 cache를 삭제하지 않는다.

변경 전후 동일 데이터·장비에서 linked pan/zoom latency, 6-well T/Z latency, command 수, peak RSS, 불필요한 tile reload를 기록한다. Residual은 정렬이 바뀔 때만 다시 계산하고, 무효 command는 pixel read나 tile reload 전에 버린다.

## 최종 완료 조건

**Cancel 뒤에는 취소한 작업의 모든 지연 응답을 실행해도 committed 결과와 최신 상태가 바뀌지 않아야 한다.**

**Valid alignment는 현재 spatial context, 현재 대응점 세트, 실제 effective transform, 표시 residual이 서로 일치하고 renderer가 지원하는 경우에만 허용한다.**

`Valid`나 낮은 RMS는 serial section의 동일 세포 대응 또는 영상 전체의 정확한 registration을 보장한다는 의미로 사용하지 않는다.
