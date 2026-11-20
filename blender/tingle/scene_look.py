"""Materials, world/sky, feel-driven scene setup, and weather-condition scenes for tingle.

World and sun are scene-global in Blender, so each condition lives in its own
Scene that instances (or, for wet materials, copies) a building collection.
The original collection is never permanently mutated. Hunch reads whatever it
actually consumes: geometry features plus, when a still is provided, pixel
conditions measured from that still. Mood here means the target feel.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import bpy
from mathutils import Vector

# ISO times used as the `when` passed to hunch (solar position only, not a weather sim).
WHEN = {
    "clear_noon": "2024-03-21T12:00:00+00:00",
    "golden_hour": "2024-03-21T18:00:00+00:00",
    "overcast": "2024-03-21T12:00:00+00:00",
    "rain": "2024-03-21T12:00:00+00:00",
    "harmattan": "2024-01-15T12:00:00+00:00",
    "night": "2024-03-21T21:00:00+00:00",
    "on_water": "2024-03-21T12:00:00+00:00",
    "on_land": "2024-03-21T12:00:00+00:00",
}

# Sun altitude/azimuth (deg). Azimuth: 0 north, clockwise.
SUN = {
    "clear_noon": {"alt": 84.4, "az": 180.0, "energy": 4.0, "color": (1.0, 0.98, 0.94), "sky": 0.85},
    "golden_hour": {"alt": 12.0, "az": 270.0, "energy": 3.2, "color": (1.0, 0.62, 0.28), "sky": 0.55},
    "overcast": {"alt": 70.0, "az": 180.0, "energy": 0.15, "color": (0.85, 0.88, 0.92), "sky": 0.7},
    "rain": {"alt": 55.0, "az": 200.0, "energy": 0.2, "color": (0.75, 0.80, 0.88), "sky": 0.35},
    "harmattan": {"alt": 62.0, "az": 180.0, "energy": 2.2, "color": (1.0, 0.86, 0.62), "sky": 0.45},
    "night": {"alt": -20.0, "az": 300.0, "energy": 0.0, "color": (0.55, 0.62, 0.9), "sky": 0.04},
    "on_water": {"alt": 84.4, "az": 180.0, "energy": 4.0, "color": (1.0, 0.98, 0.94), "sky": 0.85},
    "on_land": {"alt": 84.4, "az": 180.0, "energy": 4.0, "color": (1.0, 0.98, 0.94), "sky": 0.85},
}

CONDITION_SPECS = [
    {"id": "clear_noon", "label": "clear noon", "needs_copy": False,
     "hint": "overhead sun shortens shadows and brightens surfaces",
     "blurb": "hard noon sun, clear sky, dry surfaces."},
    {"id": "golden_hour", "label": "golden hour", "needs_copy": False,
     "hint": "low sun stretches shadows and warms surfaces",
     "blurb": "low warm western sun, longer shadows."},
    {"id": "overcast", "label": "overcast", "needs_copy": False,
     "hint": "a soft sky evens light and cuts hard shadow",
     "blurb": "soft grey sky, almost no direct sun."},
    {"id": "rain", "label": "rain", "needs_copy": True, "wet": True, "streaks": True,
     "hint": "wet surfaces darken and glass picks up streaks",
     "blurb": "wet materials, darker sky, droplets and streaks on glass."},
    {"id": "harmattan", "label": "harmattan haze", "needs_copy": False, "fog": True,
     "hint": "dusty air warms the sky and softens the sun",
     "blurb": "dry-season dust: warm, hazy sky and softer sun (dusty-sky look; not a volumetric fog simulation)."},
    {"id": "night", "label": "night, interior light", "needs_copy": False, "interior": True,
     "hint": "interior light takes over when the sun is gone",
     "blurb": "no sun, dim sky, warm interior area lights."},
    {"id": "on_water", "label": "on water", "needs_copy": False, "water": True,
     "hint": "a water plane brightens the underside and the horizon",
     "blurb": "same noon sun, with a simple water plane around the building (what-if, not a site survey)."},
]


# Feel-driven scene setup (applied on a variant, or used as a render swap). Mood = target feel.
SCENE_SETUPS = {
    "calm": {
        "condition": "golden_hour",
        "light_energy": 35.0,
        "light_color": (1.0, 0.78, 0.55),
        "camera": "loft interior, slightly lower, looking across the room",
        "reason": "a lower warm sun and dimmer warm interior light support a calmer read than harsh noon.",
    },
    "cozy": {
        "condition": "golden_hour",
        "light_energy": 28.0,
        "light_color": (1.0, 0.70, 0.42),
        "camera": "loft interior, close, looking across",
        "reason": "warm low sun and a closer camera tighten the room without changing the original.",
    },
    "open": {
        "condition": "clear_noon",
        "light_energy": 80.0,
        "light_color": (1.0, 0.97, 0.92),
        "camera": "loft, looking out",
        "reason": "clear noon and a view out keep prospect and brightness high.",
    },
    "energized": {
        "condition": "clear_noon",
        "light_energy": 120.0,
        "light_color": (1.0, 0.97, 0.92),
        "camera": "loft, looking out",
        "reason": "bright noon light and a view out.",
    },
    "focused": {
        "condition": "overcast",
        "light_energy": 50.0,
        "light_color": (1.0, 0.95, 0.85),
        "camera": "loft interior, across the work surface",
        "reason": "even overcast light cuts glare.",
    },
    "uneasy": {
        "condition": "night",
        "light_energy": 8.0,
        "light_color": (0.7, 0.8, 1.0),
        "camera": "low corner",
        "reason": "night with cool, dim light.",
    },
    "closed-in": {
        "condition": "overcast",
        "light_energy": 20.0,
        "light_color": (1.0, 0.9, 0.75),
        "camera": "loft, tight, away from the openings",
        "reason": "soft light and a tighter camera, used with smaller openings on the variant.",
    },
}


HELPER_ROLES = {"soffit", "partition", "overhang", "shade", "label", "droplet", "water", "fog", "streak", "setup"}


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


def _sock(node, *names):
    for n in names:
        s = node.inputs.get(n)
        if s is not None:
            return s
    return None


def _set_sock(node, names, value):
    s = _sock(node, *names) if isinstance(names, (list, tuple)) else _sock(node, names)
    if s is None:
        return False
    try:
        s.default_value = value
        return True
    except Exception:
        try:
            if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
                s.default_value = tuple(value)[: len(s.default_value)]
                return True
        except Exception:
            return False
    return False


def principled(mat):
    if mat is None:
        return None
    if not getattr(mat, "use_nodes", False) or mat.node_tree is None:
        mat.use_nodes = True
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def enhance_pbr(mat):
    """Tighten technical PBR values on an existing Principled material; keep its base colour."""
    node = principled(mat)
    if node is None:
        return False
    name = (mat.name or "").lower()
    if any(w in name for w in ("glaz", "glass", "window")):
        _set_sock(node, ("Transmission Weight", "Transmission"), 0.9)
        _set_sock(node, ("IOR",), 1.52)
        _set_sock(node, ("Roughness",), 0.02)
        _set_sock(node, ("Metallic",), 0.0)
        _set_sock(node, ("Coat Weight", "Coat", "Clearcoat"), 0.15)
        _set_sock(node, ("Coat Roughness", "Clearcoat Roughness"), 0.05)
    elif any(w in name for w in ("timber", "wood", "cladding", "deck", "louver", "structure")):
        if _sock(node, "Roughness") and _sock(node, "Roughness").default_value < 0.2:
            _set_sock(node, ("Roughness",), 0.45)
        _set_sock(node, ("Coat Weight", "Coat", "Clearcoat"), 0.08)
        _set_sock(node, ("Specular IOR Level", "Specular"), 0.4)
    elif any(w in name for w in ("floor", "plaster", "shell", "room")):
        if _sock(node, "Roughness") and _sock(node, "Roughness").default_value < 0.25:
            _set_sock(node, ("Roughness",), 0.65)
    return True


def make_wet_material(mat):
    """Copy a material and make it look wet (darker, glossier, a clear coat)."""
    if mat is None:
        return None
    wet = mat.copy()
    wet.name = (mat.name or "mat") + " wet"
    node = principled(wet)
    if node is not None:
        base = _sock(node, "Base Color")
        if base is not None:
            try:
                c = list(base.default_value)
                c[0], c[1], c[2] = c[0] * 0.72, c[1] * 0.72, c[2] * 0.78
                base.default_value = c
            except Exception:
                pass
        _set_sock(node, ("Roughness",), 0.08)
        _set_sock(node, ("Coat Weight", "Coat", "Clearcoat"), 0.85)
        _set_sock(node, ("Coat Roughness", "Clearcoat Roughness"), 0.04)
        _set_sock(node, ("Specular IOR Level", "Specular"), 0.7)
        name = (mat.name or "").lower()
        if any(w in name for w in ("glaz", "glass", "window")):
            _set_sock(node, ("Transmission Weight", "Transmission"), 0.92)
            _set_sock(node, ("Roughness",), 0.04)
            _set_sock(node, ("Coat Weight", "Coat", "Clearcoat"), 1.0)
    return wet


def world_bbox(objects):
    pts = []
    for obj in objects:
        if obj.type not in {"MESH", "CURVE", "FONT", "LIGHT", "CAMERA", "EMPTY"}:
            continue
        try:
            corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            pts.extend(corners)
        except Exception:
            continue
    if not pts:
        return Vector((-4, -6, 0)), Vector((4, 2, 8))
    xs, ys, zs = [p.x for p in pts], [p.y for p in pts], [p.z for p in pts]
    return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))


def _sun_euler(alt_deg, az_deg):
    """Euler XYZ so a sun lamp's -Z points along the incoming rays."""
    alt = math.radians(alt_deg)
    az = math.radians(az_deg)
    # incoming direction from the sun (east=x, north=y, up=z)
    x = math.sin(az) * math.cos(alt)
    y = math.cos(az) * math.cos(alt)
    z = math.sin(alt)
    direction = Vector((-x, -y, -z))
    if direction.length < 1e-6:
        return (0.0, 0.0, 0.0)
    quat = direction.normalized().to_track_quat("-Z", "Y")
    return tuple(quat.to_euler("XYZ"))


