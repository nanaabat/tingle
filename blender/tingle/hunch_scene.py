"""Read the current Blender scene into a hunch building snapshot.

Rooms come from closed meshes (a 'rooms' collection, a hunch_room_shell property, or a
closed mesh named as a room). Glazing is detected by object/material name or a transmissive
material. Other meshes are classified as floor, ceiling, wall or other from name, material
and face normals. Units are metres, Z is up. No hunch import: this module is safe inside Blender.
"""
from __future__ import annotations

import math

import bpy
from mathutils import Vector

OPENING_WORDS = ("window", "glaz", "glass", "opening", "sash", "fenestr")
FLOOR_WORDS = ("floor", "slab", "deck")
CEIL_WORDS = ("ceil", "roof", "soffit")
SHADE_WORDS = ("lovre", "louvre", "louver", "solar panel", "shade", "brise")
FURNITURE_WORDS = ("cupboard", "toilet", "seat", "table", "chair", "bed", "sofa", "furniture")
ROOM_HINTS = ("room", "kitchen", "loft", "bath", "bedroom", "living", "hall", "stair", "ground")



def _bsdf_color(mat):
    """Return the Principled BSDF base colour (r,g,b) if present."""
    if mat is None:
        return None
    if getattr(mat, "use_nodes", False) and mat.node_tree is not None:
        for n in mat.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                sock = n.inputs.get("Base Color")
                if sock is not None:
                    try:
                        v = sock.default_value
                        return (float(v[0]), float(v[1]), float(v[2]))
                    except (TypeError, ValueError, IndexError):
                        return None
    col = getattr(mat, "diffuse_color", None)
    if col is not None and len(col) >= 3:
        return (float(col[0]), float(col[1]), float(col[2]))
    return None


def _material_rec(mat) -> dict:
    name = (mat.name if mat is not None else "default") or "default"
    rec = {"name": name}
    col = _bsdf_color(mat)
    if col is not None:
        rec["color"] = [round(c, 4) for c in col]
    kind = None
    if mat is not None:
        kind = mat.get("hunch_kind") if hasattr(mat, "get") else None
    if kind:
        rec["kind"] = str(kind)
    warmth = None
    if mat is not None and hasattr(mat, "get"):
        warmth = mat.get("hunch_warmth")
    if warmth is not None:
        rec["warmth"] = float(warmth)
    return rec


