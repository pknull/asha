---
name: image-3d-craft
description: "Craft of real-time and game 3D assets in any style: blockout and modelling route, topology and edge flow, scale, units and pivots, UV unwrapping and texel density, PBR, flat-colour and vertex-colour materials, lighting for previews and look development, AO and normal baking, polycount and LOD budgets, export hygiene, and checking an asset in Godot. Use when planning or reviewing topology or edge flow, unwrapping UVs or setting texel density, choosing or building a PBR material, lighting a scene or asset for review, baking AO or normal maps, setting a polycount or LOD budget, or preparing and checking an asset for a game engine."
---

# 3D craft (craft layer)

What to build and how to judge it: general practice for real-time and game
assets, from flat low-poly to sculpted characters.

This skill is the judgement only. Load `image-blender` for the mechanics of
running Blender headless, `bpy`, render setup and the export operators and
their options; this skill names a Blender setting only where it carries a craft
decision. Installing Blender or Godot belongs to the packages layer. A
project's style, palette, shaders and budgets belong to that project's own
documents, and they win wherever they set a convention.

## Start from the brief

Write these down before modelling; when one is unknown, propose a value and
label it proposed:

- **Purpose and camera:** the closest usual viewing distance, the field of
  view and the screen resolution. They set polycount and texel density.
- **Budget:** triangles, material count, texture size, LOD count.
- **Style:** flat colour, painted, PBR, unlit. It sets the material route.
- **Motion:** static, rigid parts on bones, or skinned and deforming.
- **Target:** the engine, its import path and its lighting and shader
  contract (a project shader, probes, no ambient light).
- **Reference:** drawings, a real size, or an accepted asset to match.

## Work big to small, and check each stage

| Stage | Done when |
|---|---|
| Blockout | Primitives at true size; the silhouette reads from the gameplay camera; proportions measured against the reference. |
| Model | A route is chosen (direct low-poly, sculpt then retopology, modular kit, generated mesh as blockout) and the topology serves the next stage. |
| Parts | No floating parts, no unintended penetrations, nothing inside out. |
| UVs | Seams hidden, texel density on target, no overlap where a bake or lightmap needs unique space. |
| Materials | Built for the engine's shading model, not for Blender's viewport. |
| Bake | AO and normals checked for misses, skew and seams. |
| Budget | Triangles, materials and textures within budget; LODs and collision decided. |
| Engine | Imported headless, reported, and captured in the target scene. |

A problem found at blockout costs one change; found after baking it costs the
stage. Never call a model done without looking at renders of it: flat or clay
for form, lit for surfaces, wireframe for topology, from the gameplay angle.

## Rules that come up every time

- **Triangles and n-gons.** glTF export triangulates every face, concave
  n-gons included. N-gons are fine on flat, rigid faces that will not be
  subdivided or deformed. Keep quads and clean loops where the mesh bends,
  subdivides or needs edge-loop selection. Avoid long thin triangles on
  curved, shiny or deforming surfaces, where they shade badly.
- **Scale.** Model at real size: at unit scale 1 a Blender unit is a metre,
  glTF stores metres, and Godot treats a unit as a metre.
- **Transforms.** Apply rotation and scale before export: unapplied ones
  arrive in Godot as node transforms. Apply a negative (mirroring) scale too:
  Blender's apply reverses the faces so the normals stay outward, where an
  unapplied one reaches Godot as a mirrored node.
- **Pivots.** Put the origin where the asset is placed or turns: bottom centre
  for things that stand, the hinge for a door, the grip for a held item.
  The asset's front faces Blender's −Y, which lands on Godot's +Z
  (`Vector3.MODEL_FRONT`).
- **Hard edges cost vertices.** Every hard edge and UV seam splits vertices on
  export (a cube: 8 vertices smooth without UVs, 14 smooth with UVs, 24 flat).
- **Materials.** Each material slot becomes its own surface in Godot. Choose
  the cheapest route that gives the style: see
  [`references/materials.md`](references/materials.md#choose-the-route).
- **Lighting.** Judge form flat or unlit first, surfaces under neutral light
  second, and the final look only in the engine's target scene: Blender's
  world and area lights do not export to glTF.

## Check before calling it done

1. **Check the parts and surfaces**, to catch floating parts, penetrations and
   inside-out faces that a single render hides (`image-blender` has the
   mechanics):
   - render from several angles, the gameplay camera included, and look in
     wireframe and X-ray for parts that float, overlap or hide inside others;
   - Select All by Trait > Non-Manifold and Loose Geometry should select
     nothing unintended, and the Face Orientation overlay should show no red
     (back faces) from outside;
   - unit scale 1, transforms applied, a checker texture for texel density,
     and the UV editor's Select Overlap before a bake.
2. **Measure against the reference.** Compare dimensions and widths at fixed
   heights with the reference or the decided height. For a family of assets,
   measure the new one the same way as an accepted one and compare the tables.
3. **Import in Godot and report** what it made of the file:
   [`references/godot.md`](references/godot.md#import-and-report).
4. **Capture it in the target scene first**, before polishing. A black or
   flat asset there is a lighting or shader contract problem, not a mesh one.

## Scripts

- `scripts/godot_report.gd`: Godot, headless. For imported scenes: nodes,
  surfaces, primitive types, triangles, LOD counts, vertex colours, UV2,
  tangents, materials (and overrides), collision shapes and the bounding box.
  Prints one `RESULT {json}` line.

## References

- [`references/modelling.md`](references/modelling.md): routes, topology and edge flow, scale, pivots, multi-part assets, measuring against a reference.
- [`references/uvs.md`](references/uvs.md): seams, texel density from the camera, padding, overlap, UV2.
- [`references/materials.md`](references/materials.md): choosing a route, PBR channels, flat and vertex colour, unlit, sidedness.
- [`references/lighting-baking.md`](references/lighting-baking.md): preview and look-development lighting, AO and normal bakes, artifacts.
- [`references/budgets.md`](references/budgets.md): polycount, materials, texture memory, LODs, collision.
- [`references/godot.md`](references/godot.md): export hygiene, import defaults, name suffixes, the report, the first capture.

UV, baking and export checklists adapted from omer-metin/skills-for-antigravity `3d-modeling` (Apache-2.0, commit e8dcf4e), rewritten and re-verified on Blender 5.2.3 and Godot 4.7.2.