def _ensure_world(name, spec, *, fog=False, overcast=False, night=False):
    world = bpy.data.worlds.get(name) or bpy.data.worlds.new(name)
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    out.location = (500, 0)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.location = (220, 80)
    sky = nt.nodes.new("ShaderNodeTexSky")
    sky.location = (0, 80)
    try:
        sky.sky_type = "NISHITA"
    except Exception:
        pass
    alt = math.radians(max(-10.0, min(90.0, float(spec.get("alt") or 45))))
    # Nishita sun_elevation is 0 at horizon, sun_rotation ~0 faces +Y in many builds.
    if hasattr(sky, "sun_elevation"):
        sky.sun_elevation = max(0.0, alt)
    if hasattr(sky, "sun_rotation"):
        sky.sun_rotation = math.radians(float(spec.get("az") or 180.0))
    if hasattr(sky, "air_density"):
        sky.air_density = 1.4 if fog else (1.6 if overcast else 1.0)
    if hasattr(sky, "dust_density"):
        sky.dust_density = 4.0 if fog else (1.5 if overcast else 0.4)
    if hasattr(sky, "ozone_density"):
        sky.ozone_density = 1.0
    strength = float(spec.get("sky") or 0.6)
    if night:
        # skip the sky texture; a dark blue background reads more clearly at night
        bg.inputs["Color"].default_value = (0.02, 0.03, 0.07, 1)
        bg.inputs["Strength"].default_value = 0.45
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    elif overcast or fog:
        tint = (0.55, 0.58, 0.62, 1) if overcast else (0.78, 0.68, 0.48, 1)
        bg.inputs["Color"].default_value = tint
        bg.inputs["Strength"].default_value = strength * (1.1 if overcast else 1.2)
        if fog:
            # keep a little sky variation so the disc is dusty, not a hard white sun
            try:
                nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
                bg.inputs["Strength"].default_value = strength * 1.3
            except Exception:
                pass
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    else:
        nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = strength
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    # EEVEE 5.3 world volume rendered a black frame in this setup, so harmattan
    # is a dusty-sky look (warm tint, softer sun), not a volumetric fog simulation.
    return world


