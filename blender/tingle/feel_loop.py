"""Feel loop: hunch reads a space, a proposal is approved, and a full building
variant is built beside the original with the edits applied.

The original stays untouched. Each accepted proposal becomes its own collection
(e.g. "calm v2"), offset along +X, labelled, and independently exportable.
Edit proposals are a mapping from hunch suggestions onto this scene, not a
trained wink policy that outputs geometry. Hunch does not read a person.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from . import hunch_scene as HS

def _HP():
    from . import hunch_panel as HP
    return HP

SOURCE_COLLECTIONS = ("structure", "roof_louvers", "glazing", "rooms", "deck", "battery", "fittings")
GAP_M = 4.0
_STATE = "feel_loop_state.json"
_PROPOSAL = "feel_loop_proposal.json"

# material presets (linear-ish sRGB, used as Principled Base Color)
PRESETS = {
    "timber": {"color": (0.42, 0.25, 0.13, 1.0), "roughness": 0.48, "specular": 0.3, "kind": "timber", "warmth": 78},
    "plaster": {"color": (0.93, 0.91, 0.86, 1.0), "roughness": 0.72, "specular": 0.2, "kind": "plaster", "warmth": 48},
    # mid-value warm surface aimed at the calm colour/material terms (value ~55, warm hue)
    "calm_timber": {"color": (0.55, 0.40, 0.28, 1.0), "roughness": 0.52, "specular": 0.28, "kind": "timber", "warmth": 70},
    # mid-value warm plaster-like finish: no timber/earth/fabric/brick kind, so cozy's
    # timber_share term does not overtake calm.
    "calm_surfaces": {"color": (0.52, 0.44, 0.34, 1.0), "roughness": 0.62, "specular": 0.22, "kind": "plaster", "warmth": 58},
}


def _st(context):
    return context.scene.hunch


def _text_json(name, default=None):
    t = bpy.data.texts.get(name)
    if t is None:
        return default
    try:
        return json.loads(t.as_string() or "null")
    except ValueError:
        return default


def _set_text_json(name, value):
    t = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    t.clear()
    t.write(json.dumps(value, indent=2))


def _parent_collection(col):
    for p in bpy.data.collections:
        if col.name in p.children:
            return p
    return None


def is_variant_collection(col) -> bool:
    cur = col
    while cur is not None:
        if cur.get("feel_loop_variant"):
            return True
        cur = _parent_collection(cur)
    return False


def in_variant(obj) -> bool:
    return any(is_variant_collection(c) for c in obj.users_collection)


def walk_collection(col):
    for o in col.objects:
        yield o
    for ch in col.children:
        yield from walk_collection(ch)


def iter_scope(context, scope=None):
    """Iterate objects in a scope: 'original', a collection name, or all."""
    scene = context.scene
    if scope in (None, "", "all"):
        for obj in scene.objects:
            yield obj
        return
    if scope == "original":
        for obj in scene.objects:
            if not in_variant(obj):
                yield obj
        return
    col = bpy.data.collections.get(scope) if isinstance(scope, str) else scope
    if col is None:
        for obj in scene.objects:
            yield obj
        return
    seen = set()
    for o in walk_collection(col):
        if o.name not in seen:
            seen.add(o.name)
            yield o


def world_bbox(objects):
    pts = []
    for obj in objects:
        if obj.type not in {"MESH", "CURVE", "FONT", "LIGHT", "CAMERA", "EMPTY"}:
            continue
        try:
            if obj.type == "MESH" and obj.data and obj.data.vertices:
                for c in obj.bound_box:
                    pts.append(obj.matrix_world @ Vector(c))
            else:
                pts.append(obj.matrix_world.translation.copy())
        except (AttributeError, RuntimeError):
            pts.append(obj.matrix_world.translation.copy())
    if not pts:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    xs, ys, zs = [p.x for p in pts], [p.y for p in pts], [p.z for p in pts]
    return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))


def base_name(context) -> str:
    fp = bpy.data.filepath or "scene"
    return Path(fp).stem or "scene"


def list_variants():
    scene_ch = set(bpy.context.scene.collection.children.keys())
    roots = [c for c in bpy.data.collections if c.get("feel_loop_variant") and c.name in scene_ch]
    return sorted(roots, key=lambda c: int(c.get("feel_loop_index") or 0))


def next_index():
    vs = list_variants()
    if not vs:
        return 2
    return max(int(c.get("feel_loop_index") or 1) for c in vs) + 1


def source_objects(context):
    """Objects that belong to the original building (not a feel-loop variant)."""
    names = set(SOURCE_COLLECTIONS)
    out = []
    for obj in context.scene.objects:
        if in_variant(obj):
            continue
        if obj.type == "FONT" and "label" in obj.name.lower():
            continue
        in_src = any(_in_named(obj, n) for n in names)
        # also keep uncollected cameras, lights, viewpoint empties of the original
        if in_src or (obj.type in {"CAMERA", "LIGHT", "EMPTY"} and not in_variant(obj)):
            out.append(obj)
    return out


def _in_named(obj, name):
    return any(c.name == name or _ancestor_named(c, name) for c in obj.users_collection)


def _ancestor_named(col, name):
    cur = col
    while cur is not None:
        if cur.name == name:
            return True
        cur = _parent_collection(cur)
    return False


def _copy_object(obj):
    new = obj.copy()
    if obj.data:
        new.data = obj.data.copy()
        if hasattr(new.data, "materials"):
            for i, mat in enumerate(list(new.data.materials)):
                if mat is not None:
                    new.data.materials[i] = mat.copy()
    for k in ("tinker_id",):
        if k in new:
            new[k] = str(new[k]) + "@variant"
    new["feel_loop_src"] = obj.name
    if obj.get("hunch_room"):
        new["hunch_room"] = obj.get("hunch_room")
    elif obj.get("hunch_room_shell") or obj.name.lower() in ("bath", "ground floor", "loft") or "room" in (obj.name.lower() + " " + " ".join(c.name for c in obj.users_collection)):
        new["hunch_room"] = obj.get("hunch_room") or obj.name
    return new


def _ensure_collection(name, parent):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
    if col.name not in parent.children:
        parent.children.link(col)
    return col


def duplicate_building(context, collection_name: str, index: int, target: str, dx: float):
    """Deep-copy the original building into a new root collection, offset by dx on X."""
    root = bpy.data.collections.new(collection_name)
    context.scene.collection.children.link(root)
    root["feel_loop_variant"] = True
    root["feel_loop_index"] = index
    root["feel_loop_target"] = target

    child_map = {}
    for n in SOURCE_COLLECTIONS:
        src = bpy.data.collections.get(n)
        if src is None:
            continue
        ch = bpy.data.collections.new(f"{collection_name} / {n}")
        ch["feel_loop_variant"] = True
        root.children.link(ch)
        child_map[n] = ch

    mapping = {}  # original object -> new object
    for obj in source_objects(context):
        new = _copy_object(obj)
        # link into the matching child collection, or the root
        dest = None
        for n, ch in child_map.items():
            if _in_named(obj, n):
                dest = ch
                break
        (dest or root).objects.link(new)
        # do not leave a copy in the original collections / scene root
        # (obj.copy() is not linked until we link it)
        mapping[obj] = new

    # remap parents, keep world matrices, then offset roots
    for orig, new in mapping.items():
        if orig.parent is not None and orig.parent in mapping:
            mw = new.matrix_world.copy()
            new.parent = mapping[orig.parent]
            new.matrix_world = mw
    for orig, new in mapping.items():
        if new.parent not in mapping.values():
            mw = new.matrix_world.copy()
            mw.translation.x += dx
            new.matrix_world = mw

    try:
        context.view_layer.update()
    except Exception:
        pass
    return mapping, root


def add_label(root, name, mn, mx):
    curve = bpy.data.curves.new(name + " font", "FONT")
    curve.body = name
    curve.size = max(0.45, min(0.9, (mx.x - mn.x) * 0.08))
    curve.align_x = "CENTER"
    ob = bpy.data.objects.new(name + " label", curve)
    mid_x = (mn.x + mx.x) / 2.0
    ob.location = (mid_x, mn.y - 1.2, 0.05)
    root.objects.link(ob)
    ob["feel_loop_role"] = "label"
    return ob


def _room_name_of(obj):
    return str(obj.get("hunch_room") or obj.name or "")


def _bbox_of(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs, ys, zs = [p.x for p in corners], [p.y for p in corners], [p.z for p in corners]
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def _room_bounds_from_shells(room_name, mapping=None):
    """World AABB of the named room shell, preferably from the originals in a mapping."""
    objs = list(mapping.keys()) if mapping else list(bpy.data.objects)
    want = (room_name or "").lower()
    for obj in objs:
        if obj.type != "MESH":
            continue
        if not (HS.is_room_shell(obj) or obj.get("hunch_room_shell") or obj.get("hunch_kind") == "room"):
            # still accept an object literally named as the room
            if _room_name_of(obj).lower() != want and obj.name.lower() != want:
                continue
        if _room_name_of(obj).lower() == want or obj.name.lower() == want:
            try:
                return _bbox_of(obj)
            except Exception:
                continue
    return None, None


def _room_bounds(result, room_name, mapping=None):
    # prefer a live shell in the scene (or the originals being copied)
    mn, mx = _room_bounds_from_shells(room_name, mapping)
    if mn is not None:
        return mn, mx
    focus = result.get("focus") or {}
    room = (focus.get("room") or {}) if focus else {}
    # rooms3d may live on the snapshot, not the result; try several places
    for block in (result.get("rooms3d"), result.get("snapshot", {}).get("rooms3d") if isinstance(result.get("snapshot"), dict) else None):
        if block:
            for r in block:
                if (r.get("name") or "").lower() == (room_name or "").lower():
                    return list(r["min"]), list(r["max"])
    snap = _text_json("hunch_last_snapshot.json") or {}
    for r in snap.get("rooms3d") or []:
        if (r.get("name") or "").lower() == (room_name or "").lower():
            return list(r["min"]), list(r["max"])
    # fall back to scored room centroid + size if present
    for rec in result.get("rooms") or []:
        rd = rec.get("room") or {}
        if (rd.get("name") or "").lower() == (room_name or "").lower():
            c = rd.get("centroid") or []
            h = float(rd.get("ceiling_height") or 2.5)
            a = float(rd.get("floor_area") or 12.0)
            side = math.sqrt(max(a, 1.0))
            if len(c) == 3:
                return [c[0] - side / 2, c[1] - side / 2, c[2] - h * 0.05], [c[0] + side / 2, c[1] + side / 2, c[2] + h]
    return None, None


def _centroid_in(obj, mn, mx, pad=0.35):
    loc = obj.matrix_world.translation
    return (mn[0] - pad <= loc.x <= mx[0] + pad and
            mn[1] - pad <= loc.y <= mx[1] + pad and
            mn[2] - pad <= loc.z <= mx[2] + pad)


def _principled(mat):
    if mat is None or not mat.use_nodes:
        return None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def _set_bsdf(mat, color=None, roughness=None):
    node = _principled(mat)
    if node is None:
        return False
    if color is not None:
        sock = node.inputs.get("Base Color") or node.inputs.get("Base Color") 
        if sock:
            sock.default_value = tuple(color if len(color) == 4 else list(color) + [1.0])
    if roughness is not None and "Roughness" in node.inputs:
        node.inputs["Roughness"].default_value = float(roughness)
    return True


def _shift_color(mat, warm=0.0, lighten=0.0):
    node = _principled(mat)
    if node is None:
        return False
    sock = node.inputs.get("Base Color")
    if sock is None:
        return False
    r, g, b, a = list(sock.default_value)
    if warm:
        r = min(1.0, r + 0.08 * warm)
        b = max(0.0, b - 0.06 * warm)
        g = min(1.0, g + 0.02 * warm)
    if lighten:
        r = min(1.0, r + (1.0 - r) * lighten)
        g = min(1.0, g + (1.0 - g) * lighten)
        b = min(1.0, b + (1.0 - b) * lighten)
    sock.default_value = (r, g, b, a)
    return True


def _edits_from_suggestions(suggestions, result, target, room_name, context, mapping_src_names):
    """Map hunch suggestion labels to concrete, applicable scene edits."""
    edits = []
    seen = set()

    def add(edit):
        key = (edit["kind"], edit.get("detail") or edit.get("reason"))
        if key in seen:
            return
        seen.add(key)
        edit["id"] = f"e{len(edits) + 1}"
        edits.append(edit)

    for sug in suggestions:
        change = (sug.get("change") or "").strip()
        why = sug.get("explanation") or change
        room = sug.get("room") or room_name
        m = re.search(r"raise window-to-wall from (\d+)% to (\d+)%", change)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            scale = min(1.35, max(1.04, (b / 100) / max(a / 100, 0.05)))
            add({"kind": "glazing_scale", "room": room, "scale": scale,
                 "detail": change, "reason": why})
            continue
        m = re.search(r"reduce window-to-wall from (\d+)% to (\d+)%", change)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            # area scale: hunch's own step is timid (~44%→34%). for calm/cozy
            # retarget toward ~20% WWR, still a plausible reduction of openings.
            if target in ("calm", "cozy"):
                goal = min(b, 20)
                area_scale = max(0.35, min(0.85, (goal / 100) / max(a / 100, 0.05)))
            else:
                area_scale = max(0.55, min(0.96, (b / 100) / max(a / 100, 0.05)))
            add({"kind": "glazing_scale", "room": room, "area_scale": area_scale, "inset": 0.15 if target in ("calm", "cozy") else 0.0,
                 "detail": f"reduce window-to-wall from {a}% toward {int(round(a * area_scale))}% and set the glazing deeper",
                 "reason": why + "  retargeted so the opening area actually moves the window-to-wall feature."})
            continue
        m = re.search(r"raise the ceiling from ([\d.]+) m to ([\d.]+) m", change)
        if m:
            delta = float(m.group(2)) - float(m.group(1))
            add({"kind": "ceiling_height", "room": room, "delta": min(0.8, max(0.0, delta)),
                 "detail": change, "reason": why})
            continue
        m = re.search(r"lower the ceiling from ([\d.]+) m to ([\d.]+) m", change)
        if m:
            new_h = float(m.group(2))
            old_h = float(m.group(1))
            # a soffit at ~2.7 m in a tall loft is a plausible calm move; hunch's
            # own suggestion (often only -0.4 m) barely moves the open term.
            if target in ("calm", "cozy"):
                new_h = min(new_h, 2.7)
            if new_h >= 2.2:
                add({"kind": "ceiling_height", "room": room, "delta": new_h - old_h,
                     "target_height": new_h, "add_soffit": True,
                     "detail": f"lower the ceiling / add a soffit from {old_h:.2f} m to {new_h:.2f} m",
                     "reason": why + "  retargeted: a soffit is added so the scored ceiling height actually changes."})
            continue
        if "warmer timber" in change:
            add({"kind": "material_preset", "room": room, "preset": "timber",
                 "detail": change, "reason": why})
            continue
        if "lighter plaster" in change:
            add({"kind": "material_preset", "room": room, "preset": "plaster",
                 "detail": change, "reason": why})
            continue
        if "warm the surface colour" in change:
            if target == "calm":
                add({"kind": "material_preset", "room": room, "preset": "calm_surfaces",
                     "detail": "warm and mid-darken the surfaces (mid-value plaster, not timber)",
                     "reason": why + "  retargeted to a mid-value warm plaster so colour and material warmth reach hunch without a timber_share cozy overshoot."})
            elif target == "cozy":
                add({"kind": "material_preset", "room": room, "preset": "timber",
                     "detail": "warm the surfaces with timber",
                     "reason": why})
            else:
                add({"kind": "material_shift", "room": room, "warm": 1.0, "lighten": 0.0,
                     "detail": change, "reason": why})
            continue
        if "cool and lighten" in change:
            add({"kind": "material_shift", "room": room, "warm": -0.6, "lighten": 0.35,
                 "detail": change, "reason": why})
            continue
        m = re.search(r"add a ([\d.]+) m overhang", change)
        if m:
            add({"kind": "overhang", "room": room, "depth": float(m.group(1)),
                 "detail": change, "reason": why})
            continue
        if "deepen the overhang" in change:
            d = 1.2
            m = re.search(r"to ([\d.]+) m", change)
            if m:
                d = float(m.group(1))
            add({"kind": "overhang", "room": room, "depth": d,
                 "detail": change, "reason": why})
            continue
        if "shift more glazing toward the south" in change:
            add({"kind": "glazing_bias", "room": room, "direction": "south", "scale": 1.12,
                 "detail": change, "reason": why})
            continue
        if "shift more glazing toward the north" in change:
            add({"kind": "glazing_bias", "room": room, "direction": "north", "scale": 1.10,
                 "detail": change, "reason": why})
            continue

    # supporting louver, partition, lighting edits, clearly marked as added by the loop
    feel = ((result.get("focus") or {}).get("atmosphere") or {}).get("predicted_feel") or ""
    if any(n.startswith("louver-tilt") for n in mapping_src_names):
        if target in ("calm", "cozy") or any("overhang" in (e.get("detail") or "") or "reduce window" in (e.get("detail") or "") or "soffit" in (e.get("detail") or "") for e in edits):
            add({"kind": "louver_angle", "degrees": 80.0,
                 "detail": "close the louvers to cut view and daylight through the roof",
                 "reason": f"supporting edit: toward {target}, from a predicted read of {feel or 'unscored'}. closed slats reduce the effective opening area hunch reads."})
        elif target in ("open", "energized"):
            add({"kind": "louver_angle", "degrees": 18.0,
                 "detail": "open the louvers",
                 "reason": f"supporting edit: toward {target}, from a predicted read of {feel or 'unscored'}"})
    if target in ("calm", "cozy"):
        add({"kind": "partition", "room": room, "height": 2.2, "thickness": 0.12,
             "detail": "add a timber partition in the loft to add wall area and reduce openness",
             "reason": f"supporting edit: toward {target}. extra wall area lowers window-to-wall; a partition is a plausible interior move."})
        add({"kind": "light_energy", "sun_scale": 0.7, "add_area": True, "color": (1.0, 0.78, 0.55), "energy": 40.0,
             "detail": "soften and warm the light",
             "reason": f"supporting edit: toward {target} (viewport/render; hunch scores daylight from geometry and sun, not blender lamps)."})
    elif target in ("open", "energized"):
        add({"kind": "light_energy", "sun_scale": 1.12, "add_area": True, "color": (1.0, 0.97, 0.92), "energy": 140.0,
             "detail": "brighten the room",
             "reason": f"supporting edit: toward {target}"})
    # wink-shaped picks for movable fittings (door, fan, lights, furniture, arch)
    for act in wink_pick_actions(target, result):
        add(act)

    # drop conflicting later suggestions when a calm pack is already planned
    if target == "calm":
        has_soffit = any(
            e.get("kind") == "ceiling_height" or "soffit" in (e.get("detail") or "") or "lower the ceiling" in (e.get("detail") or "")
            for e in edits
        )
        material_kept = False
        cleaned = []
        for e in edits:
            det = (e.get("detail") or "").lower()
            if has_soffit and "raise the ceiling" in det:
                continue
            if e.get("kind") in ("material_preset", "material_shift"):
                if material_kept:
                    continue
                # one mid-value non-timber finish
                e = dict(e)
                e["kind"] = "material_preset"
                e["preset"] = "calm_surfaces"
                e["detail"] = "warm, mid-value surfaces (plaster, not timber)"
                e["reason"] = (e.get("reason") or "") + "  single calm finish; timber kind skipped so cozy does not overtake calm."
                material_kept = True
            cleaned.append(e)
        edits = cleaned
    # cap after the strongest kinds; prefer ones that move scored features
    return edits[:16]


def _mesh_objects(mapping, room_mn=None, room_mx=None, predicate=None):
    out = []
    for orig, new in mapping.items():
        if new.type != "MESH":
            continue
        if predicate and not predicate(orig, new):
            continue
        if room_mn and room_mx and not _centroid_in(orig, room_mn, room_mx, pad=1.5):
            # use original position to decide room membership (pad is generous:
            # glazing centroids can sit just outside the shell AABB)
            continue
        out.append((orig, new))
    return out



def _tag_hunch(ob, room_name, kind=None, include=True, role=None):
    if include:
        ob["hunch_include"] = True
    if room_name:
        ob["hunch_room"] = room_name
    if kind:
        ob["hunch_kind"] = kind
    if role:
        ob["hunch_role"] = role
    return ob


def _paint_material(mat, preset):
    if mat is None:
        return False
    color = preset.get("color")
    ok = _set_bsdf(mat, color=color, roughness=preset.get("roughness"))
    if ok:
        try:
            if preset.get("kind"):
                mat["hunch_kind"] = preset["kind"]
            if preset.get("warmth") is not None:
                mat["hunch_warmth"] = float(preset["warmth"])
        except Exception:
            pass
    return ok


def _scale_inplane(obj, area_scale):
    """Scale an object so its world face area changes by area_scale, regardless of pane orientation."""
    lin = math.sqrt(max(0.2, min(1.6, float(area_scale))))
    # uniform local scale: area of any face scales by lin^2
    obj.scale.x *= lin
    obj.scale.y *= lin
    obj.scale.z *= lin
    return lin


def _inset_toward(obj, room_mn, room_mx, depth):
    """Move a pane toward the room centre (deeper-set opening)."""
    if not room_mn or not room_mx or depth <= 0:
        return False
    mid = Vector(((room_mn[0] + room_mx[0]) / 2.0, (room_mn[1] + room_mx[1]) / 2.0, (room_mn[2] + room_mx[2]) / 2.0))
    loc = obj.matrix_world.translation
    delta = mid - loc
    delta.z = 0.0
    if delta.length < 1e-4:
        return False
    step = delta.normalized() * float(depth)
    # do not pass the centre
    if step.length > delta.length * 0.45:
        step = delta * 0.45
    mw = obj.matrix_world.copy()
    mw.translation += step
    obj.matrix_world = mw
    return True


def _add_box(root, name, mn, mx, room_name, kind, preset=None, role=None):
    x0, y0, z0 = mn
    x1, y1, z1 = mx
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    # inward-ish top (down) as ceiling if kind is ceiling: we'll set hunch_kind explicitly
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    mesh = bpy.data.meshes.new(name + "-mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    ob = bpy.data.objects.new(name, mesh)
    root.objects.link(ob)
    _tag_hunch(ob, room_name, kind=kind, role=role or kind)
    if preset:
        mat = bpy.data.materials.new(name + "-mat")
        mat.use_nodes = True
        _paint_material(mat, preset)
        mesh.materials.append(mat)
    return ob



def _follow_parts(root, lead):
    """Objects that should move with a fitting (couch parts, etc.)."""
    src = (lead.get("feel_loop_src") or lead.name or "").split(".", 1)[0]
    tid = str(lead.get("tinker_id") or "").split("@", 1)[0]
    out = []
    for ob in walk_collection(root):
        if ob == lead:
            continue
        if ob.get("tinker_follow") in {lead.name, src, tid, lead.get("feel_loop_src")}:
            out.append(ob)
        elif (ob.get("tinker_follow") or "") and str(ob.get("tinker_follow")) in (lead.name or ""):
            out.append(ob)
    return out


def _set_joint_value(ob, value, root=None):
    """Pose a tinker joint on a variant object and any follow-parts."""
    jd = dict(ob.get("tinker_joint") or {})
    if not jd:
        return False
    old = float(jd.get("value") or 0.0)
    lo = float(jd.get("lower") or 0.0)
    hi = float(jd.get("upper") or old)
    value = max(lo, min(hi, float(value)))
    delta = value - old
    kind = jd.get("kind") or "revolute"
    axis = list(jd.get("axis") or [0, 0, 1])
    if kind == "prismatic":
        vec = Vector(axis).normalized() * delta
        for item in [ob] + (_follow_parts(root, ob) if root is not None else []):
            item.location = item.location + vec
    else:
        # revolute around the object's origin
        import mathutils
        n = Vector(axis).normalized()
        rot = mathutils.Matrix.Rotation(delta, 4, n)
        mw = ob.matrix_world.copy()
        # rotate around origin (object location)
        origin = mw.to_translation()
        ob.matrix_world = Matrix.Translation(origin) @ rot @ Matrix.Translation(-origin) @ mw
        for item in (_follow_parts(root, ob) if root is not None else []):
            imw = item.matrix_world.copy()
            item.matrix_world = Matrix.Translation(origin) @ rot @ Matrix.Translation(-origin) @ imw
    jd["value"] = value
    ob["tinker_joint"] = jd
    return True


def _tokens(s):
    return " " + " ".join(str(s or "").lower().replace("_", " ").replace("-", " ").split()) + " "


def _name_matches(blob, *needles):
    hay = _tokens(blob)
    for n in needles:
        needle = _tokens(n).strip()
        if needle and f" {needle} " in hay:
            return True
    return False


def _find_in_root(root, *names):
    found = []
    for ob in walk_collection(root):
        blob = (ob.name or "") + " " + str(ob.get("feel_loop_src") or "") + " " + str(ob.get("tinker_id") or "")
        if _name_matches(blob, *names):
            found.append(ob)
    return found


def wink_pick_actions(target, result=None):
    """Pick tinker actions on movable fittings for the target feel (mood).

    Authored mapping, not a trained wink policy and not wink.plan. wink.plan
    still handles a learned louver skill when one exists. These actions are
    applied on the variant only.
    """
    target = (target or "calm").lower()
    acts = []
    if target in ("calm", "cozy"):
        acts.append({"kind": "door_angle", "radians": 0.18,
                     "detail": "leave the deck door almost closed",
                     "reason": f"toward {target}: a quieter threshold on the deck."})
        acts.append({"kind": "arch_screen", "radians": 0.15,
                     "detail": "close the exterior arch screen",
                     "reason": f"toward {target}: the arch reads as a shelter, not a wide opening."})
        acts.append({"kind": "fan_speed", "on": 1, "speed": 0.35, "name": "fan loft",
                     "detail": "turn the loft fan on, slowly",
                     "reason": f"toward {target}: a slow fan is for the rendered experience; hunch does not score airflow."})
        acts.append({"kind": "light_state", "on": 1, "energy": 120.0, "temp_k": 2700,
                     "detail": "dim and warm the interior lamps",
                     "reason": f"toward {target}: warmer, lower light in the stills hunch may read."})
        acts.append({"kind": "furniture_slide", "couch": 0.35, "table": 0.2,
                     "detail": "draw the couch and table into a closer grouping",
                     "reason": f"toward {target}: furniture is moved on the variant; hunch skips furniture faces as furnishings."})
    elif target in ("open", "energized"):
        acts.append({"kind": "door_angle", "radians": 1.2,
                     "detail": "open the deck door",
                     "reason": f"toward {target}: a clear way out to the deck."})
        acts.append({"kind": "arch_screen", "radians": 1.4,
                     "detail": "open the exterior arch screen",
                     "reason": f"toward {target}: the arch is a clear opening."})
        acts.append({"kind": "fan_speed", "on": 1 if target == "energized" else 0, "speed": 0.7 if target == "energized" else 0.0,
                     "name": "fan loft",
                     "detail": "run the loft fan faster" if target == "energized" else "leave the loft fan off",
                     "reason": f"toward {target} (render only; hunch does not score airflow)."})
        acts.append({"kind": "light_state", "on": 1, "energy": 320.0, "temp_k": 4200,
                     "detail": "brighten the interior lamps, cooler",
                     "reason": f"toward {target}."})
        acts.append({"kind": "furniture_slide", "couch": -0.7, "table": 0.7,
                     "detail": "slide the couch and table aside to free the middle of the loft",
                     "reason": f"toward {target}."})
    else:
        acts.append({"kind": "door_angle", "radians": 0.6,
                     "detail": "set the deck door half open",
                     "reason": f"a middle setting toward {target}."})
        acts.append({"kind": "light_state", "on": 1, "energy": 200.0, "temp_k": 3200,
                     "detail": "keep the interior lamps on, mid-warm",
                     "reason": f"toward {target}."})
    for a in acts:
        a["source"] = "wink_pick_actions"
    return acts

def apply_edits(mapping, edits, result, room_name, root, dx):
    applied = []
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    room_mn, room_mx = _room_bounds(result, room_name, mapping)
    snap = _text_json("hunch_last_snapshot.json") or {}
    if room_mn is None:
        for r in snap.get("rooms3d") or []:
            if (r.get("name") or "").lower() == (room_name or "").lower():
                room_mn, room_mx = list(r["min"]), list(r["max"])
                break

    def pairs_for(predicate, require_room=True):
        room = (room_mn, room_mx) if require_room else (None, None)
        found = _mesh_objects(mapping, room[0], room[1], predicate=predicate)
        if not found and require_room:
            found = _mesh_objects(mapping, None, None, predicate=predicate)
        return found

    def is_non_glass(o, n):
        return (not HS.is_glazing(o)) and "louver" not in o.name.lower()

    def is_glass(o, n):
        return HS.is_glazing(o)

    for ed in edits:
        kind = ed["kind"]
        try:
            n = 0
            if kind in ("material_preset", "material_shift"):
                found = pairs_for(is_non_glass, require_room=True)
                # always include the room shell even if the AABB missed it
                for orig, new in mapping.items():
                    if new.type != "MESH":
                        continue
                    if HS.is_room_shell(orig) or _room_name_of(orig).lower() == (room_name or "").lower():
                        if (orig, new) not in found and is_non_glass(orig, new):
                            found.append((orig, new))
                for orig, new in found:
                    mats = list(new.data.materials) if new.data else []
                    if not mats and new.active_material:
                        mats = [new.active_material]
                    if kind == "material_preset":
                        preset = PRESETS.get(ed.get("preset") or "calm_surfaces") or PRESETS["calm_surfaces"]
                        if not mats:
                            mat = bpy.data.materials.new(new.name + "-finish")
                            mat.use_nodes = True
                            new.data.materials.append(mat)
                            mats = [mat]
                        for mat in mats:
                            if _paint_material(mat, preset):
                                n += 1
                    else:
                        for mat in mats:
                            if _shift_color(mat, warm=ed.get("warm") or 0, lighten=ed.get("lighten") or 0):
                                n += 1
                applied.append({**ed, "applied_to": n})
            elif kind in ("glazing_scale", "glazing_bias"):
                found = pairs_for(is_glass, require_room=True)
                if not found:
                    found = pairs_for(is_glass, require_room=False)
                area_scale = float(ed.get("area_scale") or ed.get("scale") or 1.0)
                # historical 'scale' was a linear factor on x/z only; treat values
                # close to 1 as linear, otherwise as an area ratio when area_scale set.
                if "area_scale" in ed:
                    pass
                else:
                    # old linear x/z scale: convert to an area scale of scale^2
                    area_scale = max(0.2, float(ed.get("scale") or 1.0) ** 2)
                inset = float(ed.get("inset") or 0.0)
                for orig, new in found:
                    _scale_inplane(new, area_scale)
                    if inset:
                        _inset_toward(new, room_mn, room_mx, inset)
                    n += 1
                applied.append({**ed, "applied_to": n, "area_scale": area_scale})
            elif kind == "ceiling_height":
                delta = float(ed.get("delta") or 0)
                target_h = ed.get("target_height")
                if target_h is None and room_mn and room_mx and delta:
                    target_h = max(2.2, (room_mx[2] - room_mn[2]) + delta)
                if target_h is None and delta == 0:
                    applied.append({**ed, "applied_to": 0, "skipped": "zero delta"})
                    continue
                # lower / raise the room shell top so rooms3d height changes
                for orig, new in mapping.items():
                    if new.type != "MESH":
                        continue
                    is_shell = HS.is_room_shell(orig) or orig.get("hunch_room_shell") or orig.get("hunch_kind") == "room"
                    name = orig.name.lower()
                    is_ceil = any(w in name for w in ("ceil", "roof", "soffit")) or orig.get("hunch_kind") == "ceiling"
                    in_room = (_room_name_of(orig).lower() == (room_name or "").lower()
                               or (room_mn and room_mx and _centroid_in(orig, room_mn, room_mx, pad=1.5)))
                    if not ((is_shell and in_room) or (is_ceil and in_room)):
                        continue
                    if not is_shell and not is_ceil:
                        continue
                    mesh = new.data
                    mw = new.matrix_world
                    imw = mw.inverted()
                    zs = [(mw @ v.co).z for v in mesh.vertices]
                    if not zs:
                        continue
                    zmax = max(zs)
                    zmin = min(zs)
                    if target_h is not None and is_shell:
                        new_top = zmin + float(target_h)
                        cut = zmin + 0.4
                        for v in mesh.vertices:
                            wp = mw @ v.co
                            if wp.z >= cut:
                                wp.z = new_top if wp.z >= (zmax - 0.25) else min(wp.z, new_top)
                                v.co = imw @ wp
                    else:
                        cut = zmax - 0.2 if is_shell else zmax + 1.0
                        for v in mesh.vertices:
                            wp = mw @ v.co
                            if wp.z >= cut or is_ceil:
                                wp.z += delta
                                v.co = imw @ wp
                    mesh.update()
                    n += 1
                # add a named soffit so _plan_heights sees a ceiling face at the target height
                if ed.get("add_soffit") and room_mn and room_mx and target_h is not None:
                    z0 = float(room_mn[2]) + float(target_h) - 0.04
                    z1 = z0 + 0.04
                    pad = 0.02
                    # room_mn/mx are from the original shell; the variant is offset by dx on X
                    soff = _add_box(
                        root, f"{root.name} soffit",
                        (room_mn[0] + dx + pad, room_mn[1] + pad, z0),
                        (room_mx[0] + dx - pad, room_mx[1] - pad, z1),
                        room_name, "ceiling",
                        PRESETS.get("calm_surfaces") or PRESETS.get("plaster"),
                        role="soffit",
                    )
                    n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "louver_angle":
                deg = float(ed.get("degrees") or 45)
                rad = math.radians(deg)
                for orig, new in mapping.items():
                    if not orig.name.startswith("louver-tilt") or not orig.get("tinker_joint"):
                        continue
                    jd = dict(new.get("tinker_joint") or {})
                    upper = float(jd.get("upper") or 1.448623279)
                    lower = float(jd.get("lower") or 0.0)
                    rad_c = max(lower, min(upper, rad))
                    jd["value"] = rad_c
                    new["tinker_joint"] = jd
                    try:
                        old = float(dict(orig.get("tinker_joint") or {}).get("value") or 0)
                        new.rotation_mode = "XYZ"
                        e = new.rotation_euler.copy()
                        e.x += (rad_c - old)
                        new.rotation_euler = e
                        n += 1
                    except Exception:
                        n += 0
                applied.append({**ed, "applied_to": n})
            elif kind == "light_energy":
                for orig, new in mapping.items():
                    if new.type == "LIGHT":
                        new.data.energy = float(new.data.energy) * float(ed.get("sun_scale") or 1.0)
                        if ed.get("color") and hasattr(new.data, "color"):
                            try:
                                new.data.color = tuple(ed["color"])[:3]
                            except Exception:
                                pass
                        n += 1
                if ed.get("add_area") and room_mn and room_mx:
                    lamp = bpy.data.lights.new(root.name + " area", "AREA")
                    lamp.energy = float(ed.get("energy") or 100)
                    lamp.size = 1.2
                    lamp.color = tuple(ed.get("color") or (1.0, 0.95, 0.85))
                    ob = bpy.data.objects.new(root.name + " area", lamp)
                    cx = (room_mn[0] + room_mx[0]) / 2.0 + dx
                    cy = (room_mn[1] + room_mx[1]) / 2.0
                    cz = (room_mx[2] if room_mx else 2.4) - 0.15
                    if ed.get("target_height"):
                        cz = room_mn[2] + float(ed["target_height"]) - 0.1
                    ob.location = (cx, cy, cz)
                    ob.rotation_euler = (0, 0, 0)
                    root.objects.link(ob)
                    n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "overhang":
                depth = float(ed.get("depth") or 0.6)
                found = pairs_for(is_glass, require_room=True)
                if not found:
                    found = pairs_for(is_glass, require_room=False)
                for orig, new in found:
                    try:
                        corners = [new.matrix_world @ Vector(c) for c in new.bound_box]
                        xs, ys, zs = [p.x for p in corners], [p.y for p in corners], [p.z for p in corners]
                        mn, mx = Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))
                    except Exception:
                        continue
                    w, d, h = max(0.3, mx.x - mn.x), depth, 0.06
                    mid = (mn + mx) / 2.0
                    mesh = bpy.data.meshes.new(new.name + " overhang")
                    hx, hy, hz = w / 2, d / 2, h / 2
                    verts = [
                        (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
                        (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
                    ]
                    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 3, 7, 4)]
                    mesh.from_pydata(verts, [], faces)
                    mesh.update()
                    ob = bpy.data.objects.new(new.name + " overhang", mesh)
                    out_y = -1 if (mid.y - (room_mn[1] if room_mn else mid.y)) < ((room_mx[1] if room_mx else mid.y) - mid.y) else 1
                    ob.location = (mid.x, mid.y + out_y * (depth / 2 + 0.05), mx.z + 0.08)
                    root.objects.link(ob)
                    _tag_hunch(ob, room_name, kind="shade", role="overhang")
                    n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "partition":
                if not (room_mn and room_mx):
                    applied.append({**ed, "applied_to": 0, "skipped": "no room bounds"})
                    continue
                height = float(ed.get("height") or 2.2)
                thick = float(ed.get("thickness") or 0.12)
                # a wall across the shorter plan direction, with a 1.0 m opening at one end
                span_x = room_mx[0] - room_mn[0]
                span_y = room_mx[1] - room_mn[1]
                z0 = room_mn[2]
                z1 = z0 + min(height, max(1.8, (room_mx[2] - room_mn[2]) - 0.2))
                if span_x >= span_y:
                    y = (room_mn[1] + room_mx[1]) / 2.0
                    x0 = room_mn[0] + 1.0  # leave a passage
                    x1 = room_mx[0] - 0.1
                    mn, mx = (x0, y - thick / 2, z0), (x1, y + thick / 2, z1)
                else:
                    x = (room_mn[0] + room_mx[0]) / 2.0
                    y0 = room_mn[1] + 1.0
                    y1 = room_mx[1] - 0.1
                    mn, mx = (x - thick / 2, y0, z0), (x + thick / 2, y1, z1)
                # variant is already offset by dx on world X; room_mn is from the original
                mn = (mn[0] + dx, mn[1], mn[2])
                mx = (mx[0] + dx, mx[1], mx[2])
                _add_box(root, f"{root.name} partition", mn, mx, room_name, "wall",
                         PRESETS.get("calm_surfaces") or PRESETS.get("plaster"), role="partition")
                n = 1
                applied.append({**ed, "applied_to": n})
            elif kind == "door_angle":
                found = _find_in_root(root, "door")
                rad = float(ed.get("radians") or 0.0)
                for ob in found:
                    if ob.get("tinker_joint") and _set_joint_value(ob, rad, root):
                        n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "arch_screen":
                found = _find_in_root(root, "arch screen", "arch-screen")
                rad = float(ed.get("radians") or 0.0)
                for ob in found:
                    if ob.get("tinker_joint") and _set_joint_value(ob, rad, root):
                        n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "fan_speed":
                want = (ed.get("name") or "fan").lower()
                found = [ob for ob in _find_in_root(root, "fan") if "rod" not in ob.name.lower() and want in (ob.name + " " + str(ob.get("feel_loop_src") or "")).lower()]
                if not found:
                    found = [ob for ob in _find_in_root(root, "fan loft", "fan-loft", "fan") if "rod" not in ob.name.lower()]
                on = 1 if ed.get("on", 1) else 0
                speed = float(ed.get("speed") or 0.0)
                for ob in found:
                    ob["tinker_on"] = on
                    ob["tinker_speed"] = speed
                    # pose the blades slightly so an 'on' fan is not identical to off in a still
                    pose = 0.6 if on else 0.0
                    if ob.get("tinker_joint"):
                        _set_joint_value(ob, pose, root)
                    n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "light_state":
                found = _find_in_root(root, "lamp")
                on = bool(ed.get("on", 1))
                energy = ed.get("energy")
                temp = ed.get("temp_k")
                for ob in found:
                    if ob.type == "LIGHT":
                        if temp is not None or energy is not None or "tinker_on" in (ob.keys() if hasattr(ob, "keys") else []):
                            try:
                                from . import scene_look as SL
                                # use fittings kelvin if present
                            except Exception:
                                pass
                        if temp is not None:
                            ob["tinker_temp_k"] = float(temp)
                            # rough kelvin
                            tK = float(temp)
                            # simple warm/cool mix from kelvin
                            u = max(0.0, min(1.0, (tK - 2000.0) / 5000.0))
                            ob.data.color = (1.0, 0.55 + 0.4 * u, 0.25 + 0.7 * u)
                        if energy is not None:
                            ob["tinker_energy"] = float(energy)
                        ob["tinker_on"] = 1 if on else 0
                        base = float(ob.get("tinker_energy") or ob.data.energy or 0)
                        ob.data.energy = base if on else 0.0
                        n += 1
                    elif energy is not None or temp is not None:
                        if energy is not None:
                            ob["tinker_energy"] = float(energy)
                        if temp is not None:
                            ob["tinker_temp_k"] = float(temp)
                        ob["tinker_on"] = 1 if on else 0
                        n += 1
                applied.append({**ed, "applied_to": n})
            elif kind == "furniture_slide":
                for key, val in (("couch", ed.get("couch")), ("table", ed.get("table"))):
                    if val is None:
                        continue
                    found = _find_in_root(root, key, key.replace(" ", "-"))
                    leads = [ob for ob in found if ob.get("tinker_joint")]
                    for ob in leads:
                        if _set_joint_value(ob, float(val), root):
                            n += 1
                applied.append({**ed, "applied_to": n})
            else:
                applied.append({**ed, "applied_to": 0, "skipped": "unknown kind"})
        except Exception as exc:
            applied.append({**ed, "applied_to": 0, "error": str(exc)[:200]})
    return applied


def next_dx(context):
    objs = [o for o in context.scene.objects if o.type in {"MESH", "FONT", "EMPTY", "CAMERA"}]
    mn, mx = world_bbox(objs)
    src = source_objects(context)
    smn, smx = world_bbox(src)
    width = max(1.0, smx.x - smn.x)
    # place the new copy so its min X is current scene max X + gap
    # source min is smn.x; after offset, new_min = smn.x + dx = mx.x + GAP
    return (mx.x + GAP_M) - smn.x, width


def matching_camera(mapping, context):
    st = _st(context)
    src = None
    if st.viewpoint is not None and st.viewpoint.type == "CAMERA":
        src = st.viewpoint
    elif context.scene.camera is not None and not in_variant(context.scene.camera):
        src = context.scene.camera
    if src is not None and src in mapping:
        return mapping[src]
    # any copied camera
    for orig, new in mapping.items():
        if new.type == "CAMERA":
            return new
    return None


def _engine_id():
    try:
        items = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
        ids = [i.identifier for i in items]
    except Exception:
        ids = []
    if "BLENDER_EEVEE_NEXT" in ids:
        return "BLENDER_EEVEE_NEXT"
    if "BLENDER_EEVEE" in ids:
        return "BLENDER_EEVEE"
    return "CYCLES"


def render_view(context, camera, path, samples=32, hide=None, show=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = context.scene
    prev_cam = scene.camera
    prev_eng = scene.render.engine
    prev_path = scene.render.filepath
    prev_fmt = scene.render.image_settings.file_format
    prev_resx, prev_resy = scene.render.resolution_x, scene.render.resolution_y
    hidden = []
    try:
        if hide:
            for o in hide:
                hidden.append((o, o.hide_render))
                o.hide_render = True
        if show:
            for o in show:
                o.hide_render = False
        if camera is not None:
            scene.camera = camera
        scene.render.engine = _engine_id()
        scene.render.resolution_x = 960
        scene.render.resolution_y = 720
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(path)
        # samples
        if scene.render.engine == "CYCLES":
            scene.cycles.samples = int(samples)
        else:
            # eevee
            ee = getattr(scene, "eevee", None)
            if ee is not None and hasattr(ee, "taa_render_samples"):
                ee.taa_render_samples = int(samples)
        bpy.ops.render.render(write_still=True)
    finally:
        for o, h in hidden:
            o.hide_render = h
        scene.camera = prev_cam
        scene.render.engine = prev_eng
        scene.render.filepath = prev_path
        scene.render.image_settings.file_format = prev_fmt
        scene.render.resolution_x, scene.render.resolution_y = prev_resx, prev_resy
    return str(path) if path.is_file() else None


def side_by_side(before, after, out, caption=""):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        py = os.environ.get("HUNCH_PYTHON") or os.path.expanduser("~/.tingle/venv/bin/python")
        if Path(py).is_file() and before and after:
            script = (
                "from PIL import Image, ImageDraw, ImageFont; import sys\n"
                "before, after, out, caption = sys.argv[1:5]\n"
                "a, b = Image.open(before).convert('RGB'), Image.open(after).convert('RGB')\n"
                "h = max(a.height, b.height)\n"
                "def fit(im):\n"
                "    return im if im.height == h else im.resize((int(im.width*h/im.height), h))\n"
                "a, b = fit(a), fit(b)\n"
                "pad, bar = 16, 48\n"
                "canvas = Image.new('RGB', (a.width+b.width+pad*3, h+bar+pad*2), (40,40,38))\n"
                "canvas.paste(a, (pad, pad+bar)); canvas.paste(b, (pad*2+a.width, pad+bar))\n"
                "d = ImageDraw.Draw(canvas)\n"
                "d.text((pad, 10), 'before (original)'); d.text((pad*2+a.width, 10), 'after (variant)')\n"
                "if caption: d.text((pad, h+bar+pad-2), caption[:160])\n"
                "canvas.save(out)\n"
            )
            r = subprocess.run([py, "-c", script, str(before), str(after), str(out), caption or ""], capture_output=True, text=True)
            return str(out) if Path(out).is_file() else None
        return None
    if not (before and after and Path(before).is_file() and Path(after).is_file()):
        return None
    a, b = Image.open(before).convert("RGB"), Image.open(after).convert("RGB")
    h = max(a.height, b.height)
    def fit(im):
        if im.height == h:
            return im
        w = int(im.width * h / im.height)
        return im.resize((w, h))
    a, b = fit(a), fit(b)
    pad, bar = 16, 48
    canvas = Image.new("RGB", (a.width + b.width + pad * 3, h + bar + pad * 2), (40, 40, 38))
    canvas.paste(a, (pad, pad + bar))
    canvas.paste(b, (pad * 2 + a.width, pad + bar))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
        small = ImageFont.truetype("DejaVuSans.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
        small = font
    draw.text((pad, 10), "before (original)", fill=(230, 228, 220), font=font)
    draw.text((pad * 2 + a.width, 10), "after (variant)", fill=(230, 228, 220), font=font)
    if caption:
        draw.text((pad, h + bar + pad - 2), caption[:160], fill=(180, 178, 170), font=small)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return str(out)


def write_changelog(path, log):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("feel loop change log")
    lines.append("hunch predicts how a space tends to feel. it does not read a person.")
    lines.append("edits are a mapping from hunch suggestions onto this blender scene, not a trained policy that outputs geometry.")
    lines.append("")
    lines.append(f"target: {log.get('target')}")
    lines.append(f"room: {log.get('room')}")
    lines.append(f"iterations: {log.get('iterations_run')}")
    lines.append(f"target reached: {log.get('reached')}")
    lines.append(f"start predicted feel: {log.get('start_feel')}")
    lines.append(f"end predicted feel: {log.get('end_feel')}")
    lines.append("")
    for i, it in enumerate(log.get("iterations") or [], 1):
        lines.append(f"iteration {i}: collection {it.get('collection')}")
        lines.append(f"  predicted feel: {it.get('feel')}  (before this step: {it.get('feel_before')})")
        scores = it.get("scores") or {}
        if scores:
            bits = ", ".join(f"{k} {v}" for k, v in list(scores.items())[:7])
            lines.append(f"  scores: {bits}")
        fb, fa = it.get("features_before") or {}, it.get("features_after") or {}
        if fa or fb:
            def _fmt(d):
                if not d:
                    return "n/a"
                wwr = d.get("window_to_wall")
                wwr_s = f"{100*wwr:.1f}%" if isinstance(wwr, (int, float)) else "?"
                ch = d.get("ceiling_height")
                ch_s = f"{ch:.2f} m" if isinstance(ch, (int, float)) else "?"
                return f"wwr {wwr_s}, ceiling {ch_s}, mat warmth {d.get('material_warmth')}, colour warmth {d.get('colour_warmth')}, value {d.get('colour_value')}"
            lines.append(f"  features before: {_fmt(fb)}")
            lines.append(f"  features after:  {_fmt(fa)}")
        for ed in it.get("applied") or []:
            lines.append(f"  - {ed.get('kind')}: {ed.get('detail') or ''}  ({ed.get('applied_to', 0)} object(s))")
            if ed.get("reason"):
                lines.append(f"    reason: {ed['reason']}")
        lines.append("")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(str(path) + ".json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    return str(path)


def export_variant(context, collection_name, out_dir):
    """Write the variant on its own: .blend collection, .obj, .glb, and .3dm if feasible."""
    col = bpy.data.collections.get(collection_name)
    if col is None:
        raise RuntimeError(f"no collection named {collection_name}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", collection_name).strip("-")
    results = {}
    blend = out / f"{stem}.blend"
    bpy.data.libraries.write(str(blend), {col}, fake_user=True)
    results["blend"] = str(blend) if blend.is_file() else None

    objs = [o for o in walk_collection(col) if o.type == "MESH"]
    obj_path = out / f"{stem}.obj"
    results["obj"] = _write_obj(objs, obj_path)

    glb = out / f"{stem}.glb"
    results["glb"] = _try_glb(context, col, glb)

    ddm = out / f"{stem}.3dm"
    results["3dm"] = _try_3dm(objs, ddm)
    return results


def _write_obj(objects, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# feel loop variant", f"# {len(objects)} meshes"]
    v_off = 1
    for obj in objects:
        if obj.type != "MESH" or not obj.data:
            continue
        mesh = obj.data
        mw = obj.matrix_world
        lines.append(f"o {obj.name.replace(' ', '_')}")
        verts = [mw @ v.co for v in mesh.vertices]
        for v in verts:
            lines.append(f"v {v.x:.6f} {v.y:.6f} {v.z:.6f}")
        # triangulate via tessellation if available, else polygon
        mesh.calc_loop_triangles()
        for t in mesh.loop_triangles:
            a, b, c = (i + v_off for i in t.vertices)
            lines.append(f"f {a} {b} {c}")
        v_off += len(verts)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _try_glb(context, col, path: Path):
    try:
        bpy.ops.object.select_all(action="DESELECT")
    except RuntimeError:
        pass
    selected = []
    for o in walk_collection(col):
        try:
            o.select_set(True)
            selected.append(o)
        except RuntimeError:
            pass
    if not selected:
        return None
    try:
        kw = dict(filepath=str(path), export_format="GLB", use_selection=True)
        try:
            bpy.ops.export_scene.gltf(**kw)
        except TypeError:
            kw.pop("export_format", None)
            bpy.ops.export_scene.gltf(filepath=str(path), use_selection=True)
        return str(path) if path.is_file() else None
    except Exception:
        return None
    finally:
        try:
            bpy.ops.object.select_all(action="DESELECT")
        except RuntimeError:
            pass


def _try_3dm(objects, path: Path):
    # dump a simple mesh payload, then build a 3dm with system python + rhino3dm if present
    payload = []
    for obj in objects:
        if obj.type != "MESH" or not obj.data:
            continue
        mesh = obj.data
        mw = obj.matrix_world
        verts = [[(mw @ v.co).x, (mw @ v.co).y, (mw @ v.co).z] for v in mesh.vertices]
        mesh.calc_loop_triangles()
        faces = [list(t.vertices) for t in mesh.loop_triangles]
        payload.append({"name": obj.name, "verts": verts, "faces": faces})
    raw = path.with_suffix(".mesh.json")
    raw.write_text(json.dumps(payload), encoding="utf-8")
    script = f"""
