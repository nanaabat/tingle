"""The single tingle N-panel: sense, propose, apply, verify, report.

User-facing name is tingle. Internal packages stay as they are. Lowercase copy.
Predicts how a space tends to feel; it does not read a person.

Default view is simple: mood, condition, run. An advanced collapsible exposes
the full knobs and the step-by-step flow. On first open, tingle offers one
ready-made scene from what it can detect in the file.
"""
from __future__ import annotations

import json
import math

import bpy

from . import feel_loop as FL
from . import hunch_panel as HP
from .tingle_share import MODEL_VERSIONS

_OWNERS = "_tingle_panel_owners"


def _hunch(context):
    return context.scene.hunch


def _has_wink():
    return hasattr(bpy.types, "WINK_OT_plan") or hasattr(bpy.ops, "wink")


def _has_tinker():
    return hasattr(bpy.types, "TINKER_OT_select") or hasattr(bpy.ops, "tinker")


def _text(name, default=None):
    t = bpy.data.texts.get(name)
    if t is None:
        return default
    try:
        return json.loads(t.as_string() or "null")
    except ValueError:
        return default


def _wrap(layout, text, width=18, limit=240):
    """Word-aware labels so a narrow N-panel does not break mid-word."""
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    line = ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if line and len(trial) > width:
            layout.label(text=line)
            line = word
        else:
            line = trial
    if line:
        layout.label(text=line)


def _kelvin_rgb(temp_k):
    """Rough blackbody to sRGB, physically approximate, for preview lights."""
    t = max(1000.0, min(12000.0, float(temp_k))) / 100.0
    if t <= 66:
        r = 1.0
        g = max(0.0, min(1.0, 0.390081677 * math.log(t) - 0.631841443))
    else:
        r = max(0.0, min(1.0, 1.292936186 * (t - 60) ** -0.1332047592))
        g = max(0.0, min(1.0, 1.129890897 * (t - 60) ** -0.0755148492))
    if t >= 66:
        b = 1.0
    elif t <= 19:
        b = 0.0
    else:
        b = max(0.0, min(1.0, 0.543206789 * math.log(t - 10) - 1.196254089))
    return (r, g, b)


def prepare_offer(context):
    """Detect the space once and fill the first-run offer. Cheap; safe to call from draw."""
    st = _hunch(context)
    if st.offer_ready or st.offer_dismissed or st.offer_used:
        return
    try:
        from . import scene_look as SL
        rec = SL.suggest_first_scene(context, st.scope_collection or "original")
    except Exception as exc:
        st.offer_ready = True
        st.offer_text = f"could not auto-detect the space ({exc}). pick a mood and a condition to start."
        st.offer_mood = st.target or "calm"
        st.offer_condition = st.condition or "golden_hour"
        st.offer_label = st.offer_condition
        return
    st.offer_mood = rec.get("mood") or "calm"
    st.offer_condition = rec.get("condition") or "golden_hour"
    st.offer_label = rec.get("label") or st.offer_condition
    st.offer_hint = rec.get("hint") or ""
    st.offer_text = rec.get("reason") or ""
    st.offer_rooms = ", ".join(rec.get("rooms") or [])
    st.offer_orient = rec.get("orientation") or ""
    st.offer_ready = True
    loop = _text("feel_loop_state.json") or {}
    if not st.last_feel and not (loop.get("variants") or []):
        try:
            st.target = st.offer_mood
        except Exception:
            pass
        try:
            st.condition = st.offer_condition
        except Exception:
            pass
        sun = {}
        try:
            from . import scene_look as SL
            sun = SL.SUN.get(st.offer_condition) or {}
        except Exception:
            sun = {}
        if sun:
            st.sun_altitude = float(sun.get("alt") or st.sun_altitude)
            st.sun_azimuth = float(sun.get("az") or st.sun_azimuth)


