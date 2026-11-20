"""Local run log and optional anonymised POST of a tingle run.

Sharing is off by default. A run is posted only when the person turns
share this run on and has set a share URL (in the panel, or the
TINGLE_SHARE_URL environment variable). No network posting happens unless
that URL is set. Every run is also appended to a local JSONL log under the
add-on data directory, whether or not it is shared. No names, emails, paths
or photos are sent.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

USER_AGENT = "tingle/1.0"
MODEL_VERSIONS = {"hunch": "v1", "tinker": "v1", "wink": "v1"}
THANKS = "thanks. this run was shared."


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def env_is_test() -> bool:
    """Automated runs (tests, captures) can set TINGLE_IS_TEST=1 so a receiver can drop them from stats."""
    return _env_on("TINGLE_IS_TEST") or _env_on("TINGLE_NO_POST") or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def env_no_post() -> bool:
    """TINGLE_NO_POST=1 skips the network call entirely (local log only)."""
    return _env_on("TINGLE_NO_POST")


def _looks_like_url(value: str) -> bool:
    v = (value or "").strip()
    return v.startswith("https://") or v.startswith("http://")


def share_url(st=None) -> str:
    """Return the configured share URL, or empty if sharing has no destination."""
    for c in (
        os.environ.get("TINGLE_SHARE_URL", ""),
        getattr(st, "share_url", "") or "",
    ):
        if _looks_like_url(str(c)):
            return str(c).strip()
    return ""


def data_dir() -> Path:
    try:
        import bpy
        root = bpy.utils.user_resource("DATAFILES", path="tingle", create=True)
        if root:
            p = Path(root)
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    p = Path.home() / ".tingle"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _clean_scores(scores):
    out = {}
    for k, v in (scores or {}).items():
        try:
            out[str(k)[:40]] = round(float(v), 4)
        except (TypeError, ValueError):
            continue
    return out


def _clean_features(feat):
    out = {}
    for k, v in (feat or {}).items():
        if k in ("name",):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(k)[:40]] = round(float(v), 4)
    return out


def _clean_changes(items):
    out = []
    for ed in items or []:
        if not isinstance(ed, dict):
            continue
        rec = {"kind": str(ed.get("kind") or "edit")[:40]}
        if ed.get("detail"):
            rec["detail"] = str(ed.get("detail"))[:200]
        for k in ("radians", "speed", "energy", "temp_k", "area_scale", "applied_to", "couch", "table"):
            if isinstance(ed.get(k), (int, float)) and not isinstance(ed.get(k), bool):
                rec[k] = ed[k]
        out.append(rec)
        if len(json.dumps(out)) > 14000:
            break
    return out


def build_payload(st, loop, *, feedback=None, is_test=False) -> dict:
    before = loop.get("scores_start") or {}
    start_feel = loop.get("start_feel")
    end_it = (loop.get("iterations") or [{}])[-1] if loop.get("iterations") else {}
    after = end_it.get("scores") or loop.get("scores_end") or {}
    end_feel = loop.get("end_feel") or end_it.get("feel")
    feats = _clean_features(end_it.get("features_after") or end_it.get("features_before") or {})
    cond = loop.get("conditions") or {}
    cond_pick = (cond.get("pick") or {}).get("picked") or []
    site = ""
    if st is not None:
        chosen = getattr(st, "site", "") or ""
        if chosen == "custom":
            chosen = getattr(st, "site_custom", "") or ""
        site = str(chosen)
    conditions = {
        "target_condition": getattr(st, "condition", "") or "",
        "picked": [p.get("id") for p in cond_pick if isinstance(p, dict)][:7],
    }
    if site:
        conditions["site"] = site[:80]
    # optional lat/lon from the last hunch result if present, no paths
    loc = None
    try:
        import bpy
        t = bpy.data.texts.get("hunch_last_result.json")
        if t:
            res = json.loads(t.as_string() or "{}")
            loc = res.get("site") or {}
    except Exception:
        loc = {}
    if isinstance(loc, dict):
        if loc.get("lat") is not None:
            conditions["lat"] = loc.get("lat")
        if loc.get("lon") is not None:
            conditions["lon"] = loc.get("lon")
    proposed = []
    for it in loop.get("iterations") or []:
        for ed in (it.get("proposal") or {}).get("edits") or it.get("applied") or []:
            proposed.append(ed)
    kept = []
    for it in loop.get("iterations") or []:
        kept.extend(it.get("applied") or [])
    body = {
        "share": True,
        "run_id": uuid.uuid4().hex,
        "model": "hunch",
        "model_version": MODEL_VERSIONS["hunch"],
        "source": "blender",
        "target_feel": str(loop.get("target") or getattr(st, "target", "") or "")[:40],
        "predicted_before": {"label": start_feel, "scores": _clean_scores(before)},
        "predicted_after": {"label": end_feel, "scores": _clean_scores(after)},
        "conditions": conditions,
        "changes_proposed": _clean_changes(proposed or kept),
        "changes_kept": _clean_changes(kept),
        "geometry_features": feats,
    }
    if feedback in ("agree", "disagree"):
        body["user_feedback"] = {"agree": feedback == "agree", "label": str(end_feel or "")[:40]}
    elif isinstance(feedback, dict):
        body["user_feedback"] = feedback
    if is_test:
        body["is_test"] = True
    raw = json.dumps(body, separators=(",", ":"))
    if len(raw) > 64000:
        body.pop("changes_proposed", None)
        body["changes_kept"] = body.get("changes_kept", [])[:8]
    return body


def append_log(payload: dict, *, shared: bool, post_status=None, post_error=None) -> Path:
    rec = dict(payload)
    rec["shared"] = bool(shared)
    if post_status is not None:
        rec["post_status"] = post_status
    if post_error:
        rec["post_error"] = str(post_error)[:200]
    path = data_dir() / "runs.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return path


def post_run(payload: dict, url: str, timeout=12) -> tuple[int, dict | str]:
    import urllib.request
    import urllib.error
    if not _looks_like_url(url):
        return 0, "no share URL configured"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"raw": body[:200]}
            return int(resp.status), parsed
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:300]
        return int(e.code), err
    except Exception as e:
        return 0, str(e)[:300]


def finish_run(context, loop, *, is_test=False) -> dict:
    """Always log locally. POST only if share_run is on and a share URL is set."""
    st = context.scene.hunch
    feedback = getattr(st, "user_feedback", "none")
    if feedback == "none":
        feedback = None
    is_test = bool(is_test or getattr(st, "share_is_test", False) or env_is_test())
    payload = build_payload(st, loop or {}, feedback=feedback, is_test=is_test)
    local = dict(payload)
    want_share = bool(getattr(st, "share_run", False))
    dest = share_url(st)
    local["share"] = want_share and bool(dest)
    result = {"logged": False, "posted": False, "status": 0, "note": ""}
    if want_share and env_no_post():
        append_log(local, shared=False, post_error="skipped: TINGLE_NO_POST")
        result.update(logged=True, note="share skipped (TINGLE_NO_POST)")
        st.share_note = ""
    elif want_share and not dest:
        append_log(local, shared=False, post_error="skipped: no share URL")
        result.update(logged=True, note="share is on but no share URL is set")
        st.share_note = "share is on but no share URL is set"
    elif want_share:
        status, body = post_run(payload, dest)
        result["posted"] = status == 201
        result["status"] = status
        result["body"] = body
        append_log(local, shared=True, post_status=status, post_error=None if status == 201 else body)
        result["logged"] = True
        if status == 201:
            result["note"] = THANKS
            st.share_note = THANKS
        else:
            st.share_note = ""
            result["note"] = ""
    else:
        append_log(local, shared=False)
        result["logged"] = True
        st.share_note = ""
    return result
