# Render setup

Everything here ran on Blender 5.2.3 LTS, headless, with `--factory-startup`.
Factory defaults are quoted so a script can tell what it inherits; set every
value the output depends on explicitly.

## Engines

| Engine | `scene.render.engine` | Samples |
|---|---|---|
| Cycles (path tracer, CPU or GPU) | `CYCLES` | `scene.cycles.samples` |
| EEVEE (raster, GPU) | `BLENDER_EEVEE` | `scene.eevee.taa_render_samples` |
| Workbench (viewport-style) | `BLENDER_WORKBENCH` | none to set |

The factory scene uses `BLENDER_EEVEE`. Blender 4.5 named EEVEE
`BLENDER_EEVEE_NEXT`, which 5.2 rejects. EEVEE and Workbench rendered under
`-b` here with no `DISPLAY` set; Cycles is the engine that can also run on the
CPU.

## Scene settings

```python
import bpy

scene = bpy.context.scene
render = scene.render
render.engine = "CYCLES"
render.resolution_x, render.resolution_y = 1920, 1080
render.resolution_percentage = 100
render.film_transparent = False          # True gives an alpha background (use RGBA output)

cycles = scene.cycles
cycles.samples = 128                     # factory 4096, with adaptive sampling
cycles.use_adaptive_sampling = True      # factory True, noise threshold 0.01
cycles.adaptive_threshold = 0.01
cycles.use_denoising = True              # factory True, OpenImageDenoise
cycles.time_limit = 0.0                  # seconds per frame, 0 for none

scene.eevee.taa_render_samples = 64      # factory 64
```

## Output

```python
import bpy

scene = bpy.context.scene
image = scene.render.image_settings
image.file_format = "PNG"               # also JPEG, OPEN_EXR, TIFF, WEBP, AVIF, ...
image.color_mode = "RGBA"
image.color_depth = "8"                 # "16" for PNG, "16"/"32" for OPEN_EXR
scene.render.filepath = "/abs/out/still.png"
bpy.ops.render.render(write_still=True)  # writes exactly /abs/out/still.png
```

- `bpy.ops.render.render(write_still=True)` writes `render.filepath` as given
  when it already ends in the format's extension (`use_file_extension` is on).
- 5.x adds `image.media_type`: `IMAGE` (default), `MULTI_LAYER_IMAGE` and
  `VIDEO`. Setting it picks the matching format: `VIDEO` selects `FFMPEG`,
  `MULTI_LAYER_IMAGE` selects `OPEN_EXR_MULTILAYER`. Assigning `FFMPEG` or
  `OPEN_EXR_MULTILAYER` to `file_format` while `media_type` is `IMAGE` raises
  `TypeError`; set `media_type` first.
