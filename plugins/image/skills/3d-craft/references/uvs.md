# UVs and texel density

Where seams go, how much texture each metre gets, how far apart islands sit,
and when UVs may overlap. Blender defaults were read on 5.2.3, Godot on 4.7.2.

## Seams

- Hide seams where something already breaks the surface: hard edges, material
  boundaries, natural creases, the underside, the back, the inside of an
  opening.
- Keep them off focal areas and off continuous detail: across a face, a logo,
  a pattern that must flow.
- Put seams and hard edges on the same edges where you can. Each one splits
  vertices on export; sharing the edge pays once.
- Fewer, larger islands bake and pack better; more cuts reduce stretching.
  Check the balance with a checker texture: squares should stay square and
  equal in size across surfaces seen together.

## Texel density

Texel density is texture pixels per metre of surface:

```text
density (px/m) = texture size (px) × sqrt(UV area fraction / world area (m²))
```

A 1 m plane filling the whole 0-1 UV square at 1024 px is 1024 px/m; each face
of a default 1 m cube (each face a quarter of the square's width) is 256 px/m
at 1024.

### Choose a target from the camera

A surface needs about as many texels per metre as the screen gives it pixels
per metre at the closest usual viewing distance:

```text
screen px/m = screen height (px) / (2 × distance (m) × tan(vertical FOV / 2))
```

At 1080 px and a 75° vertical field of view that is about 352 px/m at 2 m,
141 px/m at 5 m and 70 px/m at 10 m. Pick the target for the closest
distance the asset is usually seen at, not the closest it can ever be.

Then the texture size follows from the surface area and how well the islands
pack (the fraction of the square they cover):

```text
texture size ≈ density × sqrt(total world area / packing fraction)
```

A 1 m cube (6 m²) at 256 px/m with 70 % packing needs about 750 px: a 1024
texture. Round up to the engine's preferred size and record the choice in the
brief.

### Keep it consistent

- Surfaces seen together need the same density, or the sharper one makes the
  other look blurred. Modular kit pieces must share one density.
- Raise density deliberately for focal areas (a face, a hand, a logo) and
  lower it for undersides and backs; write down why.

## Padding and margins

- Filtering and mipmaps blend neighbouring texels, so every island needs a
  margin of extended colour, and islands need space between them.
- A margin shrinks with each mip level: 16 px at full size is 8, 4, 2, then
  1 px four levels down. Size it for the smallest mip the asset is seen at.
- Blender's bake margin defaults to 16 px with the Adjacent Faces method,
  which fills the margin from the faces across each seam (5.2.3 defaults; the
  Blender manual: margins avoid "discontinuities at UV seams, due to texture
  filtering and mip-mapping").
- Space packed islands at least twice the bake margin apart, measured in
  pixels at the bake size, so two islands' margins never meet.

## Overlap and mirroring

- Overlapping or stacked UVs are fine for tiling materials and for symmetric
  parts that share one painted texture: they halve the texture cost.
- Anything baked per surface (AO, normal maps baked from a high-poly mesh,
  lightmaps) needs every face in its own space. Overlapping faces bake on top
  of each other and one of them comes out wrong.
- Before a bake, the UV editor's Select Overlap shows faces sharing space.

## Trim sheets and palettes

- A trim sheet is one texture of strips (edges, bolts, panels) shared by many
  assets: UVs lie on the strips and may run outside 0-1 along a strip's
  length.
- A palette texture is a grid of flat colours. Faces are mapped onto a swatch,
  often collapsed to a point. A palette gives many colours in one material,
  so one surface.

## Lightmap UVs (UV2)

- Godot's LightmapGI needs a second UV set in which every face has its own
  place. Godot generates it on import when Meshes > Light Baking is set to
  Static Lightmaps (verified: the default setting gave no UV2, Static
  Lightmaps gave UV2 on every mesh). Meshes > Lightmap Texel Size (default
  0.2) sets its resolution.
- Godot's docs call authoring UV2 in the modelling program "generally not
  recommended"; do it only when import-time unwrapping is too slow or the
  generated layout fails.
