"""Smoke-test Blender headless: render one still, export a mesh, read it back.

    blender -b --factory-startup --python-exit-code 1 --debug-cycles \
        -P /abs/skill/scripts/smoke.py -- --out /abs/scratch/smoke

Options (after --):
    --out DIR          absolute output directory (required). It must be new,
                       empty, or hold only an earlier smoke run's files.
    --engine NAME      CYCLES (default), EEVEE or WORKBENCH
    --backend NAME     Cycles device, as for cycles_devices.py (default auto)
    --require-gpu      fail when Cycles finds no GPU
    --formats LIST     comma list from glb,fbx,obj,usd,stl (default all five;
                       an empty list renders only)
    --size PX          square render size (default 64)
    --samples N        render samples (default 8)
    --view NAME        view transform (default AgX)

The script builds its own scene in memory and saves no .blend, so a run
changes nothing outside --out. It checks that the render is a PNG of the asked
size and that each export exists, is non-empty and imports back with at least
one mesh. It prints one line, RESULT {json}, and exits 1 when a check fails.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cycles_devices  # noqa: E402

FORMATS = ("glb", "fbx", "obj", "usd", "stl")
OWN_FILES = {"smoke_render.png", "smoke.glb", "smoke.fbx", "smoke.obj", "smoke.mtl",
             "smoke.usdc", "smoke.stl", "textures"}


def parse(argv: list[str]):
    parser = cycles_devices.Parser(prog="smoke.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--out", required=True)
    parser.add_argument("--engine", default="CYCLES")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--require-gpu", action="store_true")
    parser.add_argument("--formats", default=",".join(FORMATS))
    parser.add_argument("--size", type=int, default=64)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--view", default="AgX")
    args, _ = parser.parse_known_args(argv)
    return args


def check_options(args) -> tuple[list[str], list[str]]:
    """Return (formats, errors) for options that need no Blender."""
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    errors = [f"unknown format {f!r}" for f in formats if f not in FORMATS]
    if not os.path.isabs(args.out):
        errors.append(f"--out must be an absolute path, got {args.out!r}")
    elif os.path.isdir(args.out):
        try:
            foreign = sorted(set(os.listdir(args.out)) - OWN_FILES)
        except OSError as exc:
            errors.append(f"--out cannot be read: {exc}")
        else:
            if foreign:
                errors.append(f"--out holds files smoke.py did not write: {foreign[:5]}")
    elif os.path.exists(args.out):
        errors.append(f"--out is not a directory: {args.out!r}")
    return formats, errors


def engine_id(bpy, name: str) -> str:
    name = name.upper().removeprefix("BLENDER_")
    if name in ("EEVEE", "EEVEE_NEXT"):
        # 5.2 calls EEVEE BLENDER_EEVEE; 4.5 called it BLENDER_EEVEE_NEXT.
        return "BLENDER_EEVEE" if bpy.app.version >= (5, 0, 0) else "BLENDER_EEVEE_NEXT"
    return {"WORKBENCH": "BLENDER_WORKBENCH"}.get(name, name)


def png_size(path: str) -> tuple[int, int] | None:
    with open(path, "rb") as handle:
        head = handle.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def build_scene(bpy, scene) -> None:
    import bmesh
    from mathutils import Vector

    bpy.data.batch_remove(list(bpy.data.objects))
    cube_mesh = bpy.data.meshes.new("smoke_cube")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    bm.to_mesh(cube_mesh)
    bm.free()
    paint = bpy.data.materials.new("smoke_mat")
    if paint.node_tree is None:  # before 5.0 a new material had no node tree
        paint.use_nodes = True
    shader = [n for n in paint.node_tree.nodes if n.type == "BSDF_PRINCIPLED"][0]
    shader.inputs["Base Color"].default_value = (0.8, 0.3, 0.1, 1.0)
    cube_mesh.materials.append(paint)
    cube = bpy.data.objects.new("smoke_cube", cube_mesh)
    camera = bpy.data.objects.new("smoke_camera", bpy.data.cameras.new("smoke_camera"))
    camera.location = (4.0, -4.0, 3.0)
    camera.rotation_euler = (Vector((0.0, 0.0, 0.0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    sun = bpy.data.objects.new("smoke_sun", bpy.data.lights.new("smoke_sun", "SUN"))
    sun.data.energy = 3.0
    sun.rotation_euler = (0.6, 0.2, 0.8)
    for ob in (cube, camera, sun):
        scene.collection.objects.link(ob)
    scene.camera = camera


def render(bpy, scene, args, out: str) -> dict:
    scene.render.engine = engine_id(bpy, args.engine)
    scene.render.resolution_x = scene.render.resolution_y = args.size
    scene.render.resolution_percentage = 100
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = False
    scene.eevee.taa_render_samples = args.samples
    view = scene.view_settings
    view.view_transform = args.view
    view.look = "None"
    view.exposure = 0.0
    view.gamma = 1.0
    scene.display_settings.display_device = "sRGB"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    path = os.path.join(out, "smoke_render.png")
    if os.path.exists(path):
        os.remove(path)
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    size = png_size(path) if os.path.exists(path) else None
    return {
        "engine": scene.render.engine,
        "colour": {"view_transform": view.view_transform, "look": view.look,
                   "exposure": view.exposure, "gamma": view.gamma,
                   "display": scene.display_settings.display_device},
        "path": path,
        "bytes": os.path.getsize(path) if size else 0,
        "size": list(size) if size else None,
        "ok": size == (args.size, args.size),
    }


def exporters(bpy):
    return {
        "glb": (lambda p: bpy.ops.export_scene.gltf(filepath=p, export_format="GLB"),
                lambda p: bpy.ops.import_scene.gltf(filepath=p)),
        "fbx": (lambda p: bpy.ops.export_scene.fbx(filepath=p, object_types={"MESH"}),
                lambda p: bpy.ops.import_scene.fbx(filepath=p)),
        "obj": (lambda p: bpy.ops.wm.obj_export(filepath=p),
                lambda p: bpy.ops.wm.obj_import(filepath=p)),
        "usd": (lambda p: bpy.ops.wm.usd_export(filepath=p),
                lambda p: bpy.ops.wm.usd_import(filepath=p)),
        "stl": (lambda p: bpy.ops.wm.stl_export(filepath=p),
                lambda p: bpy.ops.wm.stl_import(filepath=p)),
    }


def export_and_reimport(bpy, fmt: str, out: str) -> dict:
    export, reimport = exporters(bpy)[fmt]
    path = os.path.join(out, "smoke." + ("usdc" if fmt == "usd" else fmt))
    if os.path.exists(path):
        os.remove(path)
    export(path)
    entry = {"path": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0}
    before = set(bpy.data.objects)
    reimport(path)
    added = [ob for ob in bpy.data.objects if ob not in before]
    entry["reimported_meshes"] = sum(1 for ob in added if ob.type == "MESH")
    bpy.data.batch_remove(added)
    entry["ok"] = entry["bytes"] > 0 and entry["reimported_meshes"] >= 1
    return entry


def main() -> None:
    import bpy

    started = time.monotonic()
    result = {"ok": False, "tool": "smoke", "blender": bpy.app.version_string, "errors": []}
    try:
        args = parse(cycles_devices.script_args(sys.argv))
        formats, result["errors"] = check_options(args)
    except cycles_devices.ArgumentError as exc:
        result["errors"].append(f"bad option: {exc}")
    if result["errors"]:
        print("RESULT " + json.dumps(result), flush=True)
        sys.exit(1)
    try:
        os.makedirs(args.out, exist_ok=True)
        scene = bpy.context.scene
        build_scene(bpy, scene)
        if engine_id(bpy, args.engine) == "CYCLES":
            device = cycles_devices.select(args.backend, require_gpu=args.require_gpu)
            result["device"] = {k: device[k] for k in ("backend", "devices", "error") if k in device}
            if not device["ok"]:
                result["errors"].append("device: " + device["error"])
        if not result["errors"]:
            result["render"] = render(bpy, scene, args, args.out)
            if not result["render"]["ok"]:
                result["errors"].append("render: no PNG of the requested size")
            result["exports"] = {}
            for fmt in formats:
                entry = export_and_reimport(bpy, fmt, args.out)
                result["exports"][fmt] = entry
                if not entry["ok"]:
                    result["errors"].append(f"{fmt}: empty export or no mesh on re-import")
    except Exception as exc:  # report it on the result line, then fail
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    result["ok"] = not result["errors"]
    result["seconds"] = round(time.monotonic() - started, 2)
    print("RESULT " + json.dumps(result), flush=True)
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
