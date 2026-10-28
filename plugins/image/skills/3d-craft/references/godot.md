# Export and checking in Godot

What to fix before export, how Godot 4.7.2 imports a glTF asset, and how to
check the result. The export operators and their options (selection,
modifiers, axes, formats) are in `image-blender`; Blender facts here were run
on 5.2.3.

## Before export

- [ ] Real size, unit scale 1.
- [ ] Rotation and scale applied, negative scale included; location is the
      part's intended offset; origin where the asset is placed or turns.
- [ ] Front toward Blender −Y, so it lands on Godot's +Z.
- [ ] Modifiers applied, or exported with modifiers on: Blender's glTF
      exporter leaves them out by default.
- [ ] No floating parts, unintended penetrations or inside-out faces.
- [ ] Stable ASCII names for objects, meshes and materials, chosen on
      purpose: Godot reads suffixes such as `-col`, `-colonly`, `-noimp`,
      `-vcol` and `-alpha` from names, so an accidental one changes the import.
- [ ] Materials built for glTF: Principled BSDF or an unlit setup, image
      textures, data maps as Non-Color, Backface Culling set, unused colour
      attributes removed.
- [ ] Helper objects (references, cages, high-poly sources) left out of the
      export, or named `-noimp`.
- [ ] glTF binary (`.glb`), the format Godot's docs recommend. Leave Draco
      compression off: Godot 4.7.2 refuses the file ("required extension
      'KHR_draco_mesh_compression' is not supported"; verified), although
      Blender's exporter can write it.
- [ ] After export, check the exported file in Godot (below), not the `.blend`.

## Import defaults

A scene imported with Godot 4.7.2's defaults (read from a generated `.import`
file; verified):

| Option | Default | What it means for the asset |
|---|---|---|
| `nodes/root_scale` | 1.0 | Metres in, units out; scale the source, not the import |
| `nodes/use_name_suffixes` | true | `-col`, `-noimp` and the rest apply |
| `meshes/ensure_tangents` | true | MikkTSpace tangents when the file has none |
| `meshes/generate_lods` | true | Automatic LODs (see [budgets.md](budgets.md#lods)) |
| `meshes/create_shadow_meshes` | true | Separate shadow meshes |
| `meshes/light_baking` | 1 | No UV2; set 2 (Static Lightmaps) for LightmapGI |
| `meshes/lightmap_texel_size` | 0.2 | Lightmap resolution, when UV2 is generated |
| `gltf/embedded_image_handling` | 1 | Embedded textures are extracted to files beside the `.glb` |

## Import and report

Import headless, then report on the imported scenes:

```bash
godot --headless --path /abs/project --import
godot --headless --path /abs/project \
    --script "$SKILL/scripts/godot_report.gd" -- res://assets/crate.glb
```

- `--import` "starts the editor, waits for any resources to be imported, and
  then quits". It exits 0 even when an asset fails to import (verified with a
  Draco file), so read its log for `ERROR` lines. The report exits 1 for a
  path that did not import as a scene.
- The report prints one `RESULT {json}` line: every node (class and path),
  each mesh's origin, scale and bounding box, and per surface its triangles,
  vertices, LOD count, vertex colours, UV2, tangents and material; the
  collision shapes; and the asset's bounding box.
- Loading a `.glb` at runtime with `GLTFDocument` (useful for test captures
  that must leave no `.import` files) skips the import options: no
  automatic LODs and no name suffixes (verified: `-col` objects stayed plain
  meshes with no bodies). Check LODs and collision through a real import.

Check the report against the brief:

- [ ] **Size:** the bounding box matches the decided dimensions (Y is up).
- [ ] **Placement:** the asset's lowest point sits at y = 0 when its origin is
      at its bottom centre; node scales are 1.
- [ ] **Front:** the front is on +Z.
- [ ] **Surfaces:** one per material, as planned; no stray default materials.
- [ ] **Colour:** vertex colours present where the style uses them, and the
      material set to use them (`vertex_color_albedo`), or a shader that reads
      `COLOR`.
- [ ] **Sidedness:** cull mode Back for closed meshes, Disabled only for
      sheets.
- [ ] **UV2** present if the asset is lightmapped.
- [ ] **LODs** generated where the triangle count warrants them.
- [ ] **Collision** bodies and shapes as named.

## The first capture

After the report, capture the asset in the target scene and beside an
accepted asset at the gameplay camera, before polishing: see
[lighting-baking.md](lighting-baking.md#the-first-capture). Headless Godot
renders nothing, so the capture needs a normal run.