def use_suggested_scene(context, op=None):
    st = _hunch(context)
    from . import scene_look as SL
    from . import feel_loop as _FL
    if not st.offer_ready:
        prepare_offer(context)
    mood = st.offer_mood or "calm"
    cond = st.offer_condition or "golden_hour"
    try:
        st.target = mood
    except Exception:
        pass
    try:
        st.condition = cond
    except Exception:
        pass
    log = SL.ensure_base_look(context.scene, scope_objects=_FL.iter_scope(context, st.scope_collection or "original"))
    plan = SL.plan_scene_setup(mood)
    plan = dict(plan, condition=cond, label=next((c["label"] for c in SL.CONDITION_SPECS if c["id"] == cond), cond),
                when=SL.WHEN.get(cond), sun=SL.SUN.get(cond), hint=SL.condition_hint(cond))
    SL._set_text_json("tingle_scene_setup.json", {"plan": plan, "base_look": log, "from_offer": True})
    st.when = plan.get("when") or st.when
    sun = plan.get("sun") or {}
    if sun:
        st.sun_altitude = float(sun.get("alt") or st.sun_altitude)
        st.sun_azimuth = float(sun.get("az") or st.sun_azimuth)
    try:
        SL.preview_condition(context.scene, cond)
    except Exception as exc:
        st.status = f"could not preview {plan['label']}: {exc}"
        if op:
            op.report({"WARNING"}, st.status)
        return {"CANCELLED"}
    st.offer_used = True
    st.offer_dismissed = True
    st.status = f"using {plan['label']} toward {mood}."
    if op:
        op.report({"INFO"}, st.status)
    return {"FINISHED"}


def run_simple(context, op=None):
    st = _hunch(context)
    from . import scene_look as SL
    from . import feel_loop as _FL
    if st.busy:
        if op:
            op.report({"WARNING"}, "hunch is already running")
        return {"CANCELLED"}
    log = SL.ensure_base_look(context.scene, scope_objects=_FL.iter_scope(context, st.scope_collection or "original"))
    plan = SL.plan_scene_setup(st.target)
    cond = st.condition or plan.get("condition")
    plan = dict(plan, condition=cond,
                label=next((c["label"] for c in SL.CONDITION_SPECS if c["id"] == cond), cond),
                when=SL.WHEN.get(cond), sun=SL.SUN.get(cond), hint=SL.condition_hint(cond))
    SL._set_text_json("tingle_scene_setup.json", {"plan": plan, "base_look": log})
    st.when = plan.get("when") or st.when
    try:
        SL.preview_condition(context.scene, cond)
    except Exception as exc:
        st.status = f"could not preview {plan['label']}: {exc}"
        if op:
            op.report({"WARNING"}, st.status)
        # still continue to sense/propose
    st.scope_collection = st.scope_collection or "original"
    try:
        bpy.ops.hunch.scan()
        if st.viewpoint is None and context.scene.camera is not None:
            st.viewpoint = context.scene.camera
        _FL._store_snapshot(context)
        _FL.read_hunch(context, st.scope_collection or "original", blocking=True)
        _FL.make_proposal(context)
    except Exception as exc:
        st.status = f"run failed: {exc}"
        if op:
            op.report({"ERROR"}, st.status)
        return {"CANCELLED"}
    if op:
        op.report({"INFO"}, st.status)
    return {"FINISHED"}


def _variant_root(context):
    st = _hunch(context)
    name = st.scope_collection or ""
    if name and name not in ("", "original", "all"):
        col = bpy.data.collections.get(name)
        if col is not None:
            return col
    state = _text("feel_loop_state.json") or {}
    names = state.get("variants") or []
    if names:
        return bpy.data.collections.get(names[-1])
    return None


def _tokens(s):
    return " " + " ".join(str(s or "").lower().replace("_", " ").replace("-", " ").split()) + " "


def _name_matches(blob, *needles):
    hay = _tokens(blob)
    for n in needles:
        needle = _tokens(n).strip()
        if needle and f" {needle} " in hay:
            return True
    return False


def _find_named(root, *needles):
    hits = []
    for ob in FL.walk_collection(root):
        blob = (ob.name + " " + str(ob.get("feel_loop_src") or "") + " " + str(ob.get("tinker_id") or ""))
        if _name_matches(blob, *needles):
            hits.append(ob)
    return hits


