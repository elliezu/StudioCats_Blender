bl_info = {
    "name": "Selected Edge to Bone",
    "author": "elliezu (StudioCats)",
    "version": (1, 0, 2),
    "blender": (4, 0, 0),
    "location": "View3D > N Panel > Edge2Bone",
    "description": "Create bones from selected edges with chain detection, mirror, flip, and auto-parent (keep offset).",
    "category": "Rigging",
}

import bpy
import bmesh
import gpu
import re
from mathutils import Vector
from gpu_extras.batch import batch_for_shader
from bpy.props import (
    StringProperty, PointerProperty, FloatVectorProperty,
    BoolProperty, FloatProperty, IntProperty, CollectionProperty,
    EnumProperty,
)


# ── Properties ──
class ELB_BoneQueueItem(bpy.types.PropertyGroup):
    name: StringProperty(name="Name")
    head: FloatVectorProperty(subtype='XYZ')
    tail: FloatVectorProperty(subtype='XYZ')


class ELB_Properties(bpy.types.PropertyGroup):
    bone_name: StringProperty(name="Bone Name", default="bone_01")
    target_armature: PointerProperty(
        name="Target Armature",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    direction_mode: EnumProperty(
        name="Direction",
        items=[
            ('HEAD', "Head (Hair)", "Closer to Head bone = bone head"),
            ('HIPS', "Hips (Clothing)", "Closer to Hips bone = bone head"),
        ],
        default='HIPS',
    )
    surface_offset: FloatProperty(
        name="Surface Offset",
        default=0.002,
        min=0.0, max=0.1,
        step=0.1, precision=4,
        description="Distance above mesh surface (along vertex normal)",
    )
    auto_increment: BoolProperty(name="Auto++", default=True)
    bone_queue: CollectionProperty(type=ELB_BoneQueueItem)

    # Root / Parent
    root_bone_name: StringProperty(
        name="Root Bone",
        default="",
        description="Parent bone name (keep offset). Leave empty for no parent",
    )
    auto_parent: BoolProperty(
        name="Auto Parent",
        default=True,
        description="Automatically parent created bones to root bone (keep offset)",
    )

    # Mirror
    mirror_separator: EnumProperty(
        name="Separator",
        items=[
            ('_', "_ (bone_L)", "underscore separator"),
            ('.', ". (bone.L)", "dot separator"),
        ],
        default='_',
    )
    mirror_root_mode: EnumProperty(
        name="Mirror Root",
        items=[
            ('AUTO', "Auto Detect", "Mirror root name if L/R found, else share same root"),
            ('SAME', "Same Root", "Mirror bones share the same root bone"),
            ('MIRROR', "Mirror Root", "Mirror bones use mirrored root name"),
            ('CUSTOM', "Custom", "Specify mirror root bone manually"),
        ],
        default='AUTO',
    )
    mirror_root_custom: StringProperty(
        name="Mirror Root Name",
        default="",
        description="Custom root bone name for mirrored bones",
    )

    last_created_bones: StringProperty(name="Last Created", default="")


# ── Core Logic ──

def get_chain_endpoints(selected_edges):
    if not selected_edges:
        return []

    adj = {}
    for e in selected_edges:
        v0, v1 = e.verts[0], e.verts[1]
        adj.setdefault(v0, []).append(v1)
        adj.setdefault(v1, []).append(v0)

    visited = set()
    chains = []

    for start_vert in adj:
        if start_vert in visited:
            continue
        endpoint = start_vert
        seen = {start_vert}
        while True:
            neighbors = [v for v in adj[endpoint] if v not in seen]
            if not neighbors:
                break
            seen.add(neighbors[0])
            endpoint = neighbors[0]

        chain = [endpoint]
        visited.add(endpoint)
        current = endpoint
        while True:
            neighbors = [v for v in adj[current] if v not in visited]
            if not neighbors:
                break
            next_v = neighbors[0]
            chain.append(next_v)
            visited.add(next_v)
            current = next_v

        if len(chain) >= 2:
            chains.append(chain)
    return chains


def get_reference_bone_position(arm_obj, mode):
    if not arm_obj or not arm_obj.data:
        return None
    candidates = {
        'HEAD': ['head', 'Head', 'HEAD', 'J_Bip_C_Head'],
        'HIPS': ['hips', 'Hips', 'HIPS', 'J_Bip_C_Hips', 'hip', 'Hip', 'Pelvis', 'pelvis'],
    }
    for name in candidates.get(mode, []):
        if name in arm_obj.data.bones:
            return arm_obj.matrix_world @ arm_obj.data.bones[name].head_local
    for bone in arm_obj.data.bones:
        bl = bone.name.lower()
        if mode == 'HEAD' and 'head' in bl and 'overhead' not in bl:
            return arm_obj.matrix_world @ bone.head_local
        if mode == 'HIPS' and ('hips' in bl or 'hip' in bl or 'pelvis' in bl):
            return arm_obj.matrix_world @ bone.head_local
    return None


def compute_bone_from_chain(chain, obj_matrix, ref_pos, offset):
    mat = obj_matrix
    mat_rot = mat.to_3x3().normalized()
    start_v, end_v = chain[0], chain[-1]
    start_world = mat @ start_v.co + (mat_rot @ start_v.normal).normalized() * offset
    end_world = mat @ end_v.co + (mat_rot @ end_v.normal).normalized() * offset

    if ref_pos is None:
        return (start_world, end_world) if start_world.z >= end_world.z else (end_world, start_world)
    if (start_world - ref_pos).length <= (end_world - ref_pos).length:
        return start_world, end_world
    else:
        return end_world, start_world


def increment_name(name):
    # If suffix is L/R (_L, _R, .L, .R), increment the number before the suffix
    lr_match = re.search(r'(\d+)([_.]?[LRlr])$', name)
    if lr_match:
        num_str = lr_match.group(1)
        suffix = lr_match.group(2)
        new_num = str(int(num_str) + 1).zfill(len(num_str))
        return name[:lr_match.start()] + new_num + suffix
    # Otherwise: increment the trailing number
    match = re.search(r'(\d+)$', name)
    if match:
        num_str = match.group(1)
        return name[:match.start()] + str(int(num_str) + 1).zfill(len(num_str))
    return name + "_01"


def get_mirror_name(bone_name, sep):
    """Find mirror name of a bone, requiring L/R to be at a word boundary.

    L/R must be followed by end-of-name, digit, dot, or another non-letter
    character. Prevents mid-word matches like 'Lower_leg.L' being mangled
    to 'Lower_reg.L' because of the internal '_l'.
    """
    import re
    other_sep = '.' if sep == '_' else '_'
    for s in (sep, other_sep):
        es = re.escape(s)
        # Uppercase L/R
        pattern = rf'({es})([LR])(?![a-zA-Z])'
        m = re.search(pattern, bone_name)
        if m:
            old_lr = m.group(2)
            new_lr = 'R' if old_lr == 'L' else 'L'
            start, end = m.start(2), m.end(2)
            return bone_name[:start] + new_lr + bone_name[end:]
        # Lowercase l/r
        pattern = rf'({es})([lr])(?![a-zA-Z])'
        m = re.search(pattern, bone_name)
        if m:
            old_lr = m.group(2)
            new_lr = 'r' if old_lr == 'l' else 'l'
            start, end = m.start(2), m.end(2)
            return bone_name[:start] + new_lr + bone_name[end:]
    return None


def has_lr_suffix(name, sep):
    """Check if name contains a proper L/R suffix (not mid-word like 'Lower')."""
    import re
    other = '.' if sep == '_' else '_'
    for s in (sep, other):
        es = re.escape(s)
        if re.search(rf'{es}[LRlr](?![a-zA-Z])', name):
            return True
    return False


def resolve_mirror_root(props):
    """Determine which root bone mirrored bones should use."""
    root = props.root_bone_name.strip()
    if not root:
        return None

    mode = props.mirror_root_mode
    sep = props.mirror_separator

    if mode == 'SAME':
        return root
    elif mode == 'MIRROR':
        m = get_mirror_name(root, sep)
        return m if m else root
    elif mode == 'CUSTOM':
        custom = props.mirror_root_custom.strip()
        return custom if custom else root
    else:  # AUTO
        if has_lr_suffix(root, sep):
            m = get_mirror_name(root, sep)
            return m if m else root
        else:
            return root


# ── Armature Operations ──

def make_bones_in_armature(context, arm_obj, bone_data, parent_name=None):
    """
    Create bones from list of (name, head_world, tail_world).
    Optionally parent to parent_name with keep offset.
    Returns list of created names.
    """
    if not bone_data:
        return []

    inv = arm_obj.matrix_world.inverted()
    prev_mode = context.mode
    prev_active = context.view_layer.objects.active

    if prev_mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.object.select_all(action='DESELECT')
    arm_obj.select_set(True)
    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = arm_obj.data.edit_bones

    # Find parent bone
    parent_bone = None
    if parent_name:
        parent_bone = edit_bones.get(parent_name)

    created = []
    for name, head_w, tail_w in bone_data:
        hl, tl = inv @ head_w, inv @ tail_w
        if (tl - hl).length < 0.0001:
            continue
        bone = edit_bones.new(name)
        bone.head = hl
        bone.tail = tl

        # Parent with keep offset (don't change bone position)
        if parent_bone:
            bone.parent = parent_bone
            bone.use_connect = False  # keep offset

        created.append(bone.name)

    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.object.select_all(action='DESELECT')
    if prev_active and prev_active.name in bpy.data.objects:
        prev_active.select_set(True)
        context.view_layer.objects.active = prev_active
        if prev_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

    return created


def mirror_bones_x(context, arm_obj, bone_names, sep, mirror_root_name=None):
    """Mirror bones across X. Returns (created, skipped)."""
    if not bone_names:
        return [], []

    prev_mode = context.mode
    prev_active = context.view_layer.objects.active

    if prev_mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.object.select_all(action='DESELECT')
    arm_obj.select_set(True)
    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = arm_obj.data.edit_bones
    created = []
    skipped = []

    for name in bone_names:
        src = edit_bones.get(name)
        if not src:
            skipped.append(f"{name} (not found)")
            continue

        mirror_name = get_mirror_name(name, sep)
        if not mirror_name:
            skipped.append(f"{name} (no L/R)")
            continue
        # Use explicit name comparison for safer existence check
        existing_names = {b.name for b in edit_bones}
        if mirror_name in existing_names:
            skipped.append(f"{mirror_name} (exists)")
            continue

        # After edit_bones.new(), bone memory is reallocated and src reference becomes invalid
        # Must copy values before calling new()
        src_head = src.head.copy()
        src_tail = src.tail.copy()
        src_roll = src.roll
        src_parent = src.parent
        src_use_connect = src.use_connect

        new_bone = edit_bones.new(mirror_name)
        new_bone.head = Vector((-src_head.x, src_head.y, src_head.z))
        new_bone.tail = Vector((-src_tail.x, src_tail.y, src_tail.z))
        new_bone.roll = -src_roll

        # Parent logic (use src_parent/src_use_connect since src becomes invalid after new())
        if src_parent:
            src_parent_name = src_parent.name
            if mirror_root_name:
                parent_mirror_name = get_mirror_name(src_parent_name, sep)
                if parent_mirror_name and parent_mirror_name in edit_bones:
                    new_bone.parent = edit_bones[parent_mirror_name]
                elif mirror_root_name in edit_bones:
                    new_bone.parent = edit_bones[mirror_root_name]
                elif src_parent_name in edit_bones:
                    new_bone.parent = edit_bones[src_parent_name]
            else:
                parent_mirror = get_mirror_name(src_parent_name, sep)
                if parent_mirror and parent_mirror in edit_bones:
                    new_bone.parent = edit_bones[parent_mirror]
                elif src_parent_name in edit_bones:
                    new_bone.parent = edit_bones[src_parent_name]

        # Always keep mirrored bone disconnected from parent to preserve our computed head position
        # (use_connect=True would force child.head = parent.tail, overriding mirror coordinates)
        new_bone.use_connect = False

        created.append(new_bone.name)

    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.object.select_all(action='DESELECT')
    if prev_active and prev_active.name in bpy.data.objects:
        prev_active.select_set(True)
        context.view_layer.objects.active = prev_active
        if prev_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

    return created, skipped


# ── Operators ──

class ELB_OT_PickRootBone(bpy.types.Operator):
    """Pick root bone from armature with click (eyedropper-style)."""
    bl_idname = "elb.pick_root_bone"
    bl_label = "Pick Root"
    bl_description = "Click on a bone in the viewport to set it as parent root (ESC/right-click to cancel)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.elb_props.target_armature is not None

    def invoke(self, context, event):
        # Always use eyedropper modal for consistency across all modes
        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Run from 3D Viewport")
            return {'CANCELLED'}

        context.window.cursor_modal_set('EYEDROPPER')
        context.area.header_text_set("Pick Root Bone: Click on a bone | ESC/Right-click to cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self._cleanup(context)
            self.report({'INFO'}, "Cancelled")
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            picked = self._pick_bone_under_mouse(context, event)
            self._cleanup(context)
            if picked:
                context.scene.elb_props.root_bone_name = picked
                self.report({'INFO'}, f"Root: {picked}")
                # Force UI redraw so the StringProperty field reflects the new value
                for area in context.window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                return {'FINISHED'}
            else:
                self.report({'WARNING'}, "No bone under cursor")
                return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _cleanup(self, context):
        context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)

    def _pick_bone_under_mouse(self, context, event):
        """Find closest bone segment to mouse cursor in screen space.

        Computes distance from mouse to the head-tail line segment of each bone
        in 2D screen space, not just to discrete points. This catches long bones
        whose head and tail are both far from the cursor.
        """
        from bpy_extras.view3d_utils import location_3d_to_region_2d

        arm_obj = context.scene.elb_props.target_armature
        if not arm_obj or not arm_obj.data:
            return None

        region = context.region
        rv3d = context.region_data
        if not region or not rv3d:
            return None

        mx, my = event.mouse_region_x, event.mouse_region_y

        best_bone = None
        best_dist = 50.0  # pixel threshold (generous)

        arm_matrix = arm_obj.matrix_world
        for bone in arm_obj.data.bones:
            if bone.hide:
                continue

            head_world = arm_matrix @ bone.head_local
            tail_world = arm_matrix @ bone.tail_local
            head_2d = location_3d_to_region_2d(region, rv3d, head_world)
            tail_2d = location_3d_to_region_2d(region, rv3d, tail_world)

            if head_2d is None and tail_2d is None:
                continue

            # If only one endpoint is in front of camera, use that
            if head_2d is None:
                dist = ((tail_2d[0] - mx) ** 2 + (tail_2d[1] - my) ** 2) ** 0.5
            elif tail_2d is None:
                dist = ((head_2d[0] - mx) ** 2 + (head_2d[1] - my) ** 2) ** 0.5
            else:
                # Distance from point (mx,my) to line segment head_2d-tail_2d
                ax, ay = head_2d
                bx, by = tail_2d
                dx, dy = bx - ax, by - ay
                seg_len_sq = dx * dx + dy * dy
                if seg_len_sq < 1e-6:
                    # Degenerate segment
                    dist = ((ax - mx) ** 2 + (ay - my) ** 2) ** 0.5
                else:
                    t = ((mx - ax) * dx + (my - ay) * dy) / seg_len_sq
                    t = max(0.0, min(1.0, t))
                    px = ax + t * dx
                    py = ay + t * dy
                    dist = ((px - mx) ** 2 + (py - my) ** 2) ** 0.5

            if dist < best_dist:
                best_dist = dist
                best_bone = bone.name

        return best_bone


class ELB_OT_CreateBone(bpy.types.Operator):
    bl_idname = "elb.create_bone"
    bl_label = "Create"
    bl_description = "Create bone(s) from selected edge chain(s)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.elb_props
        return (context.mode == 'EDIT_MESH' and
                context.edit_object is not None and
                props.target_armature is not None and
                props.bone_name.strip())

    def execute(self, context):
        props = context.scene.elb_props
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.normal_update()

        selected_edges = [e for e in bm.edges if e.select]
        if not selected_edges:
            self.report({'WARNING'}, "Select some edges!")
            return {'CANCELLED'}

        chains = get_chain_endpoints(selected_edges)
        if not chains:
            self.report({'WARNING'}, "No valid edge chains found")
            return {'CANCELLED'}

        ref_pos = get_reference_bone_position(props.target_armature, props.direction_mode)

        bone_data = []
        current_name = props.bone_name.strip()
        for chain in chains:
            head_w, tail_w = compute_bone_from_chain(
                chain, obj.matrix_world, ref_pos, props.surface_offset
            )
            bone_data.append((current_name, head_w, tail_w))
            if props.auto_increment:
                current_name = increment_name(current_name)

        # Parent bone
        parent = props.root_bone_name.strip() if props.auto_parent else None

        created = make_bones_in_armature(context, props.target_armature, bone_data, parent)
        if not created:
            self.report({'ERROR'}, "Failed")
            return {'CANCELLED'}

        props.last_created_bones = ",".join(created)

        if props.auto_parent and parent:
            parent_info = f" → parent: {parent}"
        else:
            parent_info = ""

        if props.auto_increment:
            props.bone_name = current_name

        self.report({'INFO'}, f"→ {', '.join(created)}{parent_info}")
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


def mirror_weights_for_bones(context, arm_obj, bone_pairs):
    """Mirror vertex group weights for bone pairs [(src_name, dst_name), ...] using X-axis KD-tree."""
    import mathutils
    mesh_objs = [o for o in context.scene.objects
                 if o.type == 'MESH' and
                 any(m.type == 'ARMATURE' and m.object == arm_obj for m in o.modifiers)]
    if not mesh_objs:
        return 0
    mirrored_count = 0
    for obj in mesh_objs:
        mesh = obj.data
        kd = mathutils.kdtree.KDTree(len(mesh.vertices))
        for i, v in enumerate(mesh.vertices):
            kd.insert(v.co, i)
        kd.balance()
        for src_name, dst_name in bone_pairs:
            src_vg = obj.vertex_groups.get(src_name)
            if not src_vg:
                continue
            dst_vg = obj.vertex_groups.get(dst_name)
            if dst_vg is None:
                dst_vg = obj.vertex_groups.new(name=dst_name)
            threshold = 0.001
            for v in mesh.vertices:
                try:
                    w = src_vg.weight(v.index)
                except RuntimeError:
                    continue
                if w <= 0:
                    continue
                co_mirror = mathutils.Vector((-v.co.x, v.co.y, v.co.z))
                co, mirror_idx, dist = kd.find(co_mirror)
                if dist < threshold:
                    dst_vg.add([mirror_idx], w, 'REPLACE')
                    mirrored_count += 1
    return mirrored_count

class ELB_OT_MirrorBones(bpy.types.Operator):
    bl_idname = "elb.mirror_bones"
    bl_label = "Mirror X"
    bl_description = "Mirror last created bones across X axis (L<->R)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.elb_props
        return (props.target_armature is not None and
                props.last_created_bones.strip() != "")

    def execute(self, context):
        props = context.scene.elb_props
        bone_names = [n.strip() for n in props.last_created_bones.split(",") if n.strip()]

        if not bone_names:
            self.report({'WARNING'}, "No bones to mirror")
            return {'CANCELLED'}

        mirror_root = resolve_mirror_root(props) if props.auto_parent else None

        created, skipped = mirror_bones_x(
            context, props.target_armature, bone_names,
            props.mirror_separator, mirror_root
        )

        msg = []
        if created:
            msg.append(f"→ {', '.join(created)}")
            if mirror_root:
                msg.append(f"→ parent: {mirror_root}")
            # Mirror vertex group weights
            sep = props.mirror_separator
            bone_pairs = [(n, get_mirror_name(n, sep)) for n in bone_names
                          if get_mirror_name(n, sep) and get_mirror_name(n, sep) in created]
            if bone_pairs:
                wcount = mirror_weights_for_bones(context, props.target_armature, bone_pairs)
                if wcount > 0:
                    msg.append(f"weights mirrored: {wcount} verts")
        if skipped:
            msg.append(f"Skipped: {', '.join(skipped)}")

        self.report({'INFO'} if created else {'WARNING'}, " | ".join(msg) if msg else "Nothing")

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class ELB_OT_MirrorSelected(bpy.types.Operator):
    bl_idname = "elb.mirror_selected"
    bl_label = "Mirror Selected"
    bl_description = "Mirror selected bones in armature edit mode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.elb_props
        return (props.target_armature is not None and
                context.mode == 'EDIT_ARMATURE')

    def execute(self, context):
        props = context.scene.elb_props
        arm_obj = props.target_armature
        sep = props.mirror_separator

        selected = [b.name for b in arm_obj.data.edit_bones if b.select]
        if not selected:
            self.report({'WARNING'}, "Select bones to mirror")
            return {'CANCELLED'}

        edit_bones = arm_obj.data.edit_bones
        mirror_root = resolve_mirror_root(props) if props.auto_parent else None
        created, skipped = [], []

        for name in selected:
            src = edit_bones.get(name)
            if not src:
                continue
            mirror_name = get_mirror_name(name, sep)
            if not mirror_name:
                skipped.append(f"{name} (no L/R)")
                continue
            # Use explicit name comparison instead of `in edit_bones` to avoid edge cases
            existing_names = {b.name for b in edit_bones}
            if mirror_name in existing_names:
                skipped.append(f"{mirror_name} (exists)")
                continue

            # Copy values before edit_bones.new() (src becomes invalid after new())
            src_head = src.head.copy()
            src_tail = src.tail.copy()
            src_roll = src.roll
            src_parent = src.parent
            src_parent_name = src_parent.name if src_parent else None
            src_use_connect = src.use_connect

            new_bone = edit_bones.new(mirror_name)
            new_bone.head = Vector((-src_head.x, src_head.y, src_head.z))
            new_bone.tail = Vector((-src_tail.x, src_tail.y, src_tail.z))
            new_bone.roll = -src_roll

            if src_parent:
                if mirror_root:
                    pm = get_mirror_name(src_parent_name, sep)
                    if pm and pm in edit_bones:
                        new_bone.parent = edit_bones[pm]
                    elif mirror_root in edit_bones:
                        new_bone.parent = edit_bones[mirror_root]
                    elif src_parent_name in edit_bones:
                        new_bone.parent = edit_bones[src_parent_name]
                else:
                    pm = get_mirror_name(src_parent_name, sep)
                    if pm and pm in edit_bones:
                        new_bone.parent = edit_bones[pm]
                    elif src_parent_name in edit_bones:
                        new_bone.parent = edit_bones[src_parent_name]

            # Always disconnect to preserve mirrored head position
            new_bone.use_connect = False

            created.append(new_bone.name)

        if created:
            self.report({'INFO'}, f"→ {', '.join(created)}")
        if skipped:
            self.report({'WARNING'}, f"Skipped: {', '.join(skipped)}")
        return {'FINISHED'}


class ELB_OT_FlipLastBones(bpy.types.Operator):
    """Flip head/tail of last created bones."""
    bl_idname = "elb.flip_last_bones"
    bl_label = "Flip Last"
    bl_description = "Swap head and tail of last created bones"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.elb_props
        return (props.target_armature is not None and
                props.last_created_bones.strip() != "")

    def execute(self, context):
        props = context.scene.elb_props
        arm_obj = props.target_armature
        bone_names = [n.strip() for n in props.last_created_bones.split(",") if n.strip()]

        prev_mode = context.mode
        prev_active = context.view_layer.objects.active

        if prev_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.object.select_all(action='DESELECT')
        arm_obj.select_set(True)
        context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode='EDIT')

        flipped = 0
        for name in bone_names:
            bone = arm_obj.data.edit_bones.get(name)
            if bone:
                bone.head, bone.tail = bone.tail.copy(), bone.head.copy()
                flipped += 1

        bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.object.select_all(action='DESELECT')
        if prev_active and prev_active.name in bpy.data.objects:
            prev_active.select_set(True)
            context.view_layer.objects.active = prev_active
            if prev_mode == 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Flipped {flipped} bone(s)")
        return {'FINISHED'}


