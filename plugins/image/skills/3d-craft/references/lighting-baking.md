# Lighting and baking

Lighting an asset to judge it, and baking AO and normal maps. Blender facts
were run on 5.2.3 and Godot facts on 4.7.2. Render setup, devices and colour
settings are `image-blender`'s; this file says what to light and bake, and why.

## Light to judge, in three passes

1. **Form: flat or clay.** No textures, no strong light. Judge silhouette,
   proportion and topology. Blender's Workbench engine offers Flat, Studio and
   MatCap lighting, and can colour by material, object, vertex colour, texture
   or a single colour; a single colour with MatCap is a clay pass. Add a
   wireframe overlay for topology.
2. **Surfaces: neutral light.** A neutral-coloured key, fill and rim, or an
   even environment, so material values read true. Check at least one hard
   directional light (it shows smoothing errors, normal-map seams and
   faceting) and one soft even light (it shows albedo and roughness values).
   Orbit, or render front, side, three-quarter and low views.
3. **Look: the target scene in the engine.** The final look is decided where
   the asset will live, under that scene's lights, environment, probes,
   shaders and tonemapping. Blender cannot stand in for it:
   - Blender's world lighting and area lights are not exported to glTF
     (Blender manual), and punctual lights only with "Include Punctual
     Lights". Light the scene in the engine.
   - Blender's factory view transform is AgX; a new Godot `Environment` uses
     the Linear tonemapper (verified). The same albedo can look different, so
     judge final colour in Godot.

Review renders from the gameplay camera's distance and angle as well as close
up. An asset judged only in close orbit is usually over-detailed.

## The first capture

Capture the asset in the target scene as soon as it imports, before
polishing. When it renders black, flat or wrongly lit there, the cause is
nearly always the scene's lighting contract, not the mesh:

- scenes lit only by probes or a project shader, with ambient light off, leave
  standard imported materials unlit (black);
- a project shader may expect inputs the import does not set (vertex colour,
  a second UV set, a texture slot);
- an Unshaded material ignores the lights entirely.

Fix the contract (the material, the shader, the environment) and capture
again. Headless Godot cannot capture: its display driver is a dummy, so
`--headless` reads data but renders nothing. Captures need a normal run.

## Baking

Baking moves detail or lighting into textures (or vertex colours) on the
runtime mesh. In Blender, baking needs Cycles: under EEVEE `bake` fails with
"Current render engine does not support baking" (verified).

### Before baking

- Lock the low-poly mesh, its smoothing (hard edges), its UVs and any cage.
  A change to any of them means baking again; never patch a map to hide a
  changed mesh.
- UVs for the bake must not overlap: faces sharing UV space write the same
  texels, and one of them comes out wrong (verified: an occluded face and an
  open face sharing UVs both received the open face's AO).
- Put hard edges on UV seams; smooth shading elsewhere lets the normal map do
  the work.
- Name the meshes so high and low pairs are obvious (`_high`, `_low`).

### High to low (Selected to Active)

- Select the high-poly mesh, then the low-poly mesh as active, and enable
  Selected to Active. "The rays are cast from the low-poly object inwards
  towards the high-poly object" (Blender manual).
- Detail that stands outside the low-poly surface is missed unless the ray
  start moves outward: set Extrusion (with a cage) or Max Ray Distance
  (without one). Both default to 0 (verified). Use just enough to enclose the
  high mesh; too much projects neighbouring surfaces onto each other.
- For several pieces close together, bake them moved apart ("exploded") so
  one piece's rays cannot hit another's surface, unless the contact shadow is
  wanted in the AO.

### Normal maps

- Bake in Tangent space with the default swizzle +X, +Y, +Z (verified
  defaults). That is the OpenGL convention: a surface tilted toward +V baked
  with green above 0.5 (verified, G = 0.671 for a 20° tilt). Godot "requires
  the normal map to use the X+, Y+ and Z+ coordinates" (OpenGL style), so
  Blender bakes go to Godot without flipping. Maps made for DirectX-style
  engines need their green channel inverted first.
- Godot generates tangents with MikkTSpace on import when the file has none
  (Meshes > Ensure Tangents, on by default; every imported surface had
  tangents although Blender's glTF export writes none by default; verified).
- Save normal maps as Non-Color (linear) data.

### Ambient occlusion

- Cycles' AO bake uses the World's ambient-occlusion Distance, which defaults
  to 10 m (verified: half a metre from a cube, a 0.25 m distance gave no
  occlusion and 10 m gave 0.81). For a prop, set it to the size of the contact
  and crevice shading you want, or the whole asset darkens.
- AO is a multiplier: keep it in the occlusion channel, or in a vertex colour
  for flat styles, not painted into base colour, unless the style wants it
  painted.
- Blender can bake AO straight to a colour attribute instead of an image
  (bake target Vertex Colors; verified). Its resolution is the vertex density.

### Margins

Blender's bake margin defaults to 16 px, Adjacent Faces (verified). Size it
for the mip levels the asset is seen at: see
[uvs.md](uvs.md#padding-and-margins).

### Common artifacts

| Symptom | Usual cause | Fix |
|---|---|---|
| Black or empty patches | Rays never reach the high mesh | Raise Extrusion or Max Ray Distance, or use a cage |
| Another surface's detail printed on a face | Rays too long, reaching a neighbour | Shorten the rays, use a cage, or bake exploded |
| Wavy or gradient shading on flat areas | Low-poly normals smoothed across a sharp corner | Add a hard edge (on a UV seam) or a support loop; bake again |
| Lines along UV seams | Margin too small, or a hard edge off a seam | Raise the margin; move hard edges onto seams |
| Relief inverted in the engine | Green channel in the wrong convention | Use OpenGL style (+Y) for Godot |
| One of two mirrored faces wrong | Overlapping UVs during the bake | Bake with unique UVs; stack mirrored islands afterwards |