def apply_knobs(context, op=None):
    st = _hunch(context)
    from . import scene_look as SL
    log = {"sun": False, "lights": 0, "materials": 0, "fittings": [], "camera": "", "note": ""}
    try:
        log["sun"] = bool(SL.apply_sun_angles(context.scene, st.sun_altitude, st.sun_azimuth))
    except Exception:
        log["sun"] = False
    root = _variant_root(context)
    if root is None:
        log["note"] = "building knobs wait for a variant. sun and sky apply on the in-session preview."
        st.status = log["note"]
        if op:
            op.report({"INFO"}, st.status)
        return {"FINISHED"}
    color = _kelvin_rgb(st.light_temp_k)
    for ob in FL.walk_collection(root):
        if ob.type != "LIGHT":
            continue
        if ob.data is None:
            continue
        if "sun" in (ob.data.type or "").lower() and "preview" not in ob.name.lower():
            continue
        try:
            ob["tinker_on"] = 1 if st.light_energy > 0.5 else 0
            ob["tinker_energy"] = float(st.light_energy)
            ob["tinker_temp_k"] = float(st.light_temp_k)
            if hasattr(ob.data, "energy"):
                ob.data.energy = float(st.light_energy)
            if hasattr(ob.data, "color"):
                ob.data.color = color
            log["lights"] += 1
        except Exception:
            continue
    # material starting point on the variant only (skip glazing unless transmission is set)
    from . import hunch_scene as HS
    for ob in FL.walk_collection(root):
        if ob.type != "MESH" or not ob.data:
            continue
        if HS.is_helper_mesh(ob):
            continue
        is_glass = HS.is_glazing(ob)
        mats = list(ob.data.materials) if ob.data.materials else []
        for mat in mats:
            if mat is None or not mat.use_nodes:
                continue
            for node in mat.node_tree.nodes:
                if node.type != "BSDF_PRINCIPLED":
                    continue
                if is_glass:
                    if "Transmission" in node.inputs:
                        node.inputs["Transmission"].default_value = float(st.mat_transmission)
                    elif "Transmission Weight" in node.inputs:
                        node.inputs["Transmission Weight"].default_value = float(st.mat_transmission)
                else:
                    if "Roughness" in node.inputs:
                        node.inputs["Roughness"].default_value = float(st.mat_roughness)
                    if "Base Color" in node.inputs:
                        col = tuple(st.mat_colour) + (1.0,)
                        node.inputs["Base Color"].default_value = col[:4]
                log["materials"] += 1
    # fittings
    def pose(needles, value, kind):
        found = _find_named(root, *needles)
        ok = 0
        for ob in found:
            if "rod" in ob.name.lower():
                continue
            if FL._set_joint_value(ob, value, root=root):
                ok += 1
        if ok:
            log["fittings"].append(f"{kind}:{ok}")
        return ok

    # louvers: 0 open, 1 closed maps onto the joint upper
    for ob in FL.walk_collection(root):
        blob = (ob.name + " " + str(ob.get("tinker_type") or "")).lower()
        if "louver" in blob or ob.get("tinker_type") == "blind":
            jd = dict(ob.get("tinker_joint") or {})
            upper = float(jd.get("upper") or 1.4486)
            if FL._set_joint_value(ob, float(st.knob_louver) * upper, root=root):
                if "louver" not in ",".join(log["fittings"]):
                    log["fittings"].append("louver")
    pose(("door",), float(st.knob_door), "door")
    pose(("arch screen", "arch-screen"), float(st.knob_arch), "arch")
    for ob in _find_named(root, "fan loft", "fan-loft", "fan"):
        if "rod" in ob.name.lower():
            continue
        ob["tinker_on"] = 1 if st.knob_fan_on else 0
        ob["tinker_speed"] = float(st.knob_fan_speed)
        log["fittings"].append("fan")
        break
    couch = next((o for o in _find_named(root, "couch") if o.get("tinker_joint") or o.type == "EMPTY"), None)
    if couch is not None:
        if FL._set_joint_value(couch, float(st.knob_couch), root=root):
            log["fittings"].append("couch")
    # camera path
    key = (st.camera_path or "").lower()
    if key and key not in ("still",):
        cam = None
        for ob in list(context.scene.objects) + list(bpy.data.objects):
            if ob.type == "CAMERA" and key in ob.name.lower().replace("_", " ").replace("-", " "):
                cam = ob
                break
        if cam is not None:
            context.scene.camera = cam
            st.viewpoint = cam
            log["camera"] = cam.name
    st.status = (
        f"applied knobs on {root.name}: "
        f"{log['materials']} material(s), {log['lights']} light(s)"
        + (f", {', '.join(log['fittings'])}" if log["fittings"] else "")
        + (f", camera {log['camera']}" if log["camera"] else "")
        + ("; preview sun updated" if log["sun"] else "")
        + ". the original is unchanged."
    )
    if op:
        op.report({"INFO"}, st.status)
    return {"FINISHED"}