class ELB_OT_FlipSelectedBones(bpy.types.Operator):
    """Flip head/tail of selected bones in edit mode."""
    bl_idname = "elb.flip_selected_bones"
    bl_label = "Flip Selected"
    bl_description = "Swap head and tail of selected bones in armature edit mode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_ARMATURE'

    def execute(self, context):
        arm_obj = context.active_object
        if not arm_obj or arm_obj.type != 'ARMATURE':
            self.report({'WARNING'}, "No armature active")
            return {'CANCELLED'}

        flipped = 0
        for bone in arm_obj.data.edit_bones:
            if bone.select:
                bone.head, bone.tail = bone.tail.copy(), bone.head.copy()
                flipped += 1

        self.report({'INFO'}, f"Flipped {flipped} bone(s)")
        return {'FINISHED'}


class ELB_OT_QueueBone(bpy.types.Operator):
    bl_idname = "elb.queue_bone"
    bl_label = "Queue"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.elb_props
        return (context.mode == 'EDIT_MESH' and
                context.edit_object is not None and
                props.bone_name.strip())

    def execute(self, context):
        props = context.scene.elb_props
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.normal_update()

        selected_edges = [e for e in bm.edges if e.select]
        if not selected_edges:
            self.report({'WARNING'}, "Select some edges!")
            return {'CANCELLED'}

        chains = get_chain_endpoints(selected_edges)
        if not chains:
            self.report({'WARNING'}, "No valid edge chains")
            return {'CANCELLED'}

        ref_pos = get_reference_bone_position(props.target_armature, props.direction_mode)
        current_name = props.bone_name.strip()
        count = 0
        for chain in chains:
            head_w, tail_w = compute_bone_from_chain(
                chain, obj.matrix_world, ref_pos, props.surface_offset
            )
            item = props.bone_queue.add()
            item.name = current_name
            item.head = head_w
            item.tail = tail_w
            count += 1
            if props.auto_increment:
                current_name = increment_name(current_name)

        if props.auto_increment:
            props.bone_name = current_name
        self.report({'INFO'}, f"Queued {count}")
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class ELB_OT_CreateAllQueued(bpy.types.Operator):
    bl_idname = "elb.create_all_queued"
    bl_label = "Build All"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.elb_props
        return len(props.bone_queue) > 0 and props.target_armature is not None

    def execute(self, context):
        props = context.scene.elb_props
        bone_data = [(item.name, Vector(item.head), Vector(item.tail))
                     for item in props.bone_queue]
        parent = props.root_bone_name.strip() if props.auto_parent else None
        created = make_bones_in_armature(context, props.target_armature, bone_data, parent)
        props.bone_queue.clear()
        if created:
            props.last_created_bones = ",".join(created)
        self.report({'INFO'}, f"Created {len(created)}")
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class ELB_OT_ClearQueue(bpy.types.Operator):
    bl_idname = "elb.clear_queue"
    bl_label = "Clear"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.elb_props.bone_queue.clear()
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class ELB_OT_RemoveFromQueue(bpy.types.Operator):
    bl_idname = "elb.remove_from_queue"
    bl_label = "X"
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    def execute(self, context):
        context.scene.elb_props.bone_queue.remove(self.index)
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