- Frame renders from the command line (`-o path_#### -f N`) name files by
  frame number; see [invocation.md](invocation.md#render-a-blend-from-the-command-line).

## Render devices

Under `--factory-startup` Cycles has no compute device enabled
(`compute_device_type` is `NONE`), whatever the user's own preferences say. A
scene whose `cycles.device` is `GPU` then renders on the CPU, and the run still
exits 0. Enable a device in the same process, before the render.

**Route 1, probe and enable (any machine).** Run the bundled script before the
render or before your own script:

```bash
blender -b /abs/scene.blend --factory-startup --python-exit-code 1 --debug-cycles \
    -P "$SKILL/scripts/cycles_devices.py" \
    -E CYCLES -o "$OUT/frames/frame_####" -F PNG -f 1 \
    -- --require-gpu > "$OUT/render.log" 2>&1
echo "exit=$?"
grep '^RESULT ' "$OUT/render.log"
grep -A4 'Path tracing on:' "$OUT/render.log"
```

Name the `.blend` before the `-P`. A file loaded after the script brings its
own scene settings back: with the order reversed, the same command rendered on
the CPU and still exited 0.

It asks Cycles which backends exist, picks the first with a GPU in the order
OptiX, CUDA, HIP, oneAPI, Metal, enables that backend's GPUs, sets every
scene's `cycles.device`, and prints the choice. With no GPU it falls back to
the CPU, or exits 1 when `--require-gpu` is given. `--backend HIP` (or another
name) insists on one backend; `--probe-only` reports and changes nothing.
Probing a backend that finds no device can log an initialization warning
(for example "OptiX initialization failed"); it is harmless.

**Route 2, name the backend.** When the backend is known (from a probe:
`-P "$SKILL/scripts/cycles_devices.py" -- --probe-only`), Cycles' own flag
does the same for one render:

```bash
BACKEND=HIP   # a backend the probe listed with a GPU
blender -b /abs/scene.blend --factory-startup --python-exit-code 1 --debug-cycles \
    -E CYCLES -o "$OUT/frames/frame_####" -F PNG -f 1 -- --cycles-device "$BACKEND"
```

Keep `-E CYCLES` (here and in Route 1): a file saved with EEVEE renders with
EEVEE, and the device setting then does nothing.

Valid names: `CPU`, `CUDA`, `OPTIX`, `HIP`, `ONEAPI`, `METAL`; append `+CPU`
to use both. A backend with no device stops the run with "Found no Cycles
device of the specified type" and exit 1, rather than falling back.

**Route 3, inside a script.** The calls the bundled script makes:

```python
import bpy

prefs = bpy.context.preferences.addons["cycles"].preferences
backends = [t[0] for t in prefs.get_device_types(bpy.context) if t[0] != "NONE"]
gpus = {b: [d for d in prefs.get_devices_for_type(b) if d.type != "CPU"] for b in backends}
backend = next((b for b in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL") if gpus.get(b)), None)
prefs.compute_device_type = backend or "NONE"
for device in gpus.get(backend, []):
    device.use = True
for scene in bpy.data.scenes:
    scene.cycles.device = "GPU" if backend else "CPU"
print("RESULT", backend or "CPU", [d.name for d in gpus.get(backend, [])])
```

**Proof of the device used.** The configuration is not the proof. With
`--debug-cycles` on the command line, Cycles logs the device that path traced
the frame:

```text
Path tracing on: <GPU name> (<BACKEND>) [<device id>]
Path tracing on: <CPU name> (CPU) (<N> threads) [CPU]
```

With `--with-cpu` or `+CPU` both devices are listed: the GPU on the
`Path tracing on:` line and the CPU on a following line, so read a few lines
after the match (`grep -A4`). Record the entry with the render's result.

## Colour management

The view transform maps scene-linear light to display values. It decides the
pixel values in every 8-bit or 16-bit output, so pin it in each render script and
record it in the result line:

```python
import bpy

scene = bpy.context.scene
view = scene.view_settings
view.view_transform = "AgX"     # factory default
view.look = "None"              # looks belong to a view: "AgX - Punchy", ...
view.exposure = 0.0             # stops, applied before the view transform
view.gamma = 1.0
scene.display_settings.display_device = "sRGB"
scene.render.dither_intensity = 0.0   # factory 1.0 adds noise to 8-bit output
```

Options in 5.2:

- View transforms: `Standard`, `ACES 1.3`, `ACES 2.0`, `Khronos PBR Neutral`,
  `AgX`, `Filmic`, `Filmic Log`, `False Color`, `Raw`. (4.5 has the same list
  without the two ACES entries.)
- AgX looks: `None`, `AgX - Punchy`, `AgX - Greyscale`, and contrast steps from
  `AgX - Very High Contrast` to `AgX - Very Low Contrast`. Standard and Filmic
  looks are `None` and `Very High Contrast` to `Very Low Contrast`. A look name
  must match the current view transform, so set the view first.
- Display devices: `sRGB`, `Display P3`, `Rec.1886`, `Rec.2020`, `Rec.2100-PQ`,
  `Rec.2100-HLG`.
- `bpy.data.colorspace.working_space` (new in 5.x) names the file's
  scene-linear working space; the factory value is `Linear Rec.709`.
- `image_settings.color_management` is `FOLLOW_SCENE` by default; `OVERRIDE`
  gives one output its own view settings.

Measured: a uniform background at known scene-linear values, rendered by
Cycles to 8-bit PNG with dither 0. The table holds the decoded red value.
4.5.14 and 5.2.3 decoded to identical pixels in every case.

| Linear value | Standard | Standard, exposure +1 | AgX | AgX - Punchy | Filmic |
|---|---|---|---|---|---|
| 0.18 | 118 | 162 | 118 | 88 | 128 |
| 0.5 | 188 | 255 | 170 | 144 | 177 |
| 1.0 | 255 | 255 | 197 | 177 | 206 |
| 4.0 | 255 | 255 | 233 | 222 | 242 |

What follows for a measured pipeline (anything that compares pixel values
between runs):

- The same settings gave the same pixels in both versions. A changed view
  transform or look moved them: switching AgX to Standard moved 0.5 from 170 to
  188 and clipped 1.0, while 0.18 stayed at 118. When results shift after a
  Blender upgrade or a file change, compare the colour settings before anything
  else.
- Compare decoded pixels, never file bytes. Every PNG carries `Date`, `Time`
  and `RenderTime` text, and 5.2 adds `sRGB`, `gAMA` and `cHRM` chunks that
  4.5 did not write, so none of the 20 test files matched byte for byte
  although all 20 matched pixel for pixel.
- View settings are saved in each `.blend`, so a file keeps whatever its author
  chose; a script that does not set them inherits that.
- OpenEXR output stores scene-linear values without the view transform (a 0.5
  background read back as 0.5 under AgX), so measure from EXR when the numbers
  must not depend on the view.
- Dither at the factory 1.0 spread a flat 188 into 187 and 188; set
  `dither_intensity = 0` for exact pixel comparisons.