class TINGLE_PT_panel(bpy.types.Panel):
    bl_label = "tingle"
    bl_idname = "TINGLE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "tingle"

    def draw(self, context):
        draw_tingle(self.layout, context)


_OFFER_PENDING = {"on": False}


def _offer_timer():
    _OFFER_PENDING["on"] = False
    try:
        prepare_offer(bpy.context)
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass
    return None


def schedule_offer(context):
    """Panel draw may not write to ID data; defer the first-run detection to a timer."""
    st = _hunch(context)
    if st.offer_ready or st.offer_dismissed or st.offer_used or _OFFER_PENDING["on"]:
        return
    _OFFER_PENDING["on"] = True
    try:
        bpy.app.timers.register(_offer_timer, first_interval=0.05)
    except Exception:
        _OFFER_PENDING["on"] = False


def draw_tingle(layout, context):
    st = _hunch(context)
    schedule_offer(context)
    layout.label(text="tingle")
    layout.label(text="  ·  ".join(f"{k} {v}" for k, v in MODEL_VERSIONS.items()))

    show_offer = st.offer_ready and not st.offer_dismissed and not st.offer_used
    if show_offer:
        box = layout.box()
        box.label(text="a starting scene")
        if st.offer_rooms:
            box.label(text=f"rooms: {st.offer_rooms}")
        if st.offer_orient:
            _wrap(box, st.offer_orient, 18, 80)
        mood = st.offer_mood or "calm"
        label = st.offer_label or st.offer_condition or "golden hour"
        box.label(text=f"suggested: {label} toward {mood}")
        if st.offer_hint:
            _wrap(box, "what this teaches: " + st.offer_hint, 18, 80)
        why = ""
        text = st.offer_text or ""
        if "often already reads as open" in text:
            why = "a tall, very glazed space often already reads as open."
        elif text:
            bits = [s.strip() for s in text.split(".") if s.strip()]
            why = (bits[-1] + ".") if bits else ""
        if why:
            _wrap(box, why, 18, 80)
        row = box.row(align=True)
        row.operator("hunch.use_scene", text="use this scene", icon="PLAY")
        row.operator("hunch.dismiss_offer", text="not now", icon="X")

    status = layout.box()
    head = status.row()
    head.label(text="", icon="INFO")
    _wrap(status, st.status or "pick a mood and a condition, then run", 18, 200)
    if st.busy:
        status.progress(factor=st.progress, text="working...")
    if st.share_note:
        thanks = status.column()
        _wrap(thanks, st.share_note, 18, 200)

    # --- simple view
    box = layout.box()
    box.label(text="start")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.prop(st, "target", text="mood")
    col.prop(st, "condition")
    try:
        from . import scene_look as SL
        hint = SL.condition_hint(st.condition)
    except Exception:
        hint = ""
    if hint:
        _wrap(col, "what this teaches: " + hint, 18, 120)
    col.operator("hunch.tingle_run", text="run", icon="PLAY")
    col.prop(st, "share_run")
    if getattr(st, "share_run", False):
        col.prop(st, "share_url")
    if st.last_feel:
        col.label(text=f"predicted to read as {st.last_feel}")
    proposal = _text("feel_loop_proposal.json") or {}
    if st.loop_waiting and proposal.get("edits"):
        col.label(text=f"{len(proposal['edits'])} edit(s) toward {proposal.get('target')}")
        row = col.row(align=True)
        row.operator("hunch.feel_approve", text="approve and build", icon="CHECKMARK")
        row.operator("hunch.feel_reject", text="reject", icon="X")

    layout.prop(st, "show_advanced", text="advanced", toggle=True)
    if not st.show_advanced:
        return

    # --- advanced: full knobs, then the existing step flow
    box = layout.box()
    box.label(text="scene knobs")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.prop(st, "sun_altitude")
    col.prop(st, "sun_azimuth")
    col.prop(st, "light_temp_k")
    col.prop(st, "light_energy")
    col.prop(st, "mat_roughness")
    col.prop(st, "mat_colour")
    col.prop(st, "mat_transmission")
    col.prop(st, "camera_path")
    col.separator()
    col.label(text="movable parts")
    col.prop(st, "knob_louver")
    col.prop(st, "knob_door")
    col.prop(st, "knob_arch")
    col.prop(st, "knob_fan_on")
    col.prop(st, "knob_fan_speed")
    col.prop(st, "knob_couch")
    col.operator("hunch.apply_knobs", text="apply knobs to the variant", icon="CHECKMARK")
    col.label(text="mood-to-material result is an editable starting point")

    # --- 1. sense
    box = layout.box()
    box.label(text="1  sense")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.prop(st, "site")
    if st.site == "custom":
        col.prop(st, "site_custom")
    col.prop(st, "room")
    col.prop(st, "viewpoint")
    col.prop(st, "orient_deg")
    row = col.row(align=True)
    row.operator("hunch.scan", text="find rooms", icon="VIEWZOOM")
    row.operator("hunch.place_viewpoint", text="viewpoint", icon="EMPTY_AXIS")
    run = col.row(align=True)
    run.operator("hunch.run", text="sense", icon="PLAY")
    if st.busy:
        run.operator("hunch.cancel", text="", icon="X")
    if st.last_feel:
        box.label(text=f"predicted to read as {st.last_feel}")
        _wrap(box, st.last_breakdown or "", 18, 180)
        result = _text("hunch_last_result.json") or {}
        atm = ((result.get("focus") or {}).get("atmosphere")) or {}
        scores = atm.get("scores") or {}
        for name, val in list(scores.items())[:7]:
            box.label(text=f"  {name}  {val}")

    # --- 2. set the scene
    box = layout.box()
    box.label(text="2  set the scene")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.operator("hunch.scene_setup", text="set the scene", icon="LIGHT_SUN")
    setup = _text("tingle_scene_setup.json") or {}
    plan = setup.get("plan") or {}
    if plan:
        col.label(text=f"{plan.get('target')}: {plan.get('label')}")
        _wrap(col, plan.get("reason") or "", 18, 160)

    # --- 3. conditions
    box = layout.box()
    box.label(text="3  conditions")
    col = box.column(align=True)
    col.enabled = not st.busy
    col.operator("hunch.conditions", text="generate conditions", icon="WORLD")
    try:
        from . import scene_look as SL
        specs = SL.CONDITION_SPECS
    except Exception:
        specs = []
    for spec in specs:
        col.label(text=spec.get("label") or spec.get("id"))
        hint = spec.get("hint") or spec.get("blurb") or ""
        if hint:
            _wrap(col, "what this teaches: " + hint, 18, 120)
    cond = _text("tingle_conditions.json") or {}
    reads = cond.get("conditions") or []
    if reads:
        col.label(text=f"{len(reads)} condition scene(s)")
        for rec in reads[:7]:
            feel = rec.get("feel") or "-"
            secs = rec.get("render_seconds")
            extra = f"  {secs:.1f}s" if isinstance(secs, (int, float)) else ""
            col.label(text=f"  {rec.get('label')}: {feel}{extra}")
        pick = (cond.get("pick") or {}).get("picked") or []
        if pick:
            col.label(text="wink pick: " + ", ".join(p.get("label") or p.get("id") for p in pick))

    # --- 4. propose
    box = layout.box()
    box.label(text="4  propose")
    col = box.column(align=True)
    col.enabled = not st.busy
    if _has_wink() and hasattr(context.scene, "wink"):
        wst = context.scene.wink
        col.prop(wst, "time_of_day")
        col.prop(wst, "rule_text")
        col.operator("wink.plan", text="propose a move", icon="LIGHT")
        plan = _text("wink_last_plan.json") or {}
        if plan.get("explanation"):
            for line in plan["explanation"][:6]:
                col.label(text=str(line)[:60])
            if plan.get("state"):
                col.label(text=f"plan: {plan.get('state')}")
    col.operator("hunch.feel_propose", text="propose edits", icon="MOD_BUILD")
    proposal = _text("feel_loop_proposal.json") or {}
    if st.loop_waiting and proposal.get("edits"):
        col.label(text=f"{len(proposal['edits'])} edit(s) toward {proposal.get('target')}")
        for ed in proposal["edits"][:5]:
            col.label(text=f"  - {ed.get('detail') or ed.get('kind')}")

    # --- 5. apply
    box = layout.box()
    box.label(text="5  apply")
    box.label(text="builds a full copy beside the original")
    col = box.column(align=True)
    col.enabled = not st.busy
    row = col.row(align=True)
    row.operator("hunch.feel_approve", text="approve and build", icon="CHECKMARK")
    row.operator("hunch.feel_reject", text="reject", icon="X")
    col.prop(st, "loop_max_iter")
    col.operator("hunch.feel_auto", text="auto until target", icon="PLAY")
    if _has_wink() and hasattr(context.scene, "wink"):
        plan = _text("wink_last_plan.json") or {}
        if plan.get("state") == "awaiting_approval":
            row = col.row(align=True)
            row.operator("wink.approve", text="approve move", icon="CHECKMARK")
            row.operator("wink.reject", text="reject move", icon="X")

    # --- 6. verify
    box = layout.box()
    box.label(text="6  verify")
    col = box.column(align=True)
    state = _text("feel_loop_state.json") or {}
    if state.get("variants"):
        col.label(text="variants: " + ", ".join(state["variants"]))
        col.label(text=f"{state.get('start_feel')} to {state.get('end_feel')}  (target {state.get('target')})")
        col.label(text="target reached" if state.get("reached") else "target not yet reached")
    else:
        col.label(text="no variant yet")
    row = col.row(align=True)
    row.operator("hunch.feel_render", text="before / after", icon="RENDER_STILL")
    row.operator("hunch.feel_export", text="export", icon="EXPORT")

    # --- 7. report
    box = layout.box()
    box.label(text="7  report")
    col = box.column(align=True)
    col.prop(st, "loop_out")
    col.prop(st, "user_feedback")
    col.operator("hunch.feel_report", text="one-page feel report", icon="DOCUMENTS")
    col.operator("hunch.write_report", text="write score image", icon="FILE_IMAGE")
    if st.report_path:
        col.label(text=st.report_path)

    # teach a moving part (kept, but not the default path)
    if _has_tinker() and hasattr(context.scene, "tinker"):
        tst = context.scene.tinker
        adv = layout.box()
        adv.label(text="teach a moving part")
        col = adv.column(align=True)
        col.enabled = not getattr(tst, "busy", False)
        col.operator("tinker.select", text="select part")
        row = col.row(align=True)
        row.operator("tinker.record", text="record")
        row.operator("tinker.stop", text="stop")
        col.operator("tinker.learn", text="learn")
        if tst.skill_name:
            col.label(text=f"skill {tst.skill_name}")
        col.prop(tst, "goal")
        row = col.row(align=True)
        row.operator("tinker.simulate", text="simulate")
        row.operator("tinker.preview", text="preview")
        col.operator("tinker.apply", text="apply on this model")
        col.operator("tinker.undo", text="undo")


def register():
    ns = bpy.app.driver_namespace
    n = int(ns.get(_OWNERS) or 0)
    if n == 0 and not hasattr(bpy.types, "TINGLE_PT_panel"):
        bpy.utils.register_class(TINGLE_PT_panel)
    ns[_OWNERS] = n + 1


def unregister():
    ns = bpy.app.driver_namespace
    n = max(0, int(ns.get(_OWNERS) or 1) - 1)
    ns[_OWNERS] = n
    if n == 0 and hasattr(bpy.types, "TINGLE_PT_panel"):
        try:
            bpy.utils.unregister_class(TINGLE_PT_panel)
        except RuntimeError:
            pass
