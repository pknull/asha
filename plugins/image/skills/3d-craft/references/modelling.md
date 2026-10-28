# Modelling

Blockout, choosing a route, topology, scale, pivots and multi-part assets.
The Blender facts below were run on 5.2.3 and the Godot facts on 4.7.2; the
operators that carry them out are in `image-blender`.

## Blockout

- Build from primitives at true size before any detail. A blockout answers
  three questions: does the silhouette read from the gameplay camera, are the
  proportions right against the reference, and does it fit its space (door
  height, reach, the grid of a modular kit).
- Judge it flat or as clay, from the gameplay angle and distance, not from a
  close orbit. Detail cannot rescue a silhouette that does not read.
- Keep the blockout. It is the measurement reference for the final model and
  the first LOD candidate.

## Choose a route

| Route | Use when | Watch for |
|---|---|---|
| Direct low-poly | Flat, stylised or hard-surface assets at a known budget. | Spending faces where the camera never sees the outline. |
| Sculpt, then retopology | Organic forms, or detail that will be baked. | Retopology is a second model; budget the time. |
| Modular kit | Environments built from repeated pieces. | One grid, one texel density and shared pivots across every piece. |
| Generated mesh as blockout | A generator gave usable form but messy topology, arbitrary scale and painted texture. | Use it for proportions and silhouette; rebuild or retopologise rather than ship it. |
| Rigid parts | Assets assembled from separate pieces, possibly each on a bone. | Every part must touch another; overlaps must be intended (see below). |

## Topology and edge flow

Topology serves the next stage: shading, UVs, baking, deformation or LODs.

- **Triangles.** glTF export triangulates every face (verified: an L-shaped,
  concave six-sided n-gon exported as four triangles covering exactly its area). The question is
  never "quads or triangles in the file" but "which triangles, and where".
- **N-gons are fine** on flat, rigid faces that will not be subdivided,
  deformed or used for loop selection: panel faces, card shapes, the flat caps
  of cylinders.
- **Keep quads and loops** where the mesh bends (joints, cloth folds, a mouth),
  where it will be subdivided, and where you need to select or slide loops.
  Put a loop on each side of a joint, and keep the loops that define a
  silhouette or a highlight.
- **Avoid long thin triangles** on curved, shiny or deforming surfaces: they
  interpolate normals badly and shade in streaks.
- **Poles** (vertices with three or five-plus edges) belong on flat or hidden
  areas, not on a bend or a highlight.
- **Density follows the outline.** Spend faces where the silhouette changes at
  the gameplay distance; flatten interiors the camera never sees in profile.
- **Hard edges and smoothing.** Smooth shading across a sharp corner gives a
  dark gradient; split the normals there (sharp edges, or a smooth-by-angle
  setup). Every hard edge and UV seam duplicates vertices on export: a cube
  is 8 vertices smooth without UVs, 14 smooth with UVs and 24 flat (verified on
  export and after Godot import). Put hard edges and seams on the same edges
  where you can, so you pay once.
- **Clean before export:** no loose vertices or edges, no zero-area faces, no
  edges shared by three or more faces, no faces pointing inward. Select All
  by Trait (Loose Geometry, Non-Manifold) and the Face Orientation overlay
  find them.

## Scale and units

- Model at real size. With Blender's factory unit settings (metric, unit scale
  1.0) one unit is a metre; glTF stores metres; Godot "uses the metric system
  for everything in 3D, with 1 unit being equal to 1 meter", and its physics is
  tuned for that (Godot 4.7 docs, Introduction to 3D). A 2 m crate exported
  from Blender imported into Godot at 2 units (verified).
- Decide the size from the brief before modelling, not after: the height of a
  door, a character, a step. Generated meshes arrive at arbitrary scale; scale
  them to the decided size first.
- Check that the unit scale is 1 before export.

## Pivots and transforms

- **Origin.** Put it where the asset is placed or turns: bottom centre for
  things that stand on the floor, the hinge line for doors and lids, the grip
  for held items, the wheel axle for wheels. Godot places and rotates the node
  about that point: a crate with its origin at its bottom centre imported with
  its lowest point at Godot's y = 0 (verified).
- **Front.** Face the asset's front toward Blender's −Y (the side the front view
  shows). glTF export turns that into +Z, and Godot treats +Z as a model's front
  (`Vector3.MODEL_FRONT` is (0, 0, 1); `Vector3.FORWARD` is (0, 0, −1), the way
  cameras and lights point). A marker at Blender −Y imported at Godot +Z
  (verified). Godot's docs say the same: "for oriented 3D assets (such as
  characters), the +Z axis is the direction of the front".
- **Apply rotation and scale.** An unapplied object scale reaches Godot as the
  node's scale (verified: scale 2 in Blender, node scale 2 in Godot), so the
  mesh data and the node disagree about the asset's size. Godot's docs
  recommend applying the object transform before export. A negative scale
  exports as a mirrored node; applying it in Blender reverses the faces so
  the normals stay outward (verified).
- **Location** is the one transform you usually keep: it is the offset of a
  part within the asset, or zero for a single-object asset.

## Multi-part assets

Assets built from separate pieces (rigid card panels, kitbashed props, armour
plates, a character's accessories) fail in three ways that a render can hide:

- **Floating parts.** A piece, or a group of pieces, that touches nothing
  else, often after a parent moved or a column was lifted off the floor.
  Every piece must connect, through pieces that touch, to the rest.
- **Penetrations.** One closed piece reaching into another: a thumb inside a
  forearm, a beam through a wall, a hidden part inside a shell. Penetrations
  waste triangles at best and show through in motion at worst.
- **Joints.** Some overlaps are intended: a limb slotted into a body hides the
  seam when it rotates. Declare them, so the overlap reads as intended, and
  check that they touch.

Conventions that keep parts clean:

- Overlap only at declared joints, and only as deep as the motion needs.
- Offset stacked surfaces (a trim laid on a panel, a decal card) by a small,
  fixed distance instead of placing them coplanar. Coplanar surfaces z-fight;
  check the chosen offset in the engine at the farthest camera distance.
- When a piece crosses a joint, cut it there so each segment moves with its
  own bone, instead of letting one piece bend through another.
- Check motion as well as rest: a pose or animation frame can push two pieces
  together that do not touch at rest. Check the posed frames that matter.

To find them, render from several angles and look in wireframe and X-ray:
a gap or a part buried in another shows there, and in the Godot capture.

## Measure against the reference

Judging likeness by eye drifts; measure instead.

1. **Normalise.** Scale the reference (a generated mesh, a scan, an accepted
   asset) and the new model to the same decided height.
2. **Slice.** Record width and depth at fixed heights on both, and compare
   the rows: shoulders, waist, hem, the height of a sleeve.
3. **Set parameters from the numbers**, and use judgement only where a slice
   mixes two parts (sleeves and torso at the same height).
4. **Compare the same way.** For a family of assets, take the same measures
   and the same Godot report on the new asset and an accepted one, and put the
   numbers side by side: dimensions, parts, triangles, materials, UVs, colour
   layers, joints, bones. A difference is either a decision or a defect;
   write down which.
