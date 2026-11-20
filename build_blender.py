"""build the tingle download: the tinker and wink extensions with the tingle panel, hunch section
and feel loop added. needs checkouts of the tinker and wink repos.
usage: python build_blender.py --tinker ../tinker --wink ../wink [--blender PATH] [--out dist]"""
from __future__ import annotations

import argparse, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def manifest(stage: Path, **fields):
    import re
    path = stage / "blender_manifest.toml"
    text = path.read_text()
    for key, value in fields.items():
        text = re.sub(rf"^{key} = .*$", f'{key} = "{value}"', text, flags=re.M)
    path.write_text(text)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tinker", default=str(ROOT.parent / "tinker"))
    ap.add_argument("--wink", default=str(ROOT.parent / "wink"))
    ap.add_argument("--blender")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    tinker, wink, out = Path(a.tinker), Path(a.wink), Path(a.out)
    tb = load(tinker / "build_blender.py", "tinker_build")
    wb = load(wink / "build_blender.py", "wink_build")
    blender = tb.find_blender(a.blender)
    extra = sorted((ROOT / "blender/tingle").glob("*.py"))
    net = "Local services; optional user-configured run sharing"
    st = tb.stage_tinker(out / "stage/tinker_blender", extra)
    manifest(st, name="tingle", tagline="hunch, tinker and wink in one panel", network=net)
    print(tb.pack(blender, st, out))
    sw = wb.stage_wink(out / "stage/wink_blender", tinker, extra)
    manifest(sw, network=net)
    print(wb.pack(blender, sw, out))