def _louver_closure(obj) -> float:
    """0 open .. 1 fully closed, from a tinker revolute joint on a louver/blind."""
    jd = obj.get("tinker_joint") if obj else None
    if not jd:
        return 0.0
    try:
        lo = float(jd.get("lower") or 0.0)
        hi = float(jd.get("upper") or 1.448623279)
        val = float(jd.get("value") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    span = max(1e-6, hi - lo)
    return max(0.0, min(1.0, (val - lo) / span))


def _mat_name(obj, poly=None) -> str:
    mats = obj.data.materials if obj.data else None
    if not mats:
        return (obj.active_material.name if obj.active_material else obj.name) or "default"
    idx = 0
    if poly is not None:
        idx = min(max(poly.material_index, 0), len(mats) - 1)
    m = mats[idx] if 0 <= idx < len(mats) else None
    return (m.name if m else obj.name) or "default"


def _is_glass_material(mat) -> bool:
    if mat is None:
        return False
    if any(w in mat.name.lower() for w in OPENING_WORDS):
        return True
    if not getattr(mat, "use_nodes", False) or mat.node_tree is None:
        return False
    for n in mat.node_tree.nodes:
        if n.type != "BSDF_PRINCIPLED":
            continue
        for key in ("Transmission Weight", "Transmission"):
            sock = n.inputs.get(key)
            if sock is not None:
                try:
                    if float(sock.default_value) > 0.3:
                        return True
                except (TypeError, ValueError):
                    pass
    return False


def is_glazing(obj) -> bool:
    if obj.type != "MESH":
        return False
    if obj.get("hunch_kind") == "opening":
        return True
    name = obj.name.lower()
    if any(w in name for w in OPENING_WORDS) and "frame" not in name:
        return True
    mats = (obj.data.materials if obj.data else None) or []
    return any(_is_glass_material(m) for m in mats if m)


def _in_collection(obj, names) -> bool:
    want = {n.lower() for n in names}
    return any(c.name.lower() in want for c in obj.users_collection)


HELPER_ROLES = ("soffit", "partition", "overhang", "shade", "label", "droplet", "water", "fog", "streak", "setup", "fitting")


def is_helper_mesh(obj) -> bool:
    role = str(obj.get("hunch_role") or obj.get("feel_loop_role") or "").lower()
    if role in HELPER_ROLES:
        return True
    name = (obj.name or "").lower()
    return any(name.startswith(p) or f" {p}" in f" {name}" or name.endswith(f" {p}") or f" {p} " in f" {name} "
               for p in ("soffit", "partition", "overhang")) or " soffit" in name or name.endswith("soffit") or "overhang" in name


def is_room_shell(obj) -> bool:
    if obj.type != "MESH":
        return False
    if is_helper_mesh(obj):
        return False
    if obj.get("hunch_room_shell") or obj.get("hunch_kind") == "room":
        return True
    return _in_collection(obj, ("rooms", "room"))


def is_closed_mesh(obj) -> bool:
    if obj.type != "MESH" or not obj.data or not obj.data.polygons:
        return False
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        if not bm.edges:
            return False
        return all(len(e.link_faces) == 2 for e in bm.edges)
    finally:
        bm.free()


def _world_bbox(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs, ys, zs = zip(*[(c.x, c.y, c.z) for c in corners])
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def _kind_from_name(name: str, normal, is_open: bool) -> str:
    low = name.lower()
    if is_open or any(w in low for w in OPENING_WORDS) and "frame" not in low:
        return "opening"
    if any(w in low for w in SHADE_WORDS):
        return "shade"
    if any(w in low for w in FURNITURE_WORDS):
        return "furniture"
    vz = float(normal[2])
    if vz > 0.85 or any(w in low for w in FLOOR_WORDS):
        return "floor"
    if vz < -0.85 or any(w in low for w in CEIL_WORDS):
        return "ceiling"
    if abs(vz) < 0.35:
        return "wall"
    if vz < 0:
        return "slope"
    return "slope_up"


def _room_name_for(obj, shells, centroid, fallback=""):
    explicit = obj.get("hunch_room")
    if explicit:
        return str(explicit)
    if is_room_shell(obj):
        return obj.name
    low = obj.name.lower()
    for suf in ("_windows", "_window", "_openings", "_opening", "_glazing", "-glass", "_glass"):
        if low.endswith(suf):
            return obj.name[: -len(suf)]
    # object name itself looks like a room
    for hint in ROOM_HINTS:
        if hint in low and "frame" not in low and not is_glazing(obj):
            # use the object name as the room when it is a room-like mesh
            if _in_collection(obj, ("rooms", "room")) or is_closed_mesh(obj) or hint in low:
                # prefer the matching shell name if any
                for sh, (mn, mx), name in shells:
                    if hint in name.lower():
                        return name
                return obj.name
    for _sh, (mn, mx), name in shells:
        if all(mn[i] - 0.15 <= centroid[i] <= mx[i] + 0.15 for i in range(3)):
            return name
    return fallback or "_unassigned"


def _iter_world_triangles(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    mw = obj.matrix_world
    verts = mesh.vertices
    mats = mesh.materials
    for tri in mesh.loop_triangles:
        pts = [(mw @ verts[i].co) for i in tri.vertices]
        a, b, c = pts
        ax, ay, az = (b - a).cross(c - a)
        area = 0.5 * math.sqrt(ax * ax + ay * ay + az * az)
        if area <= 1e-12:
            continue
        n = (b - a).cross(c - a).normalized()
        mid = (a + b + c) / 3.0
        mi = tri.material_index
        mat = mats[mi].name if mats and 0 <= mi < len(mats) and mats[mi] else _mat_name(obj)
        glass = False
        if mats and 0 <= mi < len(mats):
            glass = _is_glass_material(mats[mi])
        yield {
            "verts": [[p.x, p.y, p.z] for p in pts],
            "area": float(area),
            "normal": [float(n.x), float(n.y), float(n.z)],
            "centroid": [float(mid.x), float(mid.y), float(mid.z)],
            "material": mat,
            "glass": glass,
        }



def _parent_collection(col):
    for p in bpy.data.collections:
        if col.name in p.children:
            return p
    return None


def _in_variant_collection(obj) -> bool:
    def flag(c):
        cur = c
        while cur is not None:
            if cur.get("feel_loop_variant"):
                return True
            cur = _parent_collection(cur)
        return False
    return any(flag(c) for c in obj.users_collection)


def iter_scope_objects(context, scope=None):
    """Yield scene objects in a scope: 'original' (skip feel-loop variants), a collection name, or all."""
    scene = context.scene
    if scope in (None, "", "all"):
        for obj in scene.objects:
            yield obj
        return
    if scope == "original":
        for obj in scene.objects:
            if not _in_variant_collection(obj):
                yield obj
        return
    col = bpy.data.collections.get(scope) if isinstance(scope, str) else scope
    if col is None:
        for obj in scene.objects:
            yield obj
        return
    seen = set()
    def walk(c):
        for o in c.objects:
            if o.name not in seen:
                seen.add(o.name)
                yield o
        for ch in c.children:
            yield from walk(ch)
    yield from walk(col)


def discover_rooms(context, scope=None) -> list[dict]:
    """Room volumes from shells / closed meshes, for the panel's room list and for rooms3d.

    Helper meshes (soffit, partition, overhang, rain, water, fog) are never room volumes,
    even when they carry a hunch_room tag or a room-like name. When two meshes share a
    room name, prefer an explicit room shell, then the larger volume.
    """
    shells = []
    for obj in iter_scope_objects(context, scope):
        if obj.type != "MESH" or obj.hide_get():
            continue
        if is_helper_mesh(obj):
            continue
        explicit = bool(obj.get("hunch_room_shell") or obj.get("hunch_kind") == "room" or is_room_shell(obj))
        named = is_closed_mesh(obj) and (obj.get("hunch_room") or any(h in obj.name.lower() for h in ROOM_HINTS))
        if not (explicit or named):
            continue
        mn, mx = _world_bbox(obj)
        vol = max(0.0, (mx[0] - mn[0]) * (mx[1] - mn[1]) * (mx[2] - mn[2]))
        shells.append({
            "name": str(obj.get("hunch_room") or obj.name),
            "min": mn, "max": mx,
            "source": "blender room shell" if explicit else "blender closed mesh",
            "object": obj.name,
            "explicit": explicit,
            "volume": vol,
        })
    by = {}
    for s in shells:
        name = s["name"]
        if name not in by:
            by[name] = s
            continue
        old = by[name]
        if s.get("explicit") and not old.get("explicit"):
            by[name] = s
        elif s.get("explicit") == old.get("explicit") and s.get("volume", 0) > old.get("volume", 0):
            by[name] = s
    return list(by.values())


def extract_snapshot(context, *, site="", orient_deg=0.0, target="calm",
                     focus_room="", person=None, when=None, include="rooms_and_glazing",
                     scope=None) -> dict:
    """Build a hunch snapshot from the current scene.

    include:
      rooms_and_glazing (default) - room shells + glazing + explicitly tagged meshes
      all_visible - every visible mesh (capped)
    """
    scene = context.scene
    rooms = discover_rooms(context, scope=scope)
    shells = [(None, (r["min"], r["max"]), r["name"]) for r in rooms]
    faces = []
    materials = {}
    FACE_CAP = 40000
    for obj in iter_scope_objects(context, scope):
        if obj.type != "MESH" or obj.hide_get():
            continue
        # soffits are included (hunch_include / hunch_kind=ceiling) so _plan_heights can
        # see them; partitions and overhangs can be included as wall/shade. fog/water/rain
        # helpers stay out of the building snapshot unless explicitly tagged.
        role = str(obj.get("hunch_role") or "").lower()
        if role in ("droplet", "water", "fog", "streak", "setup", "label"):
            continue
        tagged = bool(obj.get("hunch_include"))
        glaze = is_glazing(obj)
        shell = is_room_shell(obj)
        if include != "all_visible" and not (glaze or shell or tagged or _in_collection(obj, ("glazing", "rooms", "room"))):
            # still take closed room-like meshes that are not in a rooms collection
            if not (is_closed_mesh(obj) and any(h in obj.name.lower() for h in ROOM_HINTS)):
                continue
        try:
            tris = list(_iter_world_triangles(obj))
        except Exception:
            continue
        for t in tris:
            if len(faces) >= FACE_CAP:
                break
            name = (obj.name + " " + t["material"]).lower()
            opening = glaze or t["glass"] or (any(w in name for w in OPENING_WORDS) and "frame" not in name)
            kind = obj.get("hunch_kind") or _kind_from_name(name, t["normal"], opening)
            if opening:
                kind = "opening"
            room = _room_name_for(obj, shells, t["centroid"])
            faces.append({
                "verts": t["verts"],
                "area": t["area"],
                "normal": t["normal"],
                "room": room,
                "material": t["material"],
                "kind": kind,
            })
            mat_obj = None
            try:
                mats = obj.data.materials if obj.data else None
                if mats and t.get("material"):
                    mat_obj = next((m for m in mats if m and m.name == t["material"]), None)
            except Exception:
                mat_obj = None
            rec = _material_rec(mat_obj) if mat_obj is not None else {"name": t["material"]}
            # keep the first (usually richer) record for this material name
            materials.setdefault(t["material"], rec)
        if len(faces) >= FACE_CAP:
            break

    # closed louvers honestly cut effective opening area (view and daylight through the slats)
    closures = []
    for obj in iter_scope_objects(context, scope):
        name = (obj.name or "").lower()
        if name.startswith("louver-tilt") or obj.get("tinker_type") == "blind" or (obj.get("tinker_joint") and "louver" in name):
            closures.append(_louver_closure(obj))
    if closures:
        close = sum(closures) / len(closures)
        if close > 0.05:
            factor = 1.0 - 0.75 * close
            for f in faces:
                if f.get("kind") == "opening":
                    f["area"] = float(f.get("area") or 0.0) * factor
                    f["shaded_by_louvers"] = round(close, 3)

    vp = person
    if vp is None:
        view = getattr(scene, "hunch", None)
        obj = None
        if view is not None and getattr(view, "viewpoint", None):
            obj = view.viewpoint
        if obj is not None:
            loc = obj.matrix_world.translation
            # if it's a camera, feet are below the eye
            eye_h = 1.6
            feet = [float(loc.x), float(loc.y), float(loc.z) - (eye_h if obj.type == "CAMERA" else 0.0)]
            if obj.type != "CAMERA":
                feet = [float(loc.x), float(loc.y), float(loc.z)]
            vp = {"feet": feet, "eye_height": eye_h}

    units = scene.unit_settings
    scale = float(units.scale_length or 1.0)
    # we assume the scene is already in metres. if the unit scale is not 1,
    # scale faces and rooms so hunch always sees metres.
    if abs(scale - 1.0) > 1e-6:
        for f in faces:
            f["verts"] = [[c * scale for c in p] for p in f["verts"]]
            f["area"] = f["area"] * scale * scale
        for r in rooms:
            r["min"] = [c * scale for c in r["min"]]
            r["max"] = [c * scale for c in r["max"]]
        if vp and vp.get("feet"):
            vp = dict(vp, feet=[c * scale for c in vp["feet"]], eye_height=float(vp.get("eye_height") or 1.6) * scale)

    return {
        "up_axis": 2,
        "faces": faces,
        "materials": materials,
        "rooms3d": [{"name": r["name"], "min": r["min"], "max": r["max"], "source": r.get("source") or "blender"} for r in rooms],
        "path": bpy.data.filepath or "blender-scene",
        "site": site,
        "orient_deg": float(orient_deg or 0),
        "target": target,
        "focus_room": focus_room or "",
        "person": vp,
        "when": when,
        "host": {"name": "blender", "version": bpy.app.version_string},
    }



def compact_snapshot(snap, max_faces=800) -> dict:
    """Shrink a snapshot so it fits the wink/tinker loopback body cap (4 MiB).

    Room volumes stay intact. Opening (glazing) faces are kept, largest first,
    up to max_faces. Other faces are dropped when rooms3d is present, otherwise
    the largest remaining faces fill the budget. The add-on's own hunch.run
    worker still receives the full snapshot through a file, not this path.
    """
    snap = dict(snap)
    faces = list(snap.get("faces") or [])
    def is_open(f):
        kind = (f.get("kind") or "")
        blob = " ".join([kind, f.get("material") or "", f.get("room") or ""]).lower()
        return kind == "opening" or any(w in blob for w in OPENING_WORDS)
    openings = [f for f in faces if is_open(f)]
    rest = [f for f in faces if not is_open(f)]
    openings.sort(key=lambda f: -float(f.get("area") or 0))
    rest.sort(key=lambda f: -float(f.get("area") or 0))
    kept = openings[:max_faces]
    if not snap.get("rooms3d"):
        kept.extend(rest[: max(0, max_faces - len(kept))])
    snap["faces"] = kept
    snap["compacted"] = {"from": len(faces), "to": len(kept)}
    return snap


def build_sample_house(context, collection_name="hunch sample house"):
    """Build the sample three-room house (kitchen, stair, loft) with named glazing, in metres, Z up."""
    # start from a clean named collection
    coll = bpy.data.collections.get(collection_name) or bpy.data.collections.new(collection_name)
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)

    def mesh_obj(name, verts, faces, material, collection=coll):
        me = bpy.data.meshes.new(name + "-mesh")
        me.from_pydata(verts, [], faces)
        me.update()
        ob = bpy.data.objects.new(name, me)
        collection.objects.link(ob)
        mat = bpy.data.materials.get(material) or bpy.data.materials.new(material)
        if material == "glass":
            mat.use_nodes = True
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                if "Transmission Weight" in bsdf.inputs:
                    bsdf.inputs["Transmission Weight"].default_value = 0.9
                elif "Transmission" in bsdf.inputs:
                    bsdf.inputs["Transmission"].default_value = 0.9
                bsdf.inputs["Base Color"].default_value = (0.55, 0.70, 0.78, 1)
        elif "timber" in material:
            mat.diffuse_color = (0.70, 0.50, 0.30, 1)
        else:
            mat.diffuse_color = (0.94, 0.93, 0.88, 1)
        if me.materials:
            me.materials[0] = mat
        else:
            me.materials.append(mat)
        return ob

    def box(name, mn, mx, material, *, shell=False):
        x0, y0, z0 = mn
        x1, y1, z1 = mx
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        # outward faces (closed)
        f = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
        ob = mesh_obj(name, v, f, material)
        if shell:
            ob["hunch_room_shell"] = True
            ob["hunch_room"] = name
        return ob

    def pane(name, verts):
        # verts are 4 points of a quad in order
        return mesh_obj(name, verts, [(0, 1, 2, 3)], "glass")

    # kitchen 0-6 x, 0-5 y, 0-2.7 z  (Z up, matching a metres plan)
    kitchen = box("kitchen", (0, 0, 0), (6, 5, 2.7), "timber_wall", shell=True)
    loft = box("loft", (0, 0, 2.7), (6, 5, 5.2), "plaster_wall", shell=True)
    stair = box("stair", (6, 0, 0), (8.5, 5, 2.7), "plaster_wall", shell=True)
    # glazing on the kitchen +y and -x faces, slightly outside so it stays an opening
    pane("kitchen_windows.south-a", [(1.0, 5.02, 0.9), (2.4, 5.02, 0.9), (2.4, 5.02, 2.2), (1.0, 5.02, 2.2)])
    pane("kitchen_windows.south-b", [(3.6, 5.02, 0.9), (5.0, 5.02, 0.9), (5.0, 5.02, 2.2), (3.6, 5.02, 2.2)])
    pane("kitchen_windows.west", [(-0.02, 1.5, 0.9), (-0.02, 3.2, 0.9), (-0.02, 3.2, 2.2), (-0.02, 1.5, 2.2)])
    pane("loft_windows.south", [(1.5, 5.02, 3.4), (4.5, 5.02, 3.4), (4.5, 5.02, 4.8), (1.5, 5.02, 4.8)])
    context.view_layer.update()
    return {"kitchen": kitchen, "loft": loft, "stair": stair}