import json, sys
try:
    import rhino3dm as r3d
except ImportError:
    sys.exit(2)
payload = json.loads(open({str(raw)!r}).read())
model = r3d.File3dm()
for item in payload:
    mesh = r3d.Mesh()
    for v in item["verts"]:
        mesh.Vertices.Add(v[0], v[1], v[2])
    for f in item["faces"]:
        if len(f) >= 3:
            mesh.Faces.AddFace(int(f[0]), int(f[1]), int(f[2]))
    mesh.Normals.ComputeNormals()
    model.Objects.AddMesh(mesh)
model.Write({str(path)!r}, 7)
print("wrote", {str(path)!r})
"""
    py = shutil.which("python3") or "python3"
    # prefer a venv that already has rhino3dm
    for cand in (os.path.expanduser("~/.tingle/venv/bin/python"), py):
        if not cand or not Path(cand).exists() and cand.endswith("python"):
            continue
        try:
            r = subprocess.run([cand, "-c", script], capture_output=True, text=True, timeout=120)
        except Exception:
            continue
        if r.returncode == 0 and path.is_file():
            return str(path)
    return None


def read_hunch(context, scope, blocking=True):
    st = _st(context)
    st.scope_collection = scope or "original"
    # refresh rooms in this scope
    rooms = HS.discover_rooms(context, scope=st.scope_collection)
    names = [r["name"] for r in rooms]
    # do not wipe the global room enum if we are reading a variant with suffixed names;
    # prefer the user's selected room name on the original.
    snap = _HP()._snapshot(context)
    if not snap.get("faces"):
        raise RuntimeError("no geometry to read in scope " + (scope or "original"))
    _HP()._run_worker(context, snap, None, True)
    return _text_json(_HP()._RESULT_TEXT if hasattr(_HP(), "_RESULT_TEXT") else "hunch_last_result.json") or json.loads(
        (bpy.data.texts.get("hunch_last_result.json") or bpy.data.texts.new("x")).as_string() or "{}"
    )


def _result():
    t = bpy.data.texts.get("hunch_last_result.json")
    if t is None:
        return {}
    try:
        return json.loads(t.as_string() or "{}")
    except ValueError:
        return {}


def _room_match(name, want):
    a, b = (name or "").lower(), (want or "").lower()
    if not b or a == b:
        return True
    # variant shells may be named "loft.001" but keep hunch_room "loft"
    return a.split(".", 1)[0] == b or b in a or a in b


def _scores(result, room_name):
    for rec in result.get("rooms") or []:
        rd = rec.get("room") or {}
        if _room_match(rd.get("name"), room_name):
            atm = rec.get("atmosphere") or {}
            return atm.get("predicted_feel"), atm.get("scores") or {}, rec
    focus = result.get("focus") or {}
    atm = focus.get("atmosphere") or {}
    return atm.get("predicted_feel"), atm.get("scores") or {}, focus


def _features_brief(rec):
    rd = (rec or {}).get("room") or rec or {}
    if not rd:
        return {}
    sun = rd.get("sun_exposure") or {}
    col = rd.get("colour") or {}
    return {
        "name": rd.get("name"),
        "ceiling_height": rd.get("ceiling_height"),
        "window_to_wall": rd.get("window_to_wall"),
        "opening_area": rd.get("opening_area"),
        "wall_area": rd.get("wall_area"),
        "material_warmth": rd.get("material_warmth"),
        "colour_warmth": col.get("warmth"),
        "colour_value": col.get("value"),
        "temperature_c": rd.get("temperature_c"),
        "sun_mean_exposure": sun.get("mean_exposure"),
    }


def make_proposal(context, result=None):
    st = _st(context)
    result = result or _result()
    room = st.room if st.room not in ("", "none") else ""
    target = st.target
    suggestions = ((result.get("suggestions") or {}).get("suggestions")) or []
    # keep suggestions for the focus room first
    if room:
        focused = [s for s in suggestions if (s.get("room") or "").lower() == room.lower()]
        suggestions = focused or suggestions
    src_names = [o.name for o in source_objects(context)]
    edits = _edits_from_suggestions(suggestions, result, target, room, context, src_names)
    feel, scores, _ = _scores(result, room)
    proposal = {
        "room": room,
        "target": target,
        "feel_now": feel,
        "scores_now": scores,
        "edits": edits,
        "suggestions_used": [s.get("change") for s in suggestions[:6]],
        "note": "these edits are a mapping from hunch suggestions onto this blender scene. they are experimental.",
    }
    _set_text_json(_PROPOSAL, proposal)
    st.loop_waiting = True
    n = len(edits)
    st.status = f"proposal ready: {n} edit(s) toward {target} (now {feel or 'unscored'}). approve to build a variant beside the original."
    return proposal


def _store_snapshot(context):
    st = _st(context)
    snap = _HP()._snapshot(context)
    _set_text_json("hunch_last_snapshot.json", {
        "rooms3d": snap.get("rooms3d") or [],
        "site": snap.get("site"),
        "focus_room": snap.get("focus_room"),
    })
    return snap



def _record_run(context, state):
    """Append a local JSONL row; POST only when share_run is on. Never fails the loop."""
    try:
        from . import tingle_share as TS
        return TS.finish_run(context, state or {})
    except Exception as exc:
        try:
            context.scene.hunch.share_note = ""
        except Exception:
            pass
        return {"logged": False, "posted": False, "status": 0, "error": str(exc)[:200]}


def approve_and_build(context, out_dir=None, record_run=True):
    st = _st(context)
    proposal = _text_json(_PROPOSAL) or {}
    edits = proposal.get("edits") or []
    if not edits:
        raise RuntimeError("no proposal to approve")
    result_before = _result()
    room = proposal.get("room") or st.room
    target = proposal.get("target") or st.target
    idx = next_index()
    name = f"{base_name(context)} v{idx} {target}"
    dx, _width = next_dx(context)
    mapping, root = duplicate_building(context, name, idx, target, dx)
    state = _text_json(_STATE) or {"iterations": [], "variants": [], "edit_history": []}
    history = list(state.get("edit_history") or [])
    all_edits = history + list(edits)
    applied_all = apply_edits(mapping, all_edits, result_before, room, root, dx)
    applied = applied_all[len(history):]
    state["edit_history"] = all_edits
    try:
        from . import scene_look as SL
        setup = SL.apply_setup_to_variant(root, target)
        applied.append({
            "kind": "scene_setup",
            "applied_to": len(setup.get("lights") or []),
            "detail": f"set the scene for {target}: {setup['plan']['label']}",
            "reason": setup["plan"]["reason"],
            "condition": setup["plan"]["condition"],
        })
    except Exception as exc:
        applied.append({"kind": "scene_setup", "applied_to": 0, "error": str(exc)[:200]})
    mn, mx = world_bbox(list(mapping.values()))
    add_label(root, name, mn, mx)
    cam = matching_camera(mapping, context)
    if cam is not None:
        cam["feel_loop_role"] = "camera"
        root["feel_loop_camera"] = cam.name

    # re-read the variant
    feel_before, scores_before, rec_before = _scores(result_before, room)
    _store_snapshot(context)  # original rooms3d already stored; keep
    try:
        result_after = read_hunch(context, root.name, blocking=True)
    except Exception as exc:
        result_after = {"error": str(exc)}
    feel, scores, rec_after = _scores(result_after if "error" not in (result_after or {}) else {}, room)
    # if room names got duplicated as ".001" the score lookup may miss; fall back to first room
    if not feel:
        feel, scores, rec_after = _scores(result_after if isinstance(result_after, dict) else {}, "")

    rec = {
        "collection": root.name,
        "index": idx,
        "dx": dx,
        "camera": cam.name if cam else None,
        "feel_before": feel_before,
        "feel": feel,
        "scores_before": scores_before,
        "scores": scores,
        "applied": applied,
        "proposal": proposal,
        "features_before": _features_brief(rec_before),
        "features_after": _features_brief(rec_after),
        "reached": (feel or "").lower() == (target or "").lower(),
    }
    state = _text_json(_STATE) or state
    state.setdefault("edit_history", all_edits)
    state.setdefault("iterations", []).append(rec)
    state.setdefault("variants", []).append(root.name)
    state["target"] = target
    state["room"] = room
    state["start_feel"] = state.get("start_feel") or feel_before
    state["scores_start"] = state.get("scores_start") or scores_before
    state["end_feel"] = feel
    state["iterations_run"] = len(state["iterations"])
    state["reached"] = any(it.get("reached") for it in state["iterations"]) and (feel or "").lower() == (target or "").lower()
    _set_text_json(_STATE, state)
    st.loop_iteration = state["iterations_run"]
    st.loop_waiting = False
    st.scope_collection = root.name
    reached = rec["reached"]
    st.status = (
        f"built {root.name} beside the original. predicted to read as {feel or 'unscored'}"
        + (f", target {target} reached." if reached else f", target {target} not yet reached.")
    )
    # reset scope for subsequent original scans? leave on the variant so a follow-up proposal
    # is based on the latest read. Propose will read current scope.
    if record_run:
        share = _record_run(context, state)
        if share.get("note"):
            st.status = (st.status or "") + "  " + share["note"]
    return rec, state


def run_auto(context, out_dir, max_iter=None, samples=32):
    st = _st(context)
    max_iter = int(max_iter or st.loop_max_iter or 4)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # start from the original
    st.scope_collection = "original"
    bpy.ops.hunch.scan()
    _store_snapshot(context)
    # viewpoint camera
    if st.viewpoint is None or st.viewpoint.type != "CAMERA":
        if context.scene.camera is not None:
            st.viewpoint = context.scene.camera
    result = read_hunch(context, "original", blocking=True)
    feel0, scores0, _ = _scores(result, st.room)
    state = {
        "target": st.target, "room": st.room, "start_feel": feel0, "end_feel": feel0,
        "scores_start": scores0, "iterations": [], "variants": [], "iterations_run": 0, "reached": False,
        "note": "edits are a mapping from hunch suggestions onto this blender scene.",
    }
    _set_text_json(_STATE, state)
    # before render from the original camera
    cam0 = st.viewpoint if st.viewpoint is not None and st.viewpoint.type == "CAMERA" else context.scene.camera
    variants_objs = lambda: [o for o in context.scene.objects if in_variant(o)]
    before = render_view(context, cam0, out / "before.png", samples=samples, hide=variants_objs())
    if before is None and (out / "before.png").is_file():
        before = str(out / "before.png")
    state["before_render"] = before
    before_path = before

    reached = (feel0 or "").lower() == (st.target or "").lower()
    if reached:
        state["reached"] = True
        state["end_feel"] = feel0
        state["note_start"] = "the original already was predicted to read as the target; no variant built."
        _set_text_json(_STATE, state)
        write_changelog(out / "changelog.txt", state)
        st.status = f"already predicted to read as {feel0}; no variant built."
        share = _record_run(context, state)
        if share.get("note"):
            st.status = st.status + "  " + share["note"]
        return state

    for i in range(max_iter):
        st.scope_collection = "original" if i == 0 else (state["variants"][-1] if state["variants"] else "original")
        # proposal is always based on the latest result (original first, then last variant)
        make_proposal(context, _result())
        rec, state = approve_and_build(context, out_dir=out, record_run=False)
        if rec.get("reached"):
            break
        if not rec.get("applied"):
            break

    # after render from the last variant's matching camera
    last = (state.get("variants") or [None])[-1]
    cam1 = None
    if last:
        col = bpy.data.collections.get(last)
        cam_name = col.get("feel_loop_camera") if col else None
        if cam_name:
            cam1 = bpy.data.objects.get(cam_name)
        if cam1 is None and col is not None:
            cam1 = next((o for o in walk_collection(col) if o.type == "CAMERA"), None)
    hide_orig = [o for o in source_objects(context)]
    after = render_view(context, cam1 or cam0, out / "after.png", samples=samples, hide=hide_orig)
    if after is None and (out / "after.png").is_file():
        after = str(out / "after.png")
    state["before_render"] = before_path or state.get("before_render")
    state["after_render"] = after
    cap = f"{state.get('room')}  {state.get('start_feel')} -> {state.get('end_feel')}  target {state.get('target')}  reached={state.get('reached')}  {state.get('iterations_run')} iteration(s)"
    state["side_by_side"] = side_by_side(before, after, out / "side-by-side.png", caption=cap)
    # overview of both buildings
    state["overview"] = render_overview(context, out / "overview-both.png", samples=max(16, samples // 2))
    write_changelog(out / "changelog.txt", state)
    _set_text_json(_STATE, state)
    # try export of the last variant
    if last:
        try:
            state["exports"] = export_variant(context, last, out / "export")
        except Exception as exc:
            state["exports"] = {"error": str(exc)}
        _set_text_json(_STATE, state)
        write_changelog(out / "changelog.txt", state)
    try:
        state["feel_report"] = write_one_page_report(context, out, state)
    except Exception as exc:
        state["feel_report"] = {"error": str(exc)[:300]}
    _set_text_json(_STATE, state)
    st.status = (
        f"feel loop finished: {state.get('iterations_run')} iteration(s), "
        f"{state.get('start_feel')} to {state.get('end_feel')}, "
        f"target {state.get('target')} {'reached' if state.get('reached') else 'not reached'}."
    )
    if not state.get("conditions"):
        cond = _text_json("tingle_conditions.json")
        if cond:
            state["conditions"] = cond
            _set_text_json(_STATE, state)
    share = _record_run(context, state)
    if share.get("note"):
        st.status = st.status + "  " + share["note"]
    return state


def render_overview(context, path, samples=16):
    """A third-person shot of the original and every variant, side by side."""
    objs = [o for o in context.scene.objects if o.type == "MESH"]
    mn, mx = world_bbox(objs)
    mid = (mn + mx) / 2.0
    span = (mx - mn).length
    cam_data = bpy.data.cameras.new("feel loop overview")
    cam = bpy.data.objects.new("feel loop overview", cam_data)
    context.scene.collection.objects.link(cam)
    # stand back on +Y, look at mid
    dist = max(8.0, span * 0.85)
    cam.location = (mid.x, mid.y - dist, mid.z + max(3.0, span * 0.25))
    direction = mid - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam_data.lens = 35
    try:
        return render_view(context, cam, path, samples=samples)
    finally:
        bpy.data.objects.remove(cam, do_unlink=True)
        bpy.data.cameras.remove(cam_data)



def write_one_page_report(context, out_dir, state=None):
    """Call the tingle one-page report writer (pdf + png) via the service python."""
    st = _st(context)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = _result()
    state = state or _text_json(_STATE) or {}
    payload = {
        "project": base_name(context),
        "room": state.get("room") or st.room,
        "target": state.get("target") or st.target,
        "site": result.get("site") or {},
        "hunch": result,
        "loop": state,
        "viewpoint_image": state.get("before_render"),
        "before": state.get("before_render"),
        "after": state.get("after_render"),
    }
    pay = out / "feel-report-payload.json"
    pay.write_text(json.dumps(payload), encoding="utf-8")
    dest = out / "feel-report.pdf"
    py = st.service_python or os.environ.get("HUNCH_PYTHON") or os.path.expanduser("~/.tingle/venv/bin/python")
    r = subprocess.run([py, "-m", "hunch_adapter.feel_report", "--input", str(pay), "--out", str(dest)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not dest.is_file():
        raise RuntimeError((r.stderr or r.stdout or "report writer failed")[-400:])
    return {"pdf": str(dest), "png": str(dest.with_suffix(".png"))}

# ---------------- operators and panel section

class HUNCH_OT_feel_propose(bpy.types.Operator):
    bl_idname = "hunch.feel_propose"
    bl_label = "Propose edits"
    bl_description = "Read the current space with hunch and propose a concrete edit set for a new variant"

    def execute(self, context):
        st = _st(context)
        try:
            st.scope_collection = st.scope_collection or "original"
            if st.scope_collection in ("", "original"):
                bpy.ops.hunch.scan()
            _store_snapshot(context)
            if not _result() or not _result().get("rooms"):
                read_hunch(context, st.scope_collection or "original", blocking=True)
            make_proposal(context)
        except Exception as e:
            st.status = f"propose failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_feel_approve(bpy.types.Operator):
    bl_idname = "hunch.feel_approve"
    bl_label = "Approve and build variant"
    bl_description = "Duplicate the building beside the original, apply the approved edits to the copy only"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        st = _st(context)
        try:
            rec, _state = approve_and_build(context)
        except Exception as e:
            st.status = f"approve failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_feel_reject(bpy.types.Operator):
    bl_idname = "hunch.feel_reject"
    bl_label = "Reject proposal"
    bl_description = "Discard the current proposal. the scene is unchanged."

    def execute(self, context):
        st = _st(context)
        _set_text_json(_PROPOSAL, {})
        st.loop_waiting = False
        st.status = "proposal rejected. the original is unchanged."
        return {"FINISHED"}


class HUNCH_OT_feel_auto(bpy.types.Operator):
    bl_idname = "hunch.feel_auto"
    bl_label = "Auto until target"
    bl_description = "Propose, build a labelled variant beside the original, re-read, and repeat up to the max iterations"

    def execute(self, context):
        st = _st(context)
        out = bpy.path.abspath(st.loop_out or "//feel-loop")
        if out.startswith("//"):
            out = str(Path(bpy.data.filepath or tempfile.gettempdir()).parent / "feel-loop")
        try:
            run_auto(context, out, max_iter=st.loop_max_iter)
        except Exception as e:
            st.status = f"feel loop failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_feel_export(bpy.types.Operator):
    bl_idname = "hunch.feel_export"
    bl_label = "Export last variant"
    bl_description = "Write the last variant on its own as a .blend collection, .obj, .glb, and .3dm if feasible"
    directory: bpy.props.StringProperty(subtype="DIR_PATH", default="")

    def invoke(self, context, event):
        st = _st(context)
        self.directory = bpy.path.abspath(st.loop_out or "//feel-loop")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        st = _st(context)
        state = _text_json(_STATE) or {}
        names = state.get("variants") or [c.name for c in list_variants()]
        if not names:
            self.report({"ERROR"}, "no variant to export")
            return {"CANCELLED"}
        try:
            res = export_variant(context, names[-1], self.directory or bpy.path.abspath(st.loop_out or "//feel-loop"))
        except Exception as e:
            st.status = f"export failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        ok = ", ".join(f"{k}: {'yes' if v else 'no'}" for k, v in (res or {}).items() if k != "error")
        st.status = f"exported {names[-1]} ({ok})"
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_feel_render(bpy.types.Operator):
    bl_idname = "hunch.feel_render"
    bl_label = "Render before / after"
    bl_description = "Render matching cameras on the original and the last variant, plus a side-by-side image"

    def execute(self, context):
        st = _st(context)
        out = Path(bpy.path.abspath(st.loop_out or "//feel-loop"))
        if str(out).startswith("//") or not st.loop_out:
            out = Path(bpy.data.filepath or tempfile.gettempdir()).parent / "feel-loop"
        state = _text_json(_STATE) or {}
        cam0 = st.viewpoint if st.viewpoint is not None and st.viewpoint.type == "CAMERA" else context.scene.camera
        before = render_view(context, cam0, out / "before.png", hide=[o for o in context.scene.objects if in_variant(o)])
        last = (state.get("variants") or [None])[-1]
        cam1 = None
        if last and bpy.data.collections.get(last):
            col = bpy.data.collections.get(last)
            cam1 = bpy.data.objects.get(col.get("feel_loop_camera") or "") or next((o for o in walk_collection(col) if o.type == "CAMERA"), None)
        after = render_view(context, cam1 or cam0, out / "after.png", hide=source_objects(context))
        if before is None and (out / "before.png").is_file():
            before = str(out / "before.png")
        if after is None and (out / "after.png").is_file():
            after = str(out / "after.png")
        cap = f"{state.get('room')} {state.get('start_feel')} -> {state.get('end_feel')}"
        sbs = side_by_side(before, after, out / "side-by-side.png", caption=cap)
        overview = render_overview(context, out / "overview-both.png")
        state["before_render"] = before
        state["after_render"] = after
        state["side_by_side"] = sbs
        if overview:
            state["overview"] = overview
        _set_text_json(_STATE, state)
        st.status = f"wrote renders in {out}"
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


CLASSES = (
    HUNCH_OT_feel_propose,
    HUNCH_OT_feel_approve,
    HUNCH_OT_feel_reject,
    HUNCH_OT_feel_auto,
    HUNCH_OT_feel_export,
    HUNCH_OT_feel_render,
)


def draw_section(layout, context):
    st = context.scene.hunch
    box = layout.box()
    box.label(text="feel loop")
    box.label(text="build a full variant beside the original")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.prop(st, "loop_auto")
    col.prop(st, "loop_max_iter")
    col.prop(st, "loop_out")
    row = col.row(align=True)
    row.operator("hunch.feel_propose", icon="LIGHT")
    row.operator("hunch.feel_approve", icon="CHECKMARK")
    row.operator("hunch.feel_reject", icon="X")
    col.operator("hunch.feel_auto", icon="PLAY")
    row = col.row(align=True)
    row.operator("hunch.feel_render", icon="RENDER_STILL")
    row.operator("hunch.feel_export", icon="EXPORT")
    proposal = _text_json(_PROPOSAL) or {}
    if st.loop_waiting and proposal.get("edits"):
        col.label(text=f"{len(proposal['edits'])} proposed edit(s) toward {proposal.get('target')}:")
        for ed in proposal["edits"][:5]:
            col.label(text=f"  - {ed.get('detail') or ed.get('kind')}")
    state = _text_json(_STATE) or {}
    if state.get("variants"):
        col.label(text="variants: " + ", ".join(state["variants"]))
        col.label(text=f"reached: {state.get('reached')}  ({state.get('start_feel')} to {state.get('end_feel')})")