def _add_sun(col, name, spec, mn, mx):
    data = bpy.data.lights.new(name, "SUN")
    data.energy = float(spec.get("energy") or 3.0)
    data.color = tuple(spec.get("color") or (1, 1, 1))[:3]
    if hasattr(data, "angle"):
        data.angle = math.radians(0.53 if spec.get("energy", 1) > 1 else 5.0)
    ob = bpy.data.objects.new(name, data)
    mid = (mn + mx) / 2.0
    ob.location = (mid.x, mid.y, mx.z + 8.0)
    ob.rotation_euler = _sun_euler(spec.get("alt") or 45, spec.get("az") or 180)
    col.objects.link(ob)
    ob["hunch_role"] = "setup"
    return ob


def _add_interior_lights(col, name, mn, mx, energy=40.0, color=(1.0, 0.78, 0.55)):
    lamps = []
    # two area lights under a likely loft / ground ceiling band
    spans = [
        (mn.z + 2.4, energy),
        ((mn.z + mx.z) * 0.5 + 1.0, energy * 0.8),
    ]
    for i, (z, e) in enumerate(spans):
        data = bpy.data.lights.new(f"{name} area {i+1}", "AREA")
        data.energy = float(e)
        data.size = 1.4
        data.color = tuple(color)[:3]
        if hasattr(data, "spread"):
            data.spread = math.radians(120)
        ob = bpy.data.objects.new(f"{name} area {i+1}", data)
        ob.location = ((mn.x + mx.x) / 2.0, (mn.y + mx.y) / 2.0, min(mx.z - 0.2, z))
        ob.rotation_euler = (0, 0, 0)
        col.objects.link(ob)
        ob["hunch_role"] = "setup"
        lamps.append(ob)
    return lamps


def _add_water(col, name, mn, mx):
    z = mn.z + 0.02
    pad = 18.0
    verts = [
        (mn.x - pad, mn.y - pad, z),
        (mx.x + pad, mn.y - pad, z),
        (mx.x + pad, mx.y + pad, z),
        (mn.x - pad, mx.y + pad, z),
    ]
    mesh = bpy.data.meshes.new(name + "-mesh")
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    ob = bpy.data.objects.new(name, mesh)
    col.objects.link(ob)
    mat = bpy.data.materials.new(name + "-mat")
    mat.use_nodes = True
    node = principled(mat)
    if node is not None:
        _set_sock(node, ("Base Color",), (0.04, 0.10, 0.14, 1))
        _set_sock(node, ("Roughness",), 0.04)
        _set_sock(node, ("Transmission Weight", "Transmission"), 0.85)
        _set_sock(node, ("IOR",), 1.33)
        _set_sock(node, ("Metallic",), 0.0)
        _set_sock(node, ("Coat Weight", "Coat", "Clearcoat"), 0.4)
    mesh.materials.append(mat)
    ob["hunch_role"] = "water"
    return ob


def _add_fog_volume(col, name, mn, mx):
    pad = 12.0
    mesh = bpy.data.meshes.new(name + "-mesh")
    x0, y0, z0 = mn.x - pad, mn.y - pad, mn.z - 1.0
    x1, y1, z1 = mx.x + pad, mx.y + pad, mx.z + 10.0
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    ob = bpy.data.objects.new(name, mesh)
    col.objects.link(ob)
    mat = bpy.data.materials.new(name + "-mat")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    # empty/transparent surface so the camera inside the volume is not in a black shell
    trans = nt.nodes.new("ShaderNodeBsdfTransparent")
    nt.links.new(trans.outputs["BSDF"], out.inputs["Surface"])
    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    _set_sock(vol, ("Color",), (0.72, 0.62, 0.42, 1))
    _set_sock(vol, ("Density",), 0.008)
    _set_sock(vol, ("Anisotropy",), 0.25)
    nt.links.new(vol.outputs["Volume"], out.inputs["Volume"])
    try:
        mat.blend_method = "BLEND"
    except Exception:
        pass
    mesh.materials.append(mat)
    ob.display_type = "WIRE"
    ob["hunch_role"] = "fog"
    return ob


