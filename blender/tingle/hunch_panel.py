"""Hunch section drawn inside the existing tinker and wink N-panels (no new tab, no new add-on).

Scan the scene, pick a site and a room, place a viewpoint, run hunch in a background
process, and write a report image. Copy is lowercase. The read is a prediction of how
the space tends to feel.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty,
)

from . import hunch_scene as HS
from . import feel_loop as FL

_RESULT_TEXT = "hunch_last_result.json"
_ROOMS_TEXT = "hunch_rooms.json"
_FEELS = [
    ("calm", "calm", "a calmer predicted atmosphere"),
    ("open", "open", "a more open predicted atmosphere"),
    ("cozy", "cozy", "a cozier predicted atmosphere"),
    ("focused", "focused", "a more focused predicted atmosphere"),
    ("energized", "energized", "a more energized predicted atmosphere"),
    ("closed-in", "closed-in", "a more closed-in predicted atmosphere"),
    ("uneasy", "uneasy", "an uneasy predicted atmosphere (experimental)"),
]
_SITES = [
    ("Accra", "Accra", "Accra, Ghana"),
    ("Boston", "Boston", "Boston, Massachusetts"),
    ("London", "London", "London, United Kingdom"),
    ("custom", "other place", "type a place name or lat,lon in the field below"),
]

# process state (module-level; one run at a time)
_PROC = None
_PATHS = None


def _text_json(name, default=None):
    t = bpy.data.texts.get(name)
    if t is None:
        return default
    try:
        return json.loads(t.as_string())
    except ValueError:
        return default


def _set_text_json(name, value):
    t = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    t.clear()
    t.write(json.dumps(value, indent=2) if not isinstance(value, str) else value)


def _st(context):
    return context.scene.hunch


def _room_items(self, context):
    rooms = _text_json(_ROOMS_TEXT, []) or []
    if not rooms:
        return [("none", "(scan the scene first)", "")]
    return [(r, r, "") for r in rooms]


def _python(context) -> str:
    st = _st(context)
    candidates = [
        st.service_python,
        os.environ.get("HUNCH_PYTHON"),
        os.environ.get("TW_PYTHON"),
        os.path.expanduser("~/.tingle/venv/bin/python"),
    ]
    # reuse tinker add-on pref if the user already set a service python
    addon = context.preferences.addons.get("tinker_blender") or context.preferences.addons.get(__package__)
    if addon and hasattr(addon, "preferences") and hasattr(addon.preferences, "service_python"):
        candidates.insert(0, addon.preferences.service_python)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return sys.executable


def _hunch_root(context) -> str:
    st = _st(context)
    for c in [st.hunch_root, os.environ.get("HUNCH_ROOT"), os.path.expanduser("~/.tingle/hunch")]:
        if c and os.path.isfile(os.path.join(c, "hunch", "atmosphere.py")):
            return c
    return st.hunch_root or ""


def _snapshot(context) -> dict:
    st = _st(context)
    site = st.site if st.site != "custom" else (st.site_custom or "")
    person = None
    if st.viewpoint is not None:
        loc = st.viewpoint.matrix_world.translation
        if st.viewpoint.type == "CAMERA":
            person = {"feet": [loc.x, loc.y, loc.z - st.eye_height], "eye_height": st.eye_height}
        else:
            person = {"feet": [loc.x, loc.y, loc.z], "eye_height": st.eye_height}
    focus = st.room if st.room and st.room != "none" else ""
    snap = HS.extract_snapshot(
        context,
        site=site,
        orient_deg=st.orient_deg,
        target=st.target,
        focus_room=focus,
        person=person,
        when=st.when or None,
        scope=st.scope_collection or "original",
    )
    snap["hunch_root"] = _hunch_root(context)
    img = getattr(st, "viewpoint_image", "") or ""
    if img and os.path.isfile(img):
        snap["viewpoint_image"] = img
    return snap


class HunchState(bpy.types.PropertyGroup):
    site: EnumProperty(name="site", items=_SITES, default="custom")
    site_custom: StringProperty(name="place", default="", description="place name or lat,lon")
    room: EnumProperty(name="room", items=_room_items)
    viewpoint: PointerProperty(name="viewpoint", type=bpy.types.Object,
                               poll=lambda self, obj: obj.type in {"EMPTY", "CAMERA"})
    target: EnumProperty(name="target", items=_FEELS, default="calm")
    orient_deg: FloatProperty(name="orient", default=0.0, description="compass bearing of the model's +x axis, clockwise from north")
    eye_height: FloatProperty(name="eye height", default=1.6, min=0.4, max=2.4, unit="LENGTH")
    when: StringProperty(name="when", default="", description="optional ISO time, e.g. 2024-03-21T12:00:00+00:00")
    status: StringProperty(default="scan the scene, then run hunch")
    busy: BoolProperty(default=False)
    progress: FloatProperty(default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    last_feel: StringProperty(default="")
    last_breakdown: StringProperty(default="")
    report_path: StringProperty(name="report", subtype="FILE_PATH", default="")
    hunch_root: StringProperty(name="hunch root", subtype="DIR_PATH", default="")
    service_python: StringProperty(name="python", subtype="FILE_PATH", default="",
                                   description="python with hunch-adapter installed, used for the background run")
    loop_auto: BoolProperty(name="auto until target", default=False,
                            description="when running the feel loop, keep building variants until the target is reached or the iteration cap")
    loop_max_iter: IntProperty(name="max iterations", default=4, min=1, max=8)
    loop_iteration: IntProperty(default=0, options={"HIDDEN"})
    loop_waiting: BoolProperty(default=False, options={"HIDDEN"})
    scope_collection: StringProperty(default="", options={"HIDDEN"})
    loop_out: StringProperty(name="loop output", subtype="DIR_PATH", default="//feel-loop")

    viewpoint_image: StringProperty(default="", options={"HIDDEN"})

    # first-run offer + simple/advanced + share
    condition: EnumProperty(
        name="condition",
        items=(
            ("clear_noon", "clear noon", "overhead sun shortens shadows and brightens surfaces"),
            ("golden_hour", "golden hour", "low sun stretches shadows and warms surfaces"),
            ("overcast", "overcast", "a soft sky evens light and cuts hard shadow"),
            ("rain", "rain", "wet surfaces darken and glass picks up streaks"),
            ("harmattan", "harmattan haze", "dusty air warms the sky and softens the sun"),
            ("night", "night, interior light", "interior light takes over when the sun is gone"),
            ("on_water", "on water", "a water plane brightens the underside and the horizon"),
        ),
        default="golden_hour",
    )
    show_advanced: BoolProperty(
        name="advanced", default=False,
        description="show the full knobs and the step-by-step flow",
    )
    share_run: BoolProperty(
        name="share this run", default=False,
        description="when on, and a share URL is set, an anonymised summary is sent to that URL (no names, paths or photos). off by default.",
    )
    share_url: StringProperty(
        name="share URL", default="",
        description="optional URL to post an anonymised run summary. empty means no network posting. can also be set with TINGLE_SHARE_URL.",
    )
    share_is_test: BoolProperty(default=False, options={"HIDDEN"})
    share_note: StringProperty(default="", options={"HIDDEN"})
    user_feedback: EnumProperty(
        name="does this read feel right",
        items=(
            ("none", "(no feedback)", "leave blank"),
            ("agree", "agree", "the predicted feel matches how the space tends to read"),
            ("disagree", "disagree", "the predicted feel does not match how the space tends to read"),
        ),
        default="none",
    )
    offer_ready: BoolProperty(default=False, options={"HIDDEN"})
    offer_dismissed: BoolProperty(default=False, options={"HIDDEN"})
    offer_used: BoolProperty(default=False, options={"HIDDEN"})
    offer_mood: StringProperty(default="")
    offer_condition: StringProperty(default="")
    offer_label: StringProperty(default="")
    offer_text: StringProperty(default="")
    offer_hint: StringProperty(default="")
    offer_rooms: StringProperty(default="")
    offer_orient: StringProperty(default="")
    sun_altitude: FloatProperty(name="sun altitude", default=12.0, min=-20.0, max=90.0, description="degrees above the horizon")
    sun_azimuth: FloatProperty(name="sun azimuth", default=270.0, min=0.0, max=360.0, description="0 is north, clockwise")
    light_temp_k: FloatProperty(name="light temperature", default=3000.0, min=1800.0, max=6500.0, description="kelvin")
    light_energy: FloatProperty(name="light intensity", default=40.0, min=0.0, max=2000.0)
    mat_roughness: FloatProperty(name="roughness", default=0.45, min=0.0, max=1.0)
    mat_colour: FloatVectorProperty(name="colour", subtype="COLOR", size=3, min=0.0, max=1.0, default=(0.72, 0.68, 0.60))
    mat_transmission: FloatProperty(name="transmission", default=0.0, min=0.0, max=1.0)
    camera_path: EnumProperty(
        name="camera",
        items=(
            ("still", "still", "keep the current camera"),
            ("loft", "loft interior", "look from the loft camera"),
            ("deck", "deck", "look from the deck camera"),
            ("exterior", "exterior", "look from the exterior camera"),
            ("both", "original and variant", "overview of both buildings"),
        ),
        default="still",
    )
    knob_louver: FloatProperty(name="louvers", default=0.0, min=0.0, max=1.0, description="0 open, 1 closed")
    knob_door: FloatProperty(name="door", default=0.85, min=0.0, max=1.6, description="radians; 0 is closed")
    knob_arch: FloatProperty(name="arch screen", default=0.0, min=0.0, max=1.6, description="radians; 0 is closed")
    knob_fan_on: BoolProperty(name="fan on", default=False)
    knob_fan_speed: FloatProperty(name="fan speed", default=0.35, min=0.0, max=2.0)
    knob_couch: FloatProperty(name="couch slide", default=0.0, min=-2.0, max=2.0, unit="LENGTH")


class HUNCH_OT_scan(bpy.types.Operator):
    bl_idname = "hunch.scan"
    bl_label = "Find rooms"
    bl_description = "Read rooms (closed meshes) and glazing (by name or material) in the current scene"

    def execute(self, context):
        st = _st(context)
        rooms = HS.discover_rooms(context, scope=st.scope_collection or 'original')
        names = [r["name"] for r in rooms]
        _set_text_json(_ROOMS_TEXT, names)
        # force the enum to a valid item
        if names and (st.room not in names or st.room in ("", "none")):
            st.room = names[0]
        n_glass = sum(1 for o in context.scene.objects if o.type == "MESH" and not o.hide_get() and HS.is_glazing(o))
        if names:
            st.status = f"rooms: {', '.join(names)}  ·  {n_glass} glazing object(s)"
        else:
            st.status = "no room shells found. add closed meshes in a 'rooms' collection, or name them as rooms."
            self.report({"WARNING"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_place_viewpoint(bpy.types.Operator):
    bl_idname = "hunch.place_viewpoint"
    bl_label = "Place viewpoint"
    bl_description = "Put an empty at standing height in the selected room (or use the active camera / empty)"

    def execute(self, context):
        st = _st(context)
        obj = context.active_object
        if obj is not None and obj.type in {"EMPTY", "CAMERA"}:
            st.viewpoint = obj
            st.status = f"viewpoint: {obj.name}"
            return {"FINISHED"}
        rooms = {r["name"]: r for r in HS.discover_rooms(context)}
        room = rooms.get(st.room)
        if room is None:
            self.report({"ERROR"}, "scan the scene and pick a room first")
            return {"CANCELLED"}
        mn, mx = room["min"], room["max"]
        x = 0.5 * (mn[0] + mx[0])
        y = 0.5 * (mn[1] + mx[1])
        z = mn[2] + 0.1  # feet just above the floor
        existing = context.scene.objects.get("hunch viewpoint")
        if existing is not None:
            bpy.data.objects.remove(existing, do_unlink=True)
        empty = bpy.data.objects.new("hunch viewpoint", None)
        empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = 0.4
        empty.location = (x, y, z)
        context.scene.collection.objects.link(empty)
        st.viewpoint = empty
        st.status = f"viewpoint in {room['name']} at standing height"
        return {"FINISHED"}


def _poll_worker():
    global _PROC, _PATHS
    ctx = bpy.context
    if ctx is None or ctx.scene is None or not hasattr(ctx.scene, "hunch"):
        return 0.3
    st = ctx.scene.hunch
    if _PATHS is None:
        return None
    prog = _PATHS.get("progress")
    if prog and os.path.isfile(prog):
        try:
            data = json.loads(Path(prog).read_text(encoding="utf-8"))
            st.progress = float(data.get("progress") or st.progress)
            if data.get("status"):
                st.status = str(data["status"])
        except (OSError, ValueError):
            pass
    proc = _PROC
    if proc is not None and proc.poll() is None:
        st.progress = min(0.95, max(st.progress, 0.1) + 0.01)
        for w in ctx.window_manager.windows:
            for a in w.screen.areas:
                a.tag_redraw()
        return 0.25
    # finished or missing
    out = _PATHS.get("output")
    report = _PATHS.get("report")
    _PROC = None
    _PATHS = None
    st.busy = False
    st.progress = 1.0
    if out and os.path.isfile(out):
        try:
            payload = json.loads(Path(out).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            st.status = f"could not read the hunch result: {e}"
            return None
        if not payload.get("ok"):
            st.status = f"run failed: {payload.get('error') or 'unknown error'}"
            return None
        _apply_result(ctx, payload["result"], report)
    else:
        err = ""
        if proc is not None and proc.stderr:
            try:
                err = proc.stderr.read().decode("utf-8", "replace")[-400:]
            except Exception:
                err = ""
        st.status = "run failed: no result file" + (f" ({err})" if err else "")
    for w in ctx.window_manager.windows:
        for a in w.screen.areas:
            a.tag_redraw()
    return None


def _apply_result(context, result, report_path=None):
    st = _st(context)
    _set_text_json(_RESULT_TEXT, result)
    focus = result.get("focus") or (result.get("rooms") or [{}])[0]
    atm = (focus or {}).get("atmosphere") or {}
    st.last_feel = str(atm.get("predicted_feel") or "")
    st.last_breakdown = str(atm.get("breakdown") or result.get("summary") or "")
    if report_path and os.path.isfile(report_path):
        st.report_path = report_path
    n = len(result.get("rooms") or [])
    feel = st.last_feel or "unscored"
    st.status = f"predicted to read as {feel}  ·  {n} room(s)  ·  not a reading of a person"


def _run_worker(context, snap, report_path, blocking: bool) -> None:
    global _PROC, _PATHS
    st = _st(context)
    py = _python(context)
    root = _hunch_root(context)
    tmp = Path(tempfile.mkdtemp(prefix="hunch-run-"))
    inp, out, prog = tmp / "snap.json", tmp / "result.json", tmp / "progress.json"
    inp.write_text(json.dumps(snap), encoding="utf-8")
    cmd = [py, "-m", "hunch_adapter.worker", "--input", str(inp), "--output", str(out),
           "--progress", str(prog)]
    if root:
        cmd += ["--hunch-root", root]
    if report_path:
        cmd += ["--report", report_path]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    if blocking:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
        if not out.exists():
            raise RuntimeError((r.stderr or r.stdout or "worker produced no result")[-500:])
        payload = json.loads(out.read_text(encoding="utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "hunch worker failed")
        _apply_result(context, payload["result"], report_path)
        st.busy, st.progress = False, 1.0
        return
    st.busy, st.progress = True, 0.05
    st.status = "running hunch..."
    _PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, start_new_session=True)
    _PATHS = {"output": str(out), "progress": str(prog), "report": report_path}
    if not bpy.app.timers.is_registered(_poll_worker):
        bpy.app.timers.register(_poll_worker, first_interval=0.2)


class HUNCH_OT_run(bpy.types.Operator):
    bl_idname = "hunch.run"
    bl_label = "Sense"
    bl_description = "Predict how the selected room tends to feel. Runs in a background process."
    blocking: BoolProperty(default=False, options={"SKIP_SAVE"})
    write_report: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        st = _st(context)
        if st.busy:
            self.report({"WARNING"}, "hunch is already running")
            return {"CANCELLED"}
        if not (_text_json(_ROOMS_TEXT) or HS.discover_rooms(context)):
            bpy.ops.hunch.scan()
        snap = _snapshot(context)
        if not snap.get("faces"):
            st.status = "no geometry to read. scan the scene after adding room shells or glazing."
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        report = None
        if self.write_report:
            report = st.report_path or str(Path(tempfile.gettempdir()) / "hunch-report.png")
        try:
            _run_worker(context, snap, report, self.blocking)
        except Exception as e:
            st.busy = False
            st.status = f"run failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        return {"FINISHED"}


class HUNCH_OT_write_report(bpy.types.Operator):
    bl_idname = "hunch.write_report"
    bl_label = "Write report image"
    bl_description = "Write a PNG of the last hunch read, scores and suggestions"
    filepath: StringProperty(subtype="FILE_PATH", default="//hunch-report.png")

    def invoke(self, context, event):
        st = _st(context)
        if st.report_path:
            self.filepath = st.report_path
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        st = _st(context)
        result = _text_json(_RESULT_TEXT)
        if not result:
            # run first, then write
            self.report({"ERROR"}, "run hunch first")
            return {"CANCELLED"}
        path = bpy.path.abspath(self.filepath)
        try:
            # write via the same python as the worker so Pillow is available
            py = _python(context)
            tmp = Path(tempfile.mkdtemp(prefix="hunch-report-"))
            (tmp / "result.json").write_text(json.dumps(result), encoding="utf-8")
            script = (
                "import json,sys; from hunch_adapter.scene import write_report_image; "
                "r=json.load(open(sys.argv[1])); write_report_image(r, sys.argv[2])"
            )
            r = subprocess.run([py, "-c", script, str(tmp / "result.json"), path],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or "write failed")[-400:])
        except Exception as e:
            st.status = f"could not write the report: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        st.report_path = path
        st.status = f"wrote {path}"
        return {"FINISHED"}


class HUNCH_OT_cancel(bpy.types.Operator):
    bl_idname = "hunch.cancel"
    bl_label = "Cancel"
    bl_description = "Stop a background hunch run"

    def execute(self, context):
        global _PROC, _PATHS
        if _PROC is not None and _PROC.poll() is None:
            _PROC.terminate()
        _PROC, _PATHS = None, None
        st = _st(context)
        st.busy = False
        st.status = "cancelled"
        return {"FINISHED"}





class HUNCH_OT_scene_setup(bpy.types.Operator):
    bl_idname = "hunch.scene_setup"
    bl_label = "Set the scene"
    bl_description = "Plan camera, sun, sky and lights for the target feel. Base look goes on the working scene; feel-driven setup is applied only on a variant."

    def execute(self, context):
        st = _st(context)
        from . import scene_look as SL
        # geometry of the original is not changed; a missing world gets a default sky
        scope_name = st.scope_collection or "original"
        from . import feel_loop as _FL
        log = SL.ensure_base_look(context.scene, scope_objects=_FL.iter_scope(context, scope_name))
        plan = SL.plan_scene_setup(st.target)
        SL._set_text_json("tingle_scene_setup.json", {"plan": plan, "base_look": log})
        st.when = plan.get("when") or st.when
        st.status = (
            f"scene for {plan['target']}: {plan['label']}. {plan['reason']} "
            f"base look: {log['materials']} material(s) tightened"
            + (", default sky added" if log.get("world") else "")
            + ". feel-driven sun and lights are applied on the variant, not the original."
        )
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_conditions(bpy.types.Operator):
    bl_idname = "hunch.conditions"
    bl_label = "Conditions"
    bl_description = "Build weather/condition scenes on the materialled model, read each with hunch, and pick those that support the target feel"

    def execute(self, context):
        st = _st(context)
        from . import scene_look as SL
        from . import feel_loop as _FL
        SL.ensure_base_look(context.scene, scope_objects=_FL.iter_scope(context, st.scope_collection or "original"))
        out_dir = bpy.path.abspath(st.loop_out or "//feel-loop")
        if out_dir.startswith("//") or not st.loop_out:
            base = Path(bpy.data.filepath).parent if bpy.data.filepath else Path(tempfile.gettempdir())
            out_dir = str(Path(out_dir.replace("//", str(base) + "/")) if str(out_dir).startswith("//") else out_dir)
        out = Path(out_dir) / "conditions"
        out.mkdir(parents=True, exist_ok=True)
        cam = st.viewpoint if st.viewpoint is not None and getattr(st.viewpoint, "type", "") == "CAMERA" else context.scene.camera
        scope = st.scope_collection or "original"
        st.status = "building condition scenes..."
        try:
            payload = SL.generate_and_read(
                context,
                scope=scope,
                camera=cam,
                out_dir=out,
                target=st.target,
                focus_room=st.room if st.room != "none" else "",
                samples=16,
            )
        except Exception as exc:
            st.status = f"conditions failed: {exc}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        pick = (payload.get("pick") or {}).get("picked") or []
        bits = ", ".join(f"{p.get('label')} ({p.get('feel') or 'unscored'})" for p in pick) or "none"
        times = [c.get("render_seconds") for c in payload.get("conditions") or [] if c.get("render_seconds") is not None]
        tnote = f"  render {sum(times):.1f}s across {len(times)} still(s)." if times else ""
        st.status = f"conditions: wink picked {bits} toward {st.target}.{tnote} hunch reads geometry plus pixel conditions from each still."
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


class HUNCH_OT_feel_report(bpy.types.Operator):
    bl_idname = "hunch.feel_report"
    bl_label = "One-page feel report"
    bl_description = "Write a single-page PDF and PNG: the read, scores, climate chart, and feel-loop before/after if it ran"

    def execute(self, context):
        st = _st(context)
        result = _text_json(_RESULT_TEXT)
        if not result:
            self.report({"ERROR"}, "run hunch first")
            return {"CANCELLED"}
        out_dir = bpy.path.abspath(st.loop_out or "//feel-loop")
        if out_dir.startswith("//") or not st.loop_out:
            base = Path(bpy.data.filepath).parent if bpy.data.filepath else Path(tempfile.gettempdir())
            out_dir = str(base / "feel-loop")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # viewpoint still: reuse a feel-loop before render, or render the current camera
        viewpoint = None
        for cand in (out / "before.png", Path(result.get("report_image") or "")):
            if cand and cand.is_file():
                viewpoint = str(cand)
                break
        if viewpoint is None:
            cam = st.viewpoint if st.viewpoint is not None and st.viewpoint.type == "CAMERA" else context.scene.camera
            if cam is not None:
                try:
                    from . import feel_loop as _FL
                    viewpoint = _FL.render_view(context, cam, out / "report-viewpoint.png", samples=24)
                except Exception:
                    viewpoint = None
        loop = None
        loop_text = bpy.data.texts.get("feel_loop_state.json")
        if loop_text:
            try:
                loop = json.loads(loop_text.as_string() or "{}")
            except ValueError:
                loop = None
        payload = {
            "project": Path(bpy.data.filepath).stem if bpy.data.filepath else "scene",
            "room": st.room,
            "target": st.target,
            "site": result.get("site") or {},
            "hunch": result,
            "loop": loop or {},
            "viewpoint_image": viewpoint,
            "before": (loop or {}).get("before_render") or (str(out / "before.png") if (out / "before.png").is_file() else None),
            "after": (loop or {}).get("after_render") or (str(out / "after.png") if (out / "after.png").is_file() else None),
            "conditions": (loop or {}).get("conditions") or _text_json("tingle_conditions.json"),
        }
        pay = out / "feel-report-payload.json"
        pay.write_text(json.dumps(payload), encoding="utf-8")
        dest = out / "feel-report.pdf"
        py = _python(context)
        cmd = [py, "-m", "hunch_adapter.feel_report", "--input", str(pay), "--out", str(dest)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as e:
            st.status = f"feel report failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        if r.returncode != 0 or not dest.is_file():
            st.status = "feel report failed: " + ((r.stderr or r.stdout or "no pdf")[-300:])
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        st.report_path = str(dest)
        st.status = f"wrote one-page feel report  {dest}"
        self.report({"INFO"}, st.status)
        return {"FINISHED"}


def draw_hunch_section(layout, context):
    """A section, not a tab, for use inside the tinker and wink N-panels."""
    st = context.scene.hunch
    box = layout.box()
    box.label(text="hunch")
    box.label(text="the feeling + thinking machine")
    status = box.column()
    status.label(text=st.status, icon="INFO")
    if st.busy:
        status.progress(factor=st.progress, text="working...")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.prop(st, "site")
    if st.site == "custom":
        col.prop(st, "site_custom")
    col.prop(st, "room")
    col.prop(st, "viewpoint")
    col.prop(st, "target")
    col.prop(st, "orient_deg")
    row = col.row(align=True)
    row.operator("hunch.scan", icon="VIEWZOOM")
    row.operator("hunch.place_viewpoint", icon="EMPTY_AXIS")
    run = col.row(align=True)
    run.operator("hunch.run", icon="PLAY")
    if st.busy:
        run.operator("hunch.cancel", icon="X")
    row = col.row(align=True)
    row.operator("hunch.write_report", icon="FILE_IMAGE")
    row.operator("hunch.feel_report", icon="DOCUMENTS")
    if st.last_feel:
        res = box.column()
        res.label(text=f"predicted to read as {st.last_feel}")
        # wrap the breakdown
        text = st.last_breakdown or ""
        for i in range(0, min(len(text), 240), 56):
            res.label(text=text[i:i + 56])
        result = _text_json(_RESULT_TEXT) or {}
        atm = ((result.get("focus") or {}).get("atmosphere")) or {}
        scores = atm.get("scores") or {}
        if scores:
            for name, val in list(scores.items())[:7]:
                res.label(text=f"  {name}  {val}")
        sug = ((result.get("suggestions") or {}).get("suggestions")) or []
        for i, s in enumerate(sug[:4], 1):
            res.label(text=f"{i}. {s.get('change', '')}")
    # small settings at the bottom, collapsed visually as labels
    sett = box.column(align=True)
    sett.prop(st, "hunch_root")
    sett.prop(st, "service_python")
    FL.draw_section(layout, context)



class HUNCH_OT_use_scene(bpy.types.Operator):
    bl_idname = "hunch.use_scene"
    bl_label = "Use this scene"
    bl_description = "Apply the suggested condition and mood as a starting point you can tweak"

    def execute(self, context):
        from . import tingle_panel as TP
        return TP.use_suggested_scene(context, self)


class HUNCH_OT_dismiss_offer(bpy.types.Operator):
    bl_idname = "hunch.dismiss_offer"
    bl_label = "Not now"
    bl_description = "Hide the first-run suggestion. the pickers stay as they are."

    def execute(self, context):
        st = _st(context)
        st.offer_dismissed = True
        st.status = "suggestion dismissed. pick a mood and a condition, then run."
        return {"FINISHED"}


class HUNCH_OT_tingle_run(bpy.types.Operator):
    bl_idname = "hunch.tingle_run"
    bl_label = "Run"
    bl_description = "Set the chosen condition, sense the space, and propose edits toward the mood"

    def execute(self, context):
        from . import tingle_panel as TP
        return TP.run_simple(context, self)


class HUNCH_OT_apply_knobs(bpy.types.Operator):
    bl_idname = "hunch.apply_knobs"
    bl_label = "Apply knobs to the variant"
    bl_description = "Apply the advanced knobs to the current variant (or to the in-session preview sun). the original stays as it is."

    def execute(self, context):
        from . import tingle_panel as TP
        return TP.apply_knobs(context, self)


CLASSES = (HunchState, HUNCH_OT_scan, HUNCH_OT_place_viewpoint, HUNCH_OT_run, HUNCH_OT_write_report, HUNCH_OT_feel_report, HUNCH_OT_cancel, HUNCH_OT_scene_setup, HUNCH_OT_conditions, HUNCH_OT_use_scene, HUNCH_OT_dismiss_offer, HUNCH_OT_tingle_run, HUNCH_OT_apply_knobs) + FL.CLASSES


def register():
    ns = bpy.app.driver_namespace
    n = int(ns.get("_hunch_section_owners") or 0)
    if n == 0 and not hasattr(bpy.types, "HUNCH_OT_run"):
        for c in CLASSES:
            bpy.utils.register_class(c)
        bpy.types.Scene.hunch = PointerProperty(type=HunchState)
    ns["_hunch_section_owners"] = n + 1


def unregister():
    ns = bpy.app.driver_namespace
    n = max(0, int(ns.get("_hunch_section_owners") or 1) - 1)
    ns["_hunch_section_owners"] = n
    if n == 0 and hasattr(bpy.types, "HUNCH_OT_run"):
        if hasattr(bpy.types.Scene, "hunch"):
            del bpy.types.Scene.hunch
        for c in reversed(CLASSES):
            try:
                bpy.utils.unregister_class(c)
            except RuntimeError:
                pass
