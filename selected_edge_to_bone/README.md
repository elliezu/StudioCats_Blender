# Selected Edge to Bone

Create armature bones from selected edge loops in Blender. Designed for quickly rigging skirt strands, hair strips, ribbons, and accessory chains where you want bones to follow specific mesh edges.

## Features

- **Edge chain detection** — select connected edges in a mesh, automatically detects each chain endpoint pair
- **Head/Tail orientation** — choose whether the bone head goes toward the head (hair) or toward the hips (clothing) reference bone
- **Surface offset** — bones can sit slightly above the mesh surface along the vertex normal
- **Queue mode** — accumulate multiple bone definitions before committing
- **Auto-parent (keep offset)** — pick a root bone with the eyedropper, new bones get parented while preserving position
- **Mirror across X** — mirror created or selected bones with smart `.L`/`.R` / `_L`/`_R` name handling
- **Flip head/tail** — swap direction of last created or selected bones
- **Eyedropper picker** — click any bone in the viewport to set as parent root (no mode-switching needed)

## Installation

1. Download `selected_edge_to_bone_v1_0_2.zip` from the [releases page](https://github.com/elliezu/StudioCats_Blender/releases)
2. In Blender: `Edit > Preferences > Add-ons > Install...`
3. Select the ZIP and enable the addon
4. Panel appears in `View3D > N Panel > Edge2Bone`

## Usage

### Basic workflow

1. In **Edit Mode** of a mesh, select edges that form chains (one or more separate chains)
2. In the `Edge2Bone` panel, set the **Object** field to your target armature
3. Choose direction (Head/Hair vs Hips/Clothing) — controls which endpoint becomes the bone head
4. (Optional) Use the eyedropper to pick a parent bone — new bones will be parented with keep-offset
5. Click **Create**

### Mirror

After creating bones (or selecting bones in Armature Edit Mode):

- **Mirror Last** — mirrors the most recently created batch
- **Mirror Selected Bones** — mirrors any bones currently selected in an armature

Name mirroring requires an L/R suffix at the end of the bone name or immediately before a number/separator. Mid-word matches like `Lower_leg.L` correctly preserve `Lower_leg` and only flip the trailing `.L`.

### Parent root

Click the eyedropper to enter pick mode, then click any bone in the viewport. Works from any mode — no need to enter Pose or Edit mode of the armature first. ESC or right-click cancels.

Mirror mode determines what root the mirrored bones use:
- **Auto Detect** — if root has L/R suffix, mirror it; otherwise share the same root
- **Same Root** — both sides parent to the same bone
- **Mirror Root** — mirror the root name (e.g. `Upper_leg.L` → `Upper_leg.R`)
- **Custom** — specify root name manually

## Compatibility

- Blender 4.0+
- Tested on Blender 5.1

## Version history

### 1.0.2
- Mirror name regex now requires word-boundary at L/R suffix (fixes `Lower_leg.L` → `Lower_reg.L` bug)
- Mirrored bones force `use_connect = False` so the computed head position is preserved (was being snapped to parent tail for connected bones)
- Existence check uses explicit name set comparison
- Eyedropper now uses consistent click-to-pick behavior across all modes

### 1.0.0
- Initial public release (rebranded from Edge Loop Bone Creator)
- Added eyedropper modal for parent root picking

## License

MIT — see [LICENSE](../LICENSE) in the repository root.