def _add_rain(col, name, mn, mx, glazing=None):
    """A volume of falling streak instances plus a few droplets on glazing."""
    # streak instance
    streak_mesh = bpy.data.meshes.new(name + "-streak-mesh")
    r, h = 0.008, 0.35
    # thin elongated icosphere approximation: a 6-sided prism
    verts = []
    for z, s in ((0, r), (h, r * 0.25)):
        for i in range(6):
            a = i * math.pi / 3
            verts.append((s * math.cos(a), s * math.sin(a), z))
    faces = []
    for i in range(6):
        j = (i + 1) % 6
        faces.append((i, j, j + 6, i + 6))
    streak_mesh.from_pydata(verts, [], faces)
    streak_mesh.update()
    inst = bpy.data.objects.new(name + " streak inst", streak_mesh)
    inst.hide_render = True
    inst.hide_viewport = True
    col.objects.link(inst)
    mat = bpy.data.materials.new(name + "-drop")
    mat.use_nodes = True
    node = principled(mat)
    if node is not None:
        _set_sock(node, ("Base Color",), (0.7, 0.8, 0.9, 1))
        _set_sock(node, ("Transmission Weight", "Transmission"), 0.85)
        _set_sock(node, ("Roughness",), 0.02)
        _set_sock(node, ("IOR",), 1.33)
        _set_sock(node, ("Alpha",), 0.65)
    try:
        mat.blend_method = "BLEND"
    except Exception:
        pass
    streak_mesh.materials.append(mat)

    # scatter ~180 streaks in a volume above and through the hut (deterministic)
    import random
    rng = random.Random(21)
    mid = (mn + mx) / 2.0
    count = 180
    for i in range(count):
        ob = inst.copy()
        ob.data = streak_mesh
        ob.hide_render = False
        ob.hide_viewport = False
        ob.location = (
            rng.uniform(mn.x - 2.0, mx.x + 2.0),
            rng.uniform(mn.y - 2.0, mx.y + 2.0),
            rng.uniform(mn.z + 0.5, mx.z + 6.0),
        )
        ob.scale = (1, 1, rng.uniform(0.7, 1.6))
        ob.rotation_euler = (0, 0, rng.uniform(0, 6.28))
        col.objects.link(ob)
        ob["hunch_role"] = "streak"
        ob.name = f"{name} streak {i+1}"

    # droplets on glazing: a few icospheres slightly in front of each glass bbox
    drops = 0
    for g in (glazing or [])[:12]:
        try:
            corners = [g.matrix_world @ Vector(c) for c in g.bound_box]
        except Exception:
            continue
        xs, ys, zs = [p.x for p in corners], [p.y for p in corners], [p.z for p in corners]
        for k in range(5):
            mesh = bpy.data.meshes.new(f"{name}-drop-{drops}-mesh")
            # tiny icosphere via uv-sphere-ish diamond
            s = 0.012
            dv = [(0, 0, s), (s, 0, 0), (0, s, 0), (-s, 0, 0), (0, -s, 0), (0, 0, -s)]
            df = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1), (5, 2, 1), (5, 3, 2), (5, 4, 3), (5, 1, 4)]
            mesh.from_pydata(dv, [], df)
            mesh.update()
            mesh.materials.append(mat)
            ob = bpy.data.objects.new(f"{name} drop {drops+1}", mesh)
            ob.location = (
                rng.uniform(min(xs), max(xs)),
                rng.uniform(min(ys), max(ys)),
                rng.uniform(min(zs), max(zs)),
            )
            col.objects.link(ob)
            ob["hunch_role"] = "droplet"
            drops += 1
    return count, drops


def iter_collection(col):
    for o in col.objects:
        yield o
    for ch in col.children:
        yield from iter_collection(ch)


def source_root_collection():
    """The original building root: prefer a collection that holds 'structure'."""
    for name in ("structure", "rooms", "glazing"):
        c = bpy.data.collections.get(name)
        if c is not None:
            # link siblings by returning the scene master if they sit there
            return None
    return None


def enhance_existing_materials(scope_objects):
    n = 0
    seen = set()
    for ob in scope_objects:
        if ob.type != "MESH" or not ob.data:
            continue
        for mat in ob.data.materials:
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            if enhance_pbr(mat):
                n += 1
    return n


def ensure_base_look(scene=None, scope_objects=None):
    """Give the working scene a default sky and tighten existing PBR.

    Does not change geometry. Sets a world only if the scene has none, or if the
    current world is an earlier tingle base world. The original file is not saved.
    """
    scene = scene or bpy.context.scene
    log = {"world": False, "materials": 0, "sun": False}
    if scene.world is None or (scene.world.get("tingle_look") if hasattr(scene.world, "get") else False):
        world = _ensure_world("tingle base sky", SUN["clear_noon"])
        try:
            world["tingle_look"] = "base"
        except Exception:
            pass
        scene.world = world
        log["world"] = True
    objs = list(scope_objects) if scope_objects is not None else list(scene.objects)
    log["materials"] = enhance_existing_materials(objs)
    # leave an existing scene sun in place for the default look; condition scenes add their own
    return log


def plan_scene_setup(target="calm"):
    target = (target or "calm").lower()
    setup = SCENE_SETUPS.get(target) or SCENE_SETUPS["calm"]
    cond = setup["condition"]
    return {
        "target": target,
        "condition": cond,
        "label": next((c["label"] for c in CONDITION_SPECS if c["id"] == cond), cond),
        "when": WHEN.get(cond),
        "sun": SUN.get(cond),
        "light_energy": setup.get("light_energy"),
        "light_color": setup.get("light_color"),
        "camera": setup.get("camera"),
        "reason": setup.get("reason"),
        "note": "applied only on a variant or in a condition scene. the original stays as it is.",
    }