# ── Overlay ──
_draw_handler = None

def draw_overlay():
    context = bpy.context
    if not hasattr(context, 'scene'):
        return
    props = context.scene.elb_props
    if len(props.bone_queue) == 0:
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.state.point_size_set(10)
    gpu.state.line_width_set(3)

    for item in props.bone_queue:
        qh, qt = Vector(item.head), Vector(item.tail)
        shader.uniform_float("color", (0.2, 1.0, 0.4, 0.9))
        batch_for_shader(shader, 'POINTS', {"pos": [qh]}).draw(shader)
        shader.uniform_float("color", (1.0, 0.5, 0.2, 0.9))
        batch_for_shader(shader, 'POINTS', {"pos": [qt]}).draw(shader)
        shader.uniform_float("color", (0.4, 1.0, 0.5, 0.5))
        batch_for_shader(shader, 'LINES', {"pos": [qh, qt]}).draw(shader)

    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.point_size_set(1)
    gpu.state.line_width_set(1)


# ── Panels ──

class ELB_PT_MainPanel(bpy.types.Panel):
    bl_label = "Edge Loop Bone"
    bl_idname = "ELB_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Edge2Bone"

    def draw(self, context):
        layout = self.layout
        props = context.scene.elb_props

        layout.prop(props, "target_armature", text="", icon='ARMATURE_DATA')

        row = layout.row(align=True)
        row.prop_enum(props, "direction_mode", 'HEAD')
        row.prop_enum(props, "direction_mode", 'HIPS')

        layout.separator()

        row = layout.row(align=True)
        row.prop(props, "bone_name", text="", icon='BONE_DATA')
        row.prop(props, "auto_increment", text="", icon='ADD', toggle=True)

        layout.prop(props, "surface_offset")

        layout.separator()

        col = layout.column()
        col.scale_y = 2.0
        col.operator("elb.create_bone", text="CREATE", icon='BONE_DATA')

        # Flip buttons
        row = layout.row(align=True)
        if props.last_created_bones:
            names = [n for n in props.last_created_bones.split(",") if n.strip()]
            row.operator("elb.flip_last_bones",
                         text=f"Flip Last ({len(names)})",
                         icon='FILE_REFRESH')
        else:
            sub = row.column()
            sub.enabled = False
            sub.operator("elb.flip_last_bones", text="Flip Last", icon='FILE_REFRESH')
        row.operator("elb.flip_selected_bones",
                     text="Flip Selected",
                     icon='FILE_REFRESH')

        layout.separator()

        # Queue
        row = layout.row(align=True)
        row.operator("elb.queue_bone", icon='ADD')
        if len(props.bone_queue) > 0:
            row.operator("elb.create_all_queued",
                         text=f"Build ({len(props.bone_queue)})",
                         icon='GROUP_BONE')
            row.operator("elb.clear_queue", text="", icon='TRASH')

        if len(props.bone_queue) > 0:
            box = layout.box()
            for i, item in enumerate(props.bone_queue):
                row = box.row(align=True)
                row.label(text=item.name, icon='BONE_DATA')
                op = row.operator("elb.remove_from_queue", text="", icon='X')
                op.index = i


