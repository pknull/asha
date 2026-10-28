# Materials

Choosing a material route, the PBR channels and their colour spaces, flat and
vertex colour, unlit materials and sidedness. glTF facts are from the Khronos
glTF 2.0 specification; Blender behaviour was run on 5.2.3 and Godot import on
4.7.2. Build materials for the engine's shading model: what Godot shows is
what counts, not Blender's viewport.

## Choose the route

| Route | Gives | Costs | Fits |
|---|---|---|---|
| Flat colour, one material per colour | Crisp colour fields, no UVs needed | One surface per colour | Stylised assets with a handful of colours |
| Palette texture | Many flat colours in one material | UVs mapped onto swatches; one small texture | Flat-colour styles with many colours |
| Vertex colour | Colour or baked AO with no texture | Resolution limited to vertex density; the engine material must be told to use it | Low-poly assets; AO on flat styles |
| PBR textures | Surfaces that respond to light: metal, wood, cloth | UVs, texture memory, usually bakes | Lit painted or realistic styles |
| Unlit | Colour exactly as authored | Ignores scene lighting | Emissive parts, UI-like props, styles where a project shader does all the lighting |

Pick the cheapest route that gives the style, and keep it consistent within a
project: a lone PBR prop in a flat-colour scene reads as foreign.

Each material slot becomes its own surface in Godot (verified: one mesh with
three material slots imported as three surfaces). Godot batches nodes that
share a mesh and a material (automatic instancing), so reuse materials across
assets rather than duplicating them.

## PBR channels

glTF's core material is metallic-roughness. The spec fixes the channels and
colour spaces, and Godot reads them as follows (verified on import):

| Map | Channel | Colour space | Godot reads it as |
|---|---|---|---|
| Base colour | RGB (A for alpha) | sRGB | Albedo texture |
| Metallic-roughness | G roughness, B metalness | Linear | Roughness from G, metallic from B |
| Occlusion | R | Linear | AO from R (it may share the metallic-roughness image: an ORM texture) |
| Normal | RGB, tangent space, +X right, +Y up | Linear | Normal map, OpenGL style |
| Emissive | RGB | sRGB | Emission |

- In Blender, set every data image (metallic-roughness, occlusion, normal) to
  Non-Color. Feed roughness and metalness from the G and B outputs of one
  Separate Color node to export them as one packed texture.
- glTF has no socket for occlusion in the Principled BSDF. Blender exports an
  occlusion map from an image connected to the `Occlusion` input of a node
  group named `glTF Material Output` (Blender manual; verified: the exported
  material shared one image for occlusion and metallic-roughness, and Godot
  enabled AO from its red channel).
- **Base colour** is the surface colour only. Do not paint light, shadow or
  AO into it when the engine lights the asset; scans and generated textures
  often carry baked lighting that has to be removed.
- **Metalness** is close to 0 or 1 per region: a surface is metal or it is
  not. Intermediate values belong to blends, such as a worn edge.
- **Roughness** carries most of the visible variation: wear, polish, wet
  patches. Avoid using it as a dirt map.
- **Alpha.** Use a cutout (glTF `MASK`) for leaves, fences and torn edges, and
  blend (`BLEND`) only where partial transparency is the point: Godot renders
  transparent objects back to front and does not auto-instance alpha-blended
  materials. In Godot a material name ending in `-alpha` imports with alpha
  transparency.

## Flat colour and vertex colour

- Flat-colour styles often need no UVs and no textures at all; spend the
  effort on form and on lighting instead.
- **Vertex colour in Godot.** Godot 4.7.2 imports a glTF `COLOR_0` onto the
  mesh but leaves the material's `vertex_color_use_as_albedo` off, both on
  editor import and on runtime `GLTFDocument` loading (verified), although
  glTF defines `COLOR_0` as a multiplier on base colour. Name the Blender
  material with a `-vcol` suffix (Godot then enables "vertex colour as albedo"
  on import and strips the suffix from the name; verified), turn the flag on in
  a material override, or read `COLOR` in the project's shader.
- In Blender, connect the colour attribute to the material (a Color Attribute
  node into Base Color): the glTF exporter's default "Material" mode exports
  vertex colour "when used in material node tree" (Blender manual). On 5.2.3 it
  also wrote an attribute the material did not use, as two extra colour sets;
  delete colour attributes the asset does not use.
- Vertex colour is a good carrier for baked AO in flat styles: Blender bakes
  AO straight to a colour attribute (verified), and a project shader can
  multiply it in.

## Unlit

- A material whose surface is a Background shader exports as
  `KHR_materials_unlit` (verified); the manual also documents a camera-ray
  mix. Godot imports it with Shading Mode Unshaded, which "does not calculate
  lighting at all" (Godot docs; verified).
- Unlit is a style decision. When an asset renders black or flat in the
  engine, first check the scene's lighting and the project's shader (see
  [godot.md](godot.md#the-first-capture)) instead of switching to unlit.

## Sidedness

- Blender's materials default to Backface Culling off, which the exporter
  writes as glTF `doubleSided: true`, and Godot imports as Cull Mode Disabled
  (verified). With Backface Culling on, Godot imported Cull Back (verified).
- Turn Backface Culling on for closed meshes: their back faces are never seen,
  so drawing them only costs. Leave it off only for single sheets that must
  show both sides: cards, leaves, cloth, banners. glTF defines double-sided
  as culling disabled and double-sided lighting enabled.
