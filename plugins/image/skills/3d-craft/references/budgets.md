# Budgets, LODs and collision

How many triangles, materials and texels an asset gets, how it simplifies
with distance, and what it collides with. Godot facts were checked on 4.7.2
and against the Godot 4.7 documentation.

## Set the budget from the camera

There are no universal polycounts: a budget follows from how large the asset
is on screen, how many of it are visible at once, and the platform.

1. **Screen size.** From [uvs.md](uvs.md#choose-a-target-from-the-camera):
   screen pixels per metre at the usual distance, times the asset's size, is
   how many pixels tall it is.
2. **Silhouette density.** Spend triangles until the outline stops changing
   at that size. Triangles much smaller than a pixel cost time and change
   nothing on screen.
3. **Count.** Multiply by how many copies are on screen at once; a prop
   placed fifty times gets a far smaller share than the hero.
4. **Write it down** as triangles, material slots, texture size and LOD count,
   and label a proposed budget as proposed.

Where triangles go, in order: the silhouette, contact points with other
objects, features that identify the asset (a face, hands, a logo), then
everything else. Interiors, undersides and faces the camera never sees come
first when cutting.

## Materials and surfaces

- Each material slot is a separate surface in Godot (verified), drawn on its
  own. Fewer slots per asset and shared materials across assets are the
  cheapest savings; Godot's automatic instancing batches nodes that share a
  mesh and a material, but never alpha-blended materials (Godot docs).
- Merge colours into a palette texture or vertex colour rather than adding
  slots; see [materials.md](materials.md#choose-the-route).
- Keep transparency to the parts that need it, as its own surface: Godot
  sorts transparent objects back to front, and distant transparency eats into
  fill rate (Godot docs).

## Texture memory

- Uncompressed RGBA at 8 bits per channel is width × height × 4 bytes; a full
  mip chain adds a third. A 1024 × 1024 texture is 4 MiB, about 5.3 MiB with
  mipmaps; 2048 is four times that.
- GPU compression cuts that several times over, so check the mode Godot
  imported with. Godot extracts textures embedded in a GLB to image files
  beside it (Embedded Texture Handling defaults to Extract Textures) and
  imports them with mipmaps; the headless import left them Lossless with
  `detect_3d/compress_to` set to VRAM Compressed (verified). Check the mode
  in the Import dock before judging memory.
- Size textures from texel density, not habit, and share them (trim sheets,
  palettes, atlases) across assets.

## LODs

- Godot generates LODs on import for glTF scenes (Meshes > Generate LODs, on
  by default) and picks one per frame with a screen-space metric:
  Rendering > Mesh LOD > LOD Change > Threshold Pixels, default 1 pixel,
  "tuned to look perceptually lossless" (Godot docs). A 16,128-triangle sphere
  imported with 7 LOD levels; a 12-triangle cube with none (verified).
- So in Godot, authored LOD chains are the exception. Author or adjust them
  when:
  - the generated levels break a silhouette that matters (check by forcing a
    lower level, or by raising the threshold, in a test scene);
  - the mesh is skinned, where Godot's docs warn generation "may occasionally
    introduce rendering issues";
  - the far version should be a different object (merged meshes, an impostor,
    fewer materials): use visibility ranges (HLOD) on the nodes.
- When authoring levels by hand, keep the order of the budget above:
  silhouette, contact points and identifying features go last. Keep the
  material slots in the same order across levels.
- Godot also creates shadow meshes on import (Meshes > Create Shadow Meshes,
  on by default; verified in the import defaults).

## Collision

- Collide with something much simpler than the render mesh: boxes, capsules
  and convex hulls first, a triangle mesh only for static level geometry.
- Godot builds collision from name suffixes on import (Godot docs; verified):

  | Suffix on the Blender object | Godot result |
  |---|---|
  | `-col` | The mesh plus a StaticBody3D child with a triangle-mesh shape |
  | `-colonly` | A StaticBody3D with a triangle-mesh shape, no visible mesh |
  | `-convcolonly` | A StaticBody3D with a convex shape, no visible mesh |
  | `-noimp` | Nothing: the node is dropped on import |

  The suffix is stripped from the node name. Godot's docs call convex
  collision "ideal for simple or dynamic objects that require
  mostly-accurate collision detection"; `-convcol` keeps the visible mesh as
  `-col` does.
- Model a dedicated collision object (a few boxes named `-colonly`) instead of
  letting the render mesh collide, unless the asset is a simple shape already.
