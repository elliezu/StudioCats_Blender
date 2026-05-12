bl_info = {
    "name": "Mirror Sync by Match Table",
    "author": "Eliju & Ruby",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > MirrorTool",
    "description": "Topology-aware mirror sync using a precomputed match table. Handles overlapping/duplicate vertices.",
    "category": "Mesh",
}

import bpy
import bmesh
import json
from mathutils import Vector
from mathutils.kdtree import KDTree


# ----------------------------------------------------------
# Properties
# ----------------------------------------------------------

class MirrorSyncMTProps(bpy.types.PropertyGroup):
    center_threshold: bpy.props.FloatProperty(
        name="Center Threshold",
        description="이 거리 이내의 X 좌표는 센터로 간주 (X=0 부근 버텍스)",
        default=0.0001,
        min=0.000001,
        max=0.1,
        precision=6,
    )
    coord_threshold: bpy.props.FloatProperty(
        name="Coord Threshold",
        description="매칭 시 미러 좌표와의 허용 거리. 겹친 버텍스는 face 토폴로지로 구분",
        default=0.0001,
        min=0.000001,
        max=0.01,
        precision=6,
    )
    direction: bpy.props.EnumProperty(
        name="Sync Direction",
        items=[
            ('AUTO', "Auto (선택 기준)", "선택된 쪽을 반대쪽으로 미러"),
            ('PLUS_TO_MINUS', "+X to -X", "양수 X 쪽을 음수 X 쪽으로 복사"),
            ('MINUS_TO_PLUS', "-X to +X", "음수 X 쪽을 양수 X 쪽으로 복사"),
        ],
        default='AUTO',
    )
    snap_center: bpy.props.BoolProperty(
        name="Snap Center X to 0",
        description="센터 버텍스의 X 좌표를 강제로 0으로",
        default=True,
    )


# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------

def _table_key():
    return "mirror_match_table"


def _has_table(obj):
    return obj is not None and _table_key() in obj


def _load_table(obj):
    return json.loads(obj[_table_key()])


def _save_table(obj, data):
    obj[_table_key()] = json.dumps(data)


# ----------------------------------------------------------
# Build Table
# ----------------------------------------------------------

class MESH_OT_msmt_build(bpy.types.Operator):
    bl_idname = "mesh.msmt_build"
    bl_label = "Build Match Table"
    bl_description = "현재 좌우 대칭 상태에서 +X / -X 버텍스 매칭 테이블 생성. 토폴로지로 겹친 버텍스 구분"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        props = context.scene.msmt_props
        center_thresh = props.center_threshold
        coord_thresh = props.coord_threshold

        was_edit = (obj.mode == 'EDIT')
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data
        verts = mesh.vertices

        plus_idx, minus_idx, center_idx = [], [], []
        for v in verts:
            x = v.co.x
            if abs(x) < center_thresh:
                center_idx.append(v.index)
            elif x > 0:
                plus_idx.append(v.index)
            else:
                minus_idx.append(v.index)

        # face center 사전 계산
        vert_face_centers = {i: [] for i in range(len(verts))}
        for poly in mesh.polygons:
            c = poly.center.copy()
            for vi in poly.vertices:
                vert_face_centers[vi].append(c)

        if len(minus_idx) == 0:
            self.report({'ERROR'}, "-X 버텍스가 없음. 좌우 대칭 메쉬여야 함")
            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')
            return {'CANCELLED'}

        kd = KDTree(len(minus_idx))
        for local_i, vi in enumerate(minus_idx):
            kd.insert(verts[vi].co, local_i)
        kd.balance()

        matches = {}
        failed = []
        dup_resolved = 0

        for pi in plus_idx:
            pv = verts[pi]
            mirror_co = Vector((-pv.co.x, pv.co.y, pv.co.z))
            candidates = kd.find_range(mirror_co, coord_thresh)

            if not candidates:
                failed.append(pi)
                continue

            if len(candidates) == 1:
                _, local_i, _ = candidates[0]
                matches[pi] = minus_idx[local_i]
                continue

            # 여러 후보 → face center 토폴로지로 결정
            pv_fc_mirror = [Vector((-c.x, c.y, c.z)) for c in vert_face_centers[pi]]
            best_local = None
            best_score = float('inf')

            for _, local_i, _ in candidates:
                cand_vi = minus_idx[local_i]
                cand_fc = vert_face_centers[cand_vi]
                if len(pv_fc_mirror) != len(cand_fc):
                    continue

                used = set()
                score = 0.0
                valid = True
                for mfc in pv_fc_mirror:
                    best_d = float('inf')
                    best_j = -1
                    for j, cfc in enumerate(cand_fc):
                        if j in used:
                            continue
                        d = (mfc - cfc).length
                        if d < best_d:
                            best_d = d
                            best_j = j
                    if best_j < 0:
                        valid = False
                        break
                    used.add(best_j)
                    score += best_d

                if valid and score < best_score:
                    best_score = score
                    best_local = local_i

            if best_local is None:
                failed.append(pi)
            else:
                matches[pi] = minus_idx[best_local]
                dup_resolved += 1

        # 1:1 검증
        reverse = {}
        collisions = 0
        for pi, mi in matches.items():
            if mi in reverse:
                collisions += 1
            reverse[mi] = pi

        data = {
            "plus_to_minus": {str(k): v for k, v in matches.items()},
            "center": center_idx,
            "vert_count": len(verts),
        }
        _save_table(obj, data)

        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')

        msg = f"매칭 {len(matches)}/{len(plus_idx)} (중복해결 {dup_resolved}, 충돌 {collisions}, 실패 {len(failed)})"
        if collisions > 0 or failed:
            self.report({'WARNING'}, msg)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


