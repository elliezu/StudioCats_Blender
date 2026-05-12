# Mirror Sync by Match Table

UV를 손상시키지 않으면서 까다로운 메쉬에서도 완벽한 미러 편집을 가능하게 하는 블렌더 애드온입니다.

A Blender addon that enables perfect mirror editing on difficult meshes without damaging UVs.

---

## 한국어

### 어떤 문제를 해결하나요?

블렌더 기본 X Mirror Edit는 좌표 거리 기반으로 미러 짝을 찾기 때문에, 다음과 같은 메쉬에서는 짝을 잘못 잡거나 일부 버텍스가 미러를 따라가지 않는 문제가 발생합니다.

- Array + Curve 모디파이어로 생성한 반복 형상 (구슬, 체인 등)
- 같은 좌표에 여러 버텍스가 겹쳐있는 메쉬 (UV 작업으로 분리된 버텍스 포함)
- 좁은 간격으로 밀집된 작은 형상들

수동으로 버텍스 좌표를 복사·붙여넣기하는 도구들도 인덱스 매칭 또는 거리 매칭에 의존하기 때문에 이런 케이스에서는 실패합니다.

Mirror Modifier로 다시 미러를 적용하면 해결되지만, **UV가 한쪽으로 통일되어 텍스처 작업이 끝난 메쉬에는 사용할 수 없습니다.**

### 해결 방법

좌우 완벽 대칭 상태에서 한 번만 **버텍스 페어 매칭 테이블**을 생성하여 오브젝트에 저장합니다. 이후 한쪽을 편집한 다음 버튼 한 번으로 반대쪽이 동기화됩니다. 매칭은 좌표와 토폴로지(face 연결 정보)를 함께 활용하므로 겹친 버텍스도 정확하게 구분합니다.

### 전제 조건

- 매칭 테이블 생성 시점에 메쉬가 **좌우 완벽 대칭** 상태여야 합니다.
- 토폴로지(버텍스 추가·삭제)가 바뀌면 테이블을 다시 빌드해야 합니다. 패널에 자동으로 경고가 표시됩니다.
- 매칭 테이블은 오브젝트의 커스텀 프로퍼티에 저장되므로, 오브젝트를 복제하거나 다른 .blend 파일로 이동해도 유지됩니다.

### 설치

1. [Releases](https://github.com/elliezu/StudioCats_Blender/releases) 페이지에서 `mirror_sync_matchtable_vX_X_X.zip` 다운로드
2. Blender > Edit > Preferences > Add-ons > Install... 에서 ZIP 선택
3. 체크박스 활성화

### 사용법

1. 좌우 완벽 대칭 상태의 메쉬를 선택합니다.
2. N패널 > **MirrorTool** 탭 > **Mirror Sync (Match Table)** 패널을 엽니다.
3. **Build Match Table** 버튼을 클릭합니다. (한 오브젝트당 한 번만)
4. Edit Mode에 들어가 한쪽 버텍스만 편집합니다. (블렌더 기본 X Mirror Edit는 끄세요.)
5. 편집한 버텍스가 선택된 상태에서 **Sync to Mirror** 버튼을 클릭합니다.

### 옵션

- **Center Threshold**: 이 거리 이내의 X 좌표를 X=0 센터 버텍스로 간주합니다.
- **Coord Threshold**: 미러 좌표 매칭 시 허용 거리입니다. 겹친 버텍스는 face 토폴로지로 추가 구분됩니다.
- **Sync Direction**:
  - `Auto`: 선택된 쪽을 반대쪽으로 미러합니다.
  - `+X to -X`: 양수 X 쪽을 음수 X 쪽으로 일괄 복사합니다. (선택 무시)
  - `-X to +X`: 음수 X 쪽을 양수 X 쪽으로 일괄 복사합니다. (선택 무시)
- **Snap Center X to 0**: 센터 버텍스의 X 좌표를 강제로 0으로 맞춥니다.

---

## English

### What problem does this solve?

Blender's built-in X Mirror Edit relies on coordinate-distance matching, which fails on certain mesh structures:

- Repeating geometry created with Array + Curve modifiers (beads, chains, etc.)
- Meshes with multiple vertices at the same coordinate (UV-split duplicates)
- Small features packed close together

Manual coordinate copy/paste tools also fail in these cases because they rely on either vertex index or distance matching.

Re-applying a Mirror Modifier would fix the geometry, but **it unifies the UVs to one side, which is destructive to meshes that already have finalized textures.**

### Solution

Build a **vertex pair match table** once while the mesh is perfectly symmetric, stored in the object's custom properties. After editing one side, sync the other side with a single click. Matching uses both coordinates and topology (face center connectivity), so overlapping vertices are disambiguated correctly.

### Requirements

- Mesh must be **perfectly symmetric on the X axis** when the table is built.
- Rebuild the table whenever topology changes (vertex add/remove). The panel warns you automatically.
- The match table is stored as a custom property on the object, so it survives duplication and .blend file transfers.

### Installation

1. Download `mirror_sync_matchtable_vX_X_X.zip` from the [Releases](https://github.com/elliezu/StudioCats_Blender/releases) page.
2. Blender > Edit > Preferences > Add-ons > Install... → select the ZIP.
3. Enable the checkbox.

### Usage

1. Select a mesh that is perfectly symmetric.
2. Open N-panel > **MirrorTool** tab > **Mirror Sync (Match Table)** panel.
3. Click **Build Match Table**. (Once per object.)
4. Enter Edit Mode and modify one side only. (Disable Blender's built-in X Mirror Edit.)
5. With the edited vertices still selected, click **Sync to Mirror**.

### Options

- **Center Threshold** — X coordinates within this distance are treated as center (X=0) vertices.
- **Coord Threshold** — Tolerance for mirror coordinate matching. Overlapping vertices are further disambiguated by face topology.
- **Sync Direction**:
  - `Auto` — mirrors the selected side to the opposite side.
  - `+X to -X` — bulk copies +X side to -X side, ignoring selection.
  - `-X to +X` — bulk copies -X side to +X side, ignoring selection.
- **Snap Center X to 0** — forces center vertex X coordinate to exactly 0.

---

## Author

- **elliezu (StudioCats)**
- Co-developed with Claude (Anthropic)

## License

[MIT License](../LICENSE)