def apply_setup_to_variant(root, target, camera=None):
    """Add variant-local lights; world/sun are swapped at render time via a condition scene."""
    plan = plan_scene_setup(target)
    objs = list(iter_collection(root)) if hasattr(root, "objects") else []
    mn, mx = world_bbox(objs)
    lights = _add_interior_lights(
        root,
        f"{root.name} setup",
        mn, mx,
        energy=float(plan.get("light_energy") or 40),
        color=tuple(plan.get("light_color") or (1, 0.8, 0.6)),
    )
    # tag a preferred condition id on the variant
    try:
        root["tingle_setup_condition"] = plan["condition"]
        root["tingle_setup_reason"] = plan["reason"]
    except Exception:
        pass
    return {"plan": plan, "lights": [o.name for o in lights], "camera": camera.name if camera else None}


def _link_collection(scene, col):
    if col is None:
        return
    if col.name not in scene.collection.children and col not in list(scene.collection.children):
        try:
            scene.collection.children.link(col)
        except RuntimeError:
            pass


def _copy_collection(src, name):
    """Shallow-new collection with copied objects (meshes and materials copied)."""
    dest = bpy.data.collections.new(name)
    mapping = {}

    def walk(src_col, dest_col):
        for obj in src_col.objects:
            if obj.name in mapping:
                new = mapping[obj.name]
            else:
                new = obj.copy()
                if obj.data:
                    new.data = obj.data.copy()
                    if hasattr(new.data, "materials"):
                        for i, mat in enumerate(list(new.data.materials)):
                            if mat is not None:
                                new.data.materials[i] = mat.copy()
                mapping[obj.name] = new
            try:
                dest_col.objects.link(new)
            except RuntimeError:
                pass
        for ch in src_col.children:
            sub = bpy.data.collections.new(f"{dest_col.name} / {ch.name}")
            dest_col.children.link(sub)
            walk(ch, sub)

    walk(src, dest)
    # remap parents
    name_to_new = {k: v for k, v in mapping.items()}
    for orig_name, new in name_to_new.items():
        orig = bpy.data.objects.get(orig_name)
        if orig is not None and orig.parent is not None and orig.parent.name in name_to_new:
            mw = new.matrix_world.copy()
            new.parent = name_to_new[orig.parent.name]
            new.matrix_world = mw
    return dest, list(name_to_new.values())


def _collections_to_instance(scope):
    """Collections that represent the building to show in a condition scene."""
    if scope and scope not in ("", "original", "all"):
        col = bpy.data.collections.get(scope) if isinstance(scope, str) else scope
        if col is not None:
            return [col]
    # original sibling collections
    found = []
    for n in ("structure", "roof_louvers", "glazing", "rooms", "deck", "battery", "fittings"):
        c = bpy.data.collections.get(n)
        if c is not None:
            found.append(c)
    if found:
        return found
    return [bpy.context.scene.collection]


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


def configure_eevee(scene, samples=24):
    scene.render.engine = _engine_id()
    ee = getattr(scene, "eevee", None)
    if ee is None:
        return
    if hasattr(ee, "taa_render_samples"):
        ee.taa_render_samples = int(samples)
    for flag in ("use_volumetric", "use_volumetric_shadows", "use_raytracing", "use_shadows"):
        if hasattr(ee, flag):
            try:
                setattr(ee, flag, True)
            except Exception:
                pass
    if hasattr(ee, "volumetric_tile_size"):
        try:
            ee.volumetric_tile_size = "4"
        except Exception:
            pass
    if hasattr(ee, "volumetric_end"):
        ee.volumetric_end = 40.0


def copy_camera(src, name, col):
    if src is None:
        return None
    cam = src.copy()
    if src.data:
        cam.data = src.data.copy()
    cam.name = name
    try:
        col.objects.link(cam)
    except RuntimeError:
        pass
    cam["hunch_role"] = "setup"
    return cam


