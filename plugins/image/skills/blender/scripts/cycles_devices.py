"""Probe Cycles render devices and enable one backend in this Blender process.

Run it in the same process as the render or script that needs it, after the
.blend is loaded and before the render:

    blender -b /abs/scene.blend --factory-startup --python-exit-code 1 \
        -P /abs/skill/scripts/cycles_devices.py \
        -E CYCLES -o /abs/out/frame_#### -F PNG -f 1 -- --require-gpu

Options (after --):
    --backend auto|CPU|OPTIX|CUDA|HIP|ONEAPI|METAL   default auto
    --with-cpu       also render on the CPU beside the GPU
    --require-gpu    exit 1 when no GPU backend is available
    --probe-only     report what is available and change nothing

auto picks the first backend in PREFERENCE that lists a GPU, and falls back to
the CPU when none does. Preferences are changed in memory only; with
--factory-startup nothing is saved.

Prints one line, RESULT {json}. Exits 1 on bad options, when --require-gpu
finds no GPU, or when a named backend has no device. The result is the
configuration; the device a render actually used is the "Path tracing on:"
line Cycles prints under --debug-cycles.
"""

from __future__ import annotations

import argparse
import json
import sys

PREFERENCE = ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")


class ArgumentError(Exception):
    """A bad option after "--"; reported on the result line, not by argparse."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def script_args(argv: list[str]) -> list[str]:
    """Return the arguments after Blender's "--" separator."""
    return argv[argv.index("--") + 1:] if "--" in argv else []


def parse(argv: list[str]) -> argparse.Namespace:
    # Other scripts in the same run, and Cycles' own --cycles-device, share the
    # arguments after "--": ignore unknown options, and match none by prefix.
    parser = Parser(prog="cycles_devices.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--with-cpu", action="store_true")
    parser.add_argument("--require-gpu", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    args, _ = parser.parse_known_args(argv)
    args.backend = args.backend.upper()
    return args


def choose(available: dict[str, list[str]], requested: str) -> tuple[str | None, str | None]:
    """Pick a backend from {backend: [GPU device names]}.

    Returns (backend, error). backend None means render on the CPU.
    """
    requested = requested.upper()
    if requested == "CPU":
        return None, None
    if requested == "AUTO":
        for backend in PREFERENCE:
            if available.get(backend):
                return backend, None
        for backend in sorted(available):
            if available[backend]:
                return backend, None
        return None, None
    if available.get(requested):
        return requested, None
    return None, f"no {requested} device found"


def probe(prefs, context) -> dict[str, list[str]]:
    """List the GPU devices Cycles finds for each compiled-in backend."""
    available = {}
    for identifier, *_ in prefs.get_device_types(context):
        if identifier == "NONE":
            continue
        devices = prefs.get_devices_for_type(identifier)
        available[identifier] = [d.name for d in devices if d.type != "CPU"]
    return available


def apply(prefs, scenes, backend: str | None, with_cpu: bool) -> list[str]:
    """Enable backend (None for CPU) and point every scene at it."""
    prefs.compute_device_type = backend or "NONE"
    used = []
    if backend:
        for device in prefs.get_devices_for_type(backend):
            device.use = device.type != "CPU" or with_cpu
            if device.use:
                used.append(f"{device.name} ({device.type})")
    for scene in scenes:
        scene.cycles.device = "GPU" if backend else "CPU"
    return used or ["CPU"]


def select(backend: str = "auto", with_cpu: bool = False,
           require_gpu: bool = False, probe_only: bool = False) -> dict:
    """Probe, choose and (unless probe_only) apply. Returns the result dict."""
    import bpy

    prefs = bpy.context.preferences.addons["cycles"].preferences
    available = probe(prefs, bpy.context)
    chosen, error = choose(available, backend)
    if error is None and require_gpu and chosen is None:
        error = "no GPU backend found"
    result = {
        "ok": error is None,
        "tool": "cycles_devices",
        "blender": bpy.app.version_string,
        "requested": backend.upper(),
        "backend": chosen or "CPU",
        "available": available,
        "applied": False,
    }
    if error:
        result["error"] = error
    elif not probe_only:
        result["devices"] = apply(prefs, bpy.data.scenes, chosen, with_cpu)
        result["scenes"] = {s.name: s.cycles.device for s in bpy.data.scenes}
        result["applied"] = True
    return result


def main() -> None:
    try:
        args = parse(script_args(sys.argv))
        result = select(args.backend, args.with_cpu, args.require_gpu, args.probe_only)
    except ArgumentError as exc:
        result = {"ok": False, "tool": "cycles_devices", "error": f"bad option: {exc}"}
    except Exception as exc:  # report it on the result line, then fail
        result = {"ok": False, "tool": "cycles_devices", "error": f"{type(exc).__name__}: {exc}"}
    print("RESULT " + json.dumps(result), flush=True)
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