# ----------------------------------------------------------
# Sync
# ----------------------------------------------------------

class MESH_OT_msmt_sync(bpy.types.Operator):
    bl_idname = "mesh.msmt_sync"
    bl_label = "Sync to Mirror"
    bl_description = "선택된 버텍스를 매칭 테이블 기반으로 반대쪽에 미러 적용"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and obj.type == 'MESH'
                and obj.mode == 'EDIT'
                and _has_table(obj))

    def execute(self, context):
        obj = context.active_object
        props = context.scene.msmt_props

        data = _load_table(obj)
        if data.get("vert_count", -1) != len(obj.data.vertices):
            self.report({'ERROR'}, "버텍스 수가 매칭 테이블 생성 시점과 다름. 토폴로지가 바뀌었으면 테이블 재생성 필요")
            return {'CANCELLED'}

        plus_to_minus = {int(k): v for k, v in data["plus_to_minus"].items()}
        minus_to_plus = {v: k for k, v in plus_to_minus.items()}
        center_ids = set(data.get("center", []))

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()

        direction = props.direction
        applied = 0
        skipped_conflict = 0

        if direction == 'PLUS_TO_MINUS':
            for pi, mi in plus_to_minus.items():
                pv = bm.verts[pi]
                bm.verts[mi].co = Vector((-pv.co.x, pv.co.y, pv.co.z))
                applied += 1
        elif direction == 'MINUS_TO_PLUS':
            for mi, pi in minus_to_plus.items():
                mv = bm.verts[mi]
                bm.verts[pi].co = Vector((-mv.co.x, mv.co.y, mv.co.z))
                applied += 1
        else:
            # AUTO: 선택된 쪽을 반대쪽으로
            handled_plus = set()
            for pi, mi in plus_to_minus.items():
                pv = bm.verts[pi]
                if pv.select:
                    bm.verts[mi].co = Vector((-pv.co.x, pv.co.y, pv.co.z))
                    handled_plus.add(pi)
                    applied += 1
            for mi, pi in minus_to_plus.items():
                mv = bm.verts[mi]
                if mv.select and pi not in handled_plus:
                    bm.verts[pi].co = Vector((-mv.co.x, mv.co.y, mv.co.z))
                    applied += 1
                elif mv.select and pi in handled_plus:
                    skipped_conflict += 1

        snapped = 0
        if props.snap_center:
            for ci in center_ids:
                cv = bm.verts[ci]
                if cv.select:
                    cv.co.x = 0.0
                    snapped += 1

        bmesh.update_edit_mesh(obj.data)

        msg = f"동기화 {applied}개"
        if snapped:
            msg += f", 센터스냅 {snapped}개"
        if skipped_conflict:
            msg += f" (양쪽선택 충돌 {skipped_conflict}개는 +X 우선)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ----------------------------------------------------------
# Clear
# ----------------------------------------------------------

class MESH_OT_msmt_clear(bpy.types.Operator):
    bl_idname = "mesh.msmt_clear"
    bl_label = "Clear Match Table"
    bl_description = "이 오브젝트의 매칭 테이블 삭제"

    @classmethod
    def poll(cls, context):
        return _has_table(context.active_object)

    def execute(self, context):
        obj = context.active_object
        del obj[_table_key()]
        self.report({'INFO'}, "매칭 테이블 삭제됨")
        return {'FINISHED'}


# ----------------------------------------------------------
# Panel
# ----------------------------------------------------------

class VIEW3D_PT_msmt(bpy.types.Panel):
    bl_label = "Mirror Sync (Match Table)"
    bl_idname = "VIEW3D_PT_msmt"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'MirrorTool'

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        props = context.scene.msmt_props
        obj = context.active_object

        box = layout.box()
        if _has_table(obj):
            try:
                data = _load_table(obj)
                pair_count = len(data.get("plus_to_minus", {}))
                vc = data.get("vert_count", -1)
                cur_vc = len(obj.data.vertices)
                box.label(text=f"Table OK ({pair_count} pairs)", icon='CHECKMARK')
                if vc != cur_vc:
                    box.label(text=f"verts changed: {vc} -> {cur_vc}", icon='ERROR')
            except Exception:
                box.label(text="Table corrupted", icon='ERROR')
        else:
            box.label(text="No Table - Build first", icon='INFO')

        bbox = layout.box()
        bbox.label(text="1. Build (좌우 대칭 상태에서)", icon='MOD_BUILD')
        col = bbox.column(align=True)
        col.prop(props, "center_threshold")
        col.prop(props, "coord_threshold")
        bbox.operator("mesh.msmt_build", icon='FILE_TICK')

        sbox = layout.box()
        sbox.label(text="2. Sync (편집 후)", icon='MOD_MIRROR')
        sbox.prop(props, "direction")
        sbox.prop(props, "snap_center")
        row = sbox.row()
        row.scale_y = 1.5
        row.operator("mesh.msmt_sync", icon='UV_SYNC_SELECT')

        layout.separator()
        layout.operator("mesh.msmt_clear", icon='TRASH')


# ----------------------------------------------------------
# Register
# ----------------------------------------------------------

classes = (
    MirrorSyncMTProps,
    MESH_OT_msmt_build,
    MESH_OT_msmt_sync,
    MESH_OT_msmt_clear,
    VIEW3D_PT_msmt,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.msmt_props = bpy.props.PointerProperty(type=MirrorSyncMTProps)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.msmt_props


if __name__ == "__main__":
    register()