def build_condition_scenes(context, scope="original", camera=None, which=None, out_dir=None, samples=24):
    """Create one Blender scene per condition, optionally render matching-camera stills.

    Linking is used when only world/sun/volume change. Rain (wet materials) gets a
    copied collection so the original materials stay dry. Returns a list of records.
    """
    specs = [s for s in CONDITION_SPECS if not which or s["id"] in which]
    src_cols = _collections_to_instance(scope)
    # objects for bbox: prefer scope objects
    from . import feel_loop as FL
    scope_objs = list(FL.iter_scope(context, scope))
    mn, mx = world_bbox(scope_objs)
    cam_src = camera or context.scene.camera
    out = Path(out_dir) if out_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    records = []

    # hide original suns at render time per-scene via a local exclusive collection
    for spec in specs:
        cid = spec["id"]
        sname = f"cond {spec['label']}"
        # replace a previous build of the same condition for this scope
        old = bpy.data.scenes.get(sname)
        if old is not None:
            try:
                bpy.data.scenes.remove(old)
            except Exception:
                sname = f"cond {spec['label']} {scope}"
                old = bpy.data.scenes.get(sname)
                if old is not None:
                    try:
                        bpy.data.scenes.remove(old)
                    except Exception:
                        pass
        scene = bpy.data.scenes.new(sname)
        scene["tingle_condition"] = cid
        scene["tingle_scope"] = str(scope)
        exclusive = bpy.data.collections.new(f"{sname} extras")
        scene.collection.children.link(exclusive)

        copied = None
        used_cols = src_cols
        if spec.get("needs_copy"):
            # one combined copy of the first / all source collections
            if len(src_cols) == 1:
                copied, copies = _copy_collection(src_cols[0], f"{sname} building")
            else:
                holder = bpy.data.collections.new(f"{sname} building")
                copies = []
                for c in src_cols:
                    sub, objs = _copy_collection(c, f"{sname} / {c.name}")
                    holder.children.link(sub)
                    copies.extend(objs)
                copied = holder
            scene.collection.children.link(copied)
            used_cols = [copied]
            if spec.get("wet"):
                for ob in copies:
                    if ob.type != "MESH" or not ob.data:
                        continue
                    for i, mat in enumerate(list(ob.data.materials)):
                        wet = make_wet_material(mat)
                        if wet is not None:
                            ob.data.materials[i] = wet
        else:
            for c in src_cols:
                _link_collection(scene, c)

        sun_spec = SUN[cid]
        scene.world = _ensure_world(
            f"world {sname}",
            sun_spec,
            fog=bool(spec.get("fog")),
            overcast=cid in ("overcast", "rain"),
            night=cid == "night",
        )
        _add_sun(exclusive, f"{sname} sun", sun_spec, mn, mx)
        if spec.get("interior") or cid == "night":
            _add_interior_lights(exclusive, f"{sname} interior", mn, mx, energy=1600.0 if cid == "night" else 80.0)
            if cid == "night":
                # faint moonlight so the exterior silhouette is readable
                moon = dict(sun_spec)
                moon["energy"] = 0.12
                moon["color"] = (0.55, 0.65, 0.95)
                moon["alt"] = 25.0
                moon["az"] = 40.0
                _add_sun(exclusive, f"{sname} moon", moon, mn, mx)
        if spec.get("fog"):
            # world volume + dusty sky already applied; a mesh volume around the
            # camera rendered black in EEVEE, so we do not add one.
            pass
        if spec.get("water"):
            _add_water(exclusive, f"{sname} water", mn, mx)
        if spec.get("streaks"):
            src_objs = copies if spec.get("needs_copy") else scope_objs
            glaze = []
            for o in src_objs:
                if o.type != "MESH":
                    continue
                nam = (o.name or "").lower()
                mats = [((m.name if m else "") or "").lower() for m in (o.data.materials if o.data else [])]
                if "glaz" in nam or "glass" in nam or any("glaz" in m or "glass" in m for m in mats):
                    glaze.append(o)
            _add_rain(exclusive, f"{sname} rain", mn, mx, glaze)

        cam = copy_camera(cam_src, f"{sname} camera", exclusive)
        if cam is not None:
            scene.camera = cam

        configure_eevee(scene, samples=samples)

        rec = {
            "id": cid,
            "label": spec["label"],
            "blurb": spec["blurb"],
            "scene": scene.name,
            "when": WHEN.get(cid),
            "needs_copy": bool(spec.get("needs_copy")),
            "camera": cam.name if cam else (cam_src.name if cam_src else None),
            "scope": (copied.name if copied is not None else str(scope)),
            "render": None,
            "render_seconds": None,
            "samples": samples,
            "engine": scene.render.engine,
        }
        if out and scene.camera is not None:
            path = out / f"cond-{cid}.png"
            rec["render"], rec["render_seconds"] = render_scene(scene, path, samples=samples, hide_suns_outside=exclusive)
        records.append(rec)

    payload = {"scope": str(scope), "conditions": records, "note": (
        "each condition is a real Blender scene with a matching camera. "
        "rain uses copied wet materials; other conditions instance the building collection. "
        "the original collection is not permanently changed. "
        "hunch will read geometry plus, when a still is provided, pixel conditions. "
        "this is not a climate or weather simulation."
    )}
    _set_text_json("tingle_conditions.json", payload)
    return payload