class ELB_PT_ParentPanel(bpy.types.Panel):
    bl_label = "Parent (Root)"
    bl_idname = "ELB_PT_parent"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Edge2Bone"
    bl_parent_id = "ELB_PT_main"

    def draw_header(self, context):
        self.layout.prop(context.scene.elb_props, "auto_parent", text="")

    def draw(self, context):
        layout = self.layout
        props = context.scene.elb_props
        layout.active = props.auto_parent

        row = layout.row(align=True)
        row.prop(props, "root_bone_name", text="", icon='BONE_DATA')
        row.operator("elb.pick_root_bone", text="", icon='EYEDROPPER')

        if props.root_bone_name.strip():
            arm = props.target_armature
            if arm and arm.data:
                exists = props.root_bone_name.strip() in [b.name for b in arm.data.bones]
                if exists:
                    layout.label(text=f"'{props.root_bone_name}' found", icon='CHECKMARK')
                else:
                    layout.label(text=f"'{props.root_bone_name}' not found!", icon='ERROR')


class ELB_PT_MirrorPanel(bpy.types.Panel):
    bl_label = "Mirror"
    bl_idname = "ELB_PT_mirror"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Edge2Bone"
    bl_parent_id = "ELB_PT_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.elb_props

        row = layout.row(align=True)
        row.prop(props, "mirror_separator", expand=True)

        layout.separator()

        # Mirror root mode (only show if auto_parent + root set)
        if props.auto_parent and props.root_bone_name.strip():
            layout.prop(props, "mirror_root_mode")
            if props.mirror_root_mode == 'CUSTOM':
                layout.prop(props, "mirror_root_custom", text="Root", icon='BONE_DATA')

            # Preview
            root = props.root_bone_name.strip()
            mirror_root = resolve_mirror_root(props)
            if mirror_root and mirror_root != root:
                layout.label(text=f"Root: {root} → {mirror_root}", icon='INFO')
            elif mirror_root:
                layout.label(text=f"Root: {root} (shared)", icon='INFO')

        layout.separator()

        # Mirror buttons
        col = layout.column(align=True)
        col.scale_y = 1.4

        if props.last_created_bones:
            names = [n for n in props.last_created_bones.split(",") if n.strip()]
            col.operator("elb.mirror_bones",
                         text=f"Mirror Last ({len(names)})",
                         icon='MOD_MIRROR')
        else:
            sub = col.column()
            sub.enabled = False
            sub.operator("elb.mirror_bones", text="Mirror Last", icon='MOD_MIRROR')

        col.operator("elb.mirror_selected",
                     text="Mirror Selected Bones",
                     icon='MOD_MIRROR')

        # Preview mapping
        if props.last_created_bones:
            sep = props.mirror_separator
            box = layout.box()
            box.label(text="Preview:", icon='INFO')
            for name in props.last_created_bones.split(","):
                name = name.strip()
                if not name:
                    continue
                mirror = get_mirror_name(name, sep)
                label = f"{name} → {mirror}" if mirror else f"{name} (no L/R)"
                box.label(text=label)


# ── Registration ──
classes = (
    ELB_BoneQueueItem,
    ELB_Properties,
    ELB_OT_PickRootBone,
    ELB_OT_CreateBone,
    ELB_OT_MirrorBones,
    ELB_OT_MirrorSelected,
    ELB_OT_FlipLastBones,
    ELB_OT_FlipSelectedBones,
    ELB_OT_QueueBone,
    ELB_OT_CreateAllQueued,
    ELB_OT_ClearQueue,
    ELB_OT_RemoveFromQueue,
    ELB_PT_MainPanel,
    ELB_PT_ParentPanel,
    ELB_PT_MirrorPanel,
)

def register():
    global _draw_handler
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.elb_props = PointerProperty(type=ELB_Properties)
    _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
        draw_overlay, (), 'WINDOW', 'POST_VIEW'
    )

def unregister():
    global _draw_handler
    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None
    del bpy.types.Scene.elb_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