def render_scene(scene, path, samples=24, hide_suns_outside=None, resolution=(960, 720)):
    """Render a specific scene; temporarily hide SUN lamps not in the condition extras."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_window_scene = None
    window = bpy.context.window
    hidden = []
    t0 = time.perf_counter()
    try:
        if window is not None:
            prev_window_scene = window.scene
            window.scene = scene
        configure_eevee(scene, samples=samples)
        scene.render.resolution_x = int(resolution[0])
        scene.render.resolution_y = int(resolution[1])
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(path)
        # hide other suns so the original scene sun does not light a night scene
        allow = set()
        if hide_suns_outside is not None:
            allow = {o.name for o in iter_collection(hide_suns_outside)}
        for ob in scene.objects:
            if ob.type == "LIGHT" and getattr(ob.data, "type", "") == "SUN":
                if allow and ob.name not in allow:
                    hidden.append((ob, ob.hide_render))
                    ob.hide_render = True
        override = {"scene": scene, "window": window} if window is not None else {"scene": scene}
        try:
            with bpy.context.temp_override(**{k: v for k, v in override.items() if v is not None}):
                bpy.ops.render.render(write_still=True)
        except Exception:
            # last resort: render anyway from the current context
            bpy.ops.render.render(write_still=True)
    finally:
        for ob, h in hidden:
            ob.hide_render = h
        if window is not None and prev_window_scene is not None:
            try:
                window.scene = prev_window_scene
            except Exception:
                pass
    elapsed = time.perf_counter() - t0
    return (str(path) if path.is_file() else None), round(elapsed, 3)


def read_conditions(context, records, focus_room="", target="calm"):
    """Run hunch on each condition scene, feeding the rendered still when present."""
    from . import hunch_panel as HP
    from . import hunch_scene as HS

    st = context.scene.hunch
    out = []
    prev = context.window.scene if context.window else None
    try:
        for rec in records:
            scene = bpy.data.scenes.get(rec["scene"])
            if scene is None:
                rec = dict(rec, error="scene missing")
                out.append(rec)
                continue
            if context.window:
                context.window.scene = scene
            # extract from this scene, scoped to the same building
            site = st.site if st.site != "custom" else (st.site_custom or "")
            snap = HS.extract_snapshot(
                context,
                site=site,
                orient_deg=st.orient_deg,
                target=target or st.target,
                focus_room=focus_room or (st.room if st.room != "none" else ""),
                when=rec.get("when"),
                scope=rec.get("scope") or "original",
            )
            if rec.get("render"):
                snap["viewpoint_image"] = rec["render"]
            snap["hunch_root"] = HP._hunch_root(context)
            snap["condition"] = rec.get("id")
            try:
                HP._run_worker(context, snap, None, True)
                result = json.loads((bpy.data.texts.get("hunch_last_result.json").as_string() or "{}"))
            except Exception as exc:
                rec = dict(rec, error=str(exc)[:300])
                out.append(rec)
                continue
            focus = result.get("focus") or {}
            atm = focus.get("atmosphere") or {}
            rec = dict(rec)
            rec["feel"] = atm.get("predicted_feel")
            rec["scores"] = atm.get("scores") or {}
            rec["breakdown"] = atm.get("breakdown") or ""
            rec["image_read"] = result.get("image_read") or {}
            rec["features"] = {
                "ceiling_height": (focus.get("room") or {}).get("ceiling_height"),
                "window_to_wall": (focus.get("room") or {}).get("window_to_wall"),
                "material_warmth": (focus.get("room") or {}).get("material_warmth"),
            }
            out.append(rec)
    finally:
        if context.window and prev is not None:
            try:
                context.window.scene = prev
            except Exception:
                pass
    return out


def wink_pick_conditions(reads, target, top_n=3):
    """Rank conditions by the hunch target-feel score.

    This is a wink-shaped pick from the condition reads, not a trained weather
    policy and not wink.plan. Honest: highest target score, with a bonus when
    the predicted feel already matches the target (mood).
    """
    target = (target or "").lower()
    ranked = []
    for rec in reads:
        if rec.get("error"):
            continue
        scores = rec.get("scores") or {}
        try:
            s = float(scores.get(target) or 0.0)
        except (TypeError, ValueError):
            s = 0.0
        bonus = 8.0 if (rec.get("feel") or "").lower() == target else 0.0
        ranked.append((s + bonus, s, rec))
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    picked = [r[2] for r in ranked[: max(1, int(top_n))]]
    return {
        "target": target,
        "picked": [{"id": p["id"], "label": p["label"], "feel": p.get("feel"), "score": (p.get("scores") or {}).get(target)} for p in picked],
        "ranking": [
            {"id": r[2]["id"], "label": r[2]["label"], "feel": r[2].get("feel"), "target_score": r[1]}
            for r in ranked
        ],
        "method": "ranked by hunch target score under each condition, with a bonus when the predicted feel already matches the target. not a trained wink weather policy.",
    }


def generate_and_read(context, scope="original", camera=None, out_dir=None, target="calm",
                      focus_room="", samples=24, which=None):
    """Build condition scenes, render them, let hunch read each, then pick for the target mood."""
    built = build_condition_scenes(
        context, scope=scope, camera=camera, which=which, out_dir=out_dir, samples=samples,
    )
    reads = read_conditions(context, built["conditions"], focus_room=focus_room, target=target)
    pick = wink_pick_conditions(reads, target)
    payload = {
        "scope": str(scope),
        "target": target,
        "conditions": reads,
        "pick": pick,
        "note": built.get("note"),
        "consumed": (
            "hunch scores geometry, materials, solar position from `when`, site climate, "
            "and, when a still was written, pixel brightness/warmth/contrast from the render. "
            "studio image head is used only if trained weights are present. "
            "this is not a compliance daylight, energy, or weather model."
        ),
    }
    _set_text_json("tingle_conditions.json", payload)
    # also stash on the feel-loop state when it exists
    state = _text_json("feel_loop_state.json") or {}
    state["conditions"] = payload
    _set_text_json("feel_loop_state.json", state)
    return payload



def condition_hint(cond_id):
    for spec in CONDITION_SPECS:
        if spec["id"] == cond_id:
            return spec.get("hint") or spec.get("blurb") or ""
    return ""


def _mesh_world_area(ob):
    if ob is None or ob.type != "MESH" or ob.data is None:
        return 0.0
    try:
        sx, sy, sz = ob.matrix_world.to_scale()
        s = (abs(sx) + abs(sy) + abs(sz)) / 3.0
        return float(sum(p.area for p in ob.data.polygons)) * (s * s)
    except Exception:
        return 0.0


def detect_space(context, scope="original"):
    """Cheap read of rooms, glazing and orientation from the current building."""
    from . import hunch_scene as HS
    from . import feel_loop as FL
    rooms = HS.discover_rooms(context, scope=scope) or []
    objs = [o for o in FL.iter_scope(context, scope) if o.type == "MESH" and not o.hide_get()]
    glass = [o for o in objs if HS.is_glazing(o)]
    mn, mx = world_bbox(objs)
    span = (mx.x - mn.x, mx.y - mn.y, mx.z - mn.z)
    if span[1] >= span[0]:
        long_axis = "Y"
        orient = "the long side runs along +Y (model +X is the short side)"
    else:
        long_axis = "X"
        orient = "the long side runs along +X"
    heights = []
    for r in rooms:
        try:
            heights.append(float(r["max"][2]) - float(r["min"][2]))
        except Exception:
            continue
    max_h = max(heights) if heights else float(span[2])
    glass_area = sum(_mesh_world_area(o) for o in glass)
    # vertical envelope as a stand-in for wall area (physically rough, for the offer only)
    wall_area = 2.0 * (span[0] + span[1]) * max(0.5, max_h)
    wwr = (glass_area / wall_area) if wall_area > 1e-3 else 0.0
    return {
        "rooms": [r.get("name") for r in rooms if r.get("name")],
        "n_rooms": len(rooms),
        "n_glass": len(glass),
        "wwr": round(wwr, 3),
        "max_height": round(max_h, 2),
        "long_axis": long_axis,
        "orientation": orient,
        "span": [round(v, 2) for v in span],
    }


def suggest_first_scene(context, scope="original"):
    """One ready-made starting scene (condition + mood) from what is in the file."""
    det = detect_space(context, scope)
    wwr = float(det.get("wwr") or 0)
    height = float(det.get("max_height") or 0)
    n_rooms = int(det.get("n_rooms") or 0)
    names = ", ".join(det.get("rooms") or []) or "the space"
    glaze = "heavily glazed" if wwr >= 0.28 else ("lightly glazed" if wwr <= 0.12 else "with a mix of wall and glass")
    if wwr >= 0.28 or height >= 3.6:
        mood, cond = "calm", "golden_hour"
        reason = (
            f"{names}: {n_rooms} room(s), {glaze}, "
            f"ceiling around {height:.1f} m. {det['orientation']}. "
            "a tall, very glazed space often already reads as open; "
            "golden hour toward calm is a ready starting scene."
        )
    elif wwr <= 0.12 and n_rooms:
        mood, cond = "open", "clear_noon"
        reason = (
            f"{names}: {n_rooms} room(s), {glaze}, "
            f"ceiling around {height:.1f} m. {det['orientation']}. "
            "clear noon toward a more open read is a ready starting scene."
        )
    else:
        mood, cond = "calm", "golden_hour"
        reason = (
            f"{names}: {n_rooms} room(s), {glaze}, "
            f"ceiling around {height:.1f} m. {det['orientation']}. "
            "golden hour toward calm is a ready starting scene."
        )
    spec = next((c for c in CONDITION_SPECS if c["id"] == cond), {})
    return {
        **det,
        "mood": mood,
        "condition": cond,
        "label": spec.get("label") or cond,
        "hint": spec.get("hint") or "",
        "reason": reason,
    }


def preview_condition(scene, cond_id):
    """In-session sky and sun preview on the working scene, without saving the file.

    Hidden original suns are remembered so the preview can be swapped. Rain wet
    materials and the water plane stay in their own condition scenes; this
    preview only changes world and a tagged preview sun.
    """
    spec = next((c for c in CONDITION_SPECS if c["id"] == cond_id), None)
    sun = SUN.get(cond_id) or SUN["clear_noon"]
    if spec is None:
        raise ValueError(f"unknown condition: {cond_id}")
    if not scene.get("tingle_preview_active"):
        scene["tingle_preview_world"] = scene.world.name if scene.world else ""
        hidden = []
        for ob in list(scene.objects):
            if ob.type == "LIGHT" and ob.data and ob.data.type == "SUN" and "tingle preview" not in ob.name:
                if not ob.hide_get():
                    hidden.append(ob.name)
                    try:
                        ob.hide_set(True)
                    except Exception:
                        pass
                    ob.hide_render = True
        scene["tingle_preview_hidden_suns"] = hidden
    fog = bool(spec.get("fog"))
    overcast = cond_id == "overcast"
    night = cond_id == "night"
    world = _ensure_world(f"tingle preview {cond_id}", sun, fog=fog, overcast=overcast, night=night)
    scene.world = world
    sun_ob = scene.objects.get("tingle preview sun")
    if sun_ob is None or sun_ob.type != "LIGHT":
        data = bpy.data.lights.new("tingle preview sun", "SUN")
        sun_ob = bpy.data.objects.new("tingle preview sun", data)
        try:
            scene.collection.objects.link(sun_ob)
        except RuntimeError:
            pass
        try:
            sun_ob["hunch_role"] = "setup"
        except Exception:
            pass
    sun_ob.data.energy = float(sun.get("energy") or 3)
    try:
        sun_ob.data.color = sun.get("color") or (1.0, 1.0, 1.0)
    except Exception:
        pass
    sun_ob.rotation_euler = _sun_euler(float(sun.get("alt") or 45), float(sun.get("az") or 180))
    try:
        sun_ob.hide_set(False)
    except Exception:
        pass
    sun_ob.hide_render = False
    scene["tingle_preview_active"] = cond_id
    return {"condition": cond_id, "world": world.name, "sun": sun_ob.name}


def apply_sun_angles(scene, alt_deg, az_deg, energy=None, color=None):
    """Move the in-session preview sun (never the original scene sun)."""
    sun_ob = scene.objects.get("tingle preview sun")
    if sun_ob is None or sun_ob.type != "LIGHT":
        return False
    sun_ob.rotation_euler = _sun_euler(float(alt_deg), float(az_deg))
    if energy is not None:
        sun_ob.data.energy = float(energy)
    if color is not None:
        try:
            sun_ob.data.color = tuple(color)[:3]
        except Exception:
            pass
    if scene.world is not None:
        try:
            for node in scene.world.node_tree.nodes:
                if node.type == "TEX_SKY":
                    if hasattr(node, "sun_elevation"):
                        node.sun_elevation = max(0.0, math.radians(float(alt_deg)))
                    if hasattr(node, "sun_rotation"):
                        node.sun_rotation = math.radians(float(az_deg))
        except Exception:
            pass
    return True
