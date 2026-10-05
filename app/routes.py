"""HTTP 路由：数据 CRUD、关联计算、人工定案、导出。"""
import json
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory, abort

from . import db as dbmod
from .correlator import correlate, prepare_layers, build_choice
from .domain import LITHOLOGIES, GRAINS, WATERS
from .seed import seed_if_empty

app = Flask(__name__, static_folder="static", static_url_path="/static")


def _attach_decisions(conn, result):
    """把各孔人工定案挂到结果上，并给出 effective 展示视图。

    定案按 run_id 精确从属于本次计算：旧 run 的定案查不到，自然不显示，
    也不可能被过期页面写回（提交端另校验 run_id，见 api_decisions_submit）。
    算法字段（pairs/unmatched_*/dp_score/solution_count/ambiguities 等）
    原样保留，不因定案改写。
    """
    if not result:
        return result
    rows = conn.execute(
        "SELECT hole_id, solution_index, payload, decided_at FROM decision"
        " WHERE run_id=?", (result.get("run_id"),)
    ).fetchall()
    by_hole = {r["hole_id"]: r for r in rows}
    for h in result.get("holes", []):
        row = by_hole.get(h["hole_id"])
        if row is None:
            h["decision"] = None
            h["effective"] = {
                "source": "auto",
                "pairs": h["pairs"],
                "unmatched_ref": h["unmatched_ref"],
                "unmatched_hole": h["unmatched_hole"],
            }
        else:
            view = json.loads(row["payload"])
            h["decision"] = {
                "solution_index": row["solution_index"],
                "decided_at": row["decided_at"],
                "pairs": view["pairs"],
                "unmatched_ref": view["unmatched_ref"],
                "unmatched_hole": view["unmatched_hole"],
                "total_score": view["total_score"],
            }
            h["effective"] = {
                "source": "manual",
                "pairs": view["pairs"],
                "unmatched_ref": view["unmatched_ref"],
                "unmatched_hole": view["unmatched_hole"],
            }
    return result


def _load_result(conn=None):
    """最近一次关联结果（持久化，多 worker 共享），并挂上当前定案。"""
    own = conn is None
    if own:
        conn = dbmod.get_db()
    try:
        row = conn.execute(
            "SELECT payload, run_id FROM run_result WHERE id=1"
        ).fetchone()
        result = json.loads(row["payload"]) if row and row["payload"] else None
        if result is not None and row["run_id"]:
            result["run_id"] = row["run_id"]
        return _attach_decisions(conn, result)
    finally:
        if own:
            conn.close()


def _save_result(conn, result, run_id):
    """保存新一次计算：同时作废全部旧定案（定案从属于 run_id）。"""
    conn.execute("DELETE FROM decision")
    conn.execute(
        "INSERT INTO run_result (id, payload, generated_at, run_id) VALUES (1,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
        " generated_at=excluded.generated_at, run_id=excluded.run_id",
        (json.dumps(result, ensure_ascii=False),
         result.get("generated_at"), run_id),
    )


def _invalidate_result(conn=None):
    """层段 / 标志层 / 参数变化或演示重置：结果与全部定案一并失效。"""
    own = conn is None
    if own:
        conn = dbmod.get_db()
    try:
        conn.execute("UPDATE run_result SET payload=NULL, run_id=NULL WHERE id=1")
        conn.execute("DELETE FROM decision")
        conn.commit()
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _borehole(conn, row):
    layers = conn.execute(
        "SELECT * FROM layer WHERE borehole_id=? ORDER BY seq", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "x": row["x"],
        "collar_elev": row["collar_elev"],
        "note": row["note"],
        "layers": [dict(l) for l in layers],
    }


def _all_boreholes(conn):
    return [_borehole(conn, r) for r in
            conn.execute("SELECT * FROM borehole ORDER BY x, id").fetchall()]


def _all_markers(conn):
    markers = conn.execute("SELECT * FROM marker ORDER BY id").fetchall()
    out = []
    for m in markers:
        links_rows = conn.execute(
            "SELECT * FROM marker_layer WHERE marker_id=?", (m["id"],)
        ).fetchall()
        out.append({
            "id": m["id"],
            "label": m["label"],
            "description": m["description"],
            "links": {str(l["borehole_id"]): l["layer_id"] for l in links_rows},
        })
    return out


def _settings(conn):
    row = conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()
    return json.loads(row["payload"])


def bad(msg, code=400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------------------
# 页面 / 配置 / 全量状态
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def api_config():
    return jsonify({
        "lithologies": list(LITHOLOGIES.keys()),
        "lithology_families": LITHOLOGIES,
        "grains": GRAINS,
        "waters": WATERS,
        "defaults": dbmod.DEFAULT_SETTINGS,
    })


@app.get("/api/state")
def api_state():
    conn = dbmod.get_db()
    try:
        return jsonify({
            "boreholes": _all_boreholes(conn),
            "markers": _all_markers(conn),
            "settings": _settings(conn),
            "result": _load_result(conn),
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 钻孔与层段
# ---------------------------------------------------------------------------

def _validate_layer(ly, idx):
    for f in ("top", "bottom"):
        if not isinstance(ly.get(f), (int, float)):
            raise ValueError(f"第 {idx} 层：{f} 必须是数字")
    if ly["top"] < 0 or ly["bottom"] <= ly["top"]:
        raise ValueError(f"第 {idx} 层：深度须满足 0 ≤ 顶深 < 底深")
    if ly.get("lithology", "未知") not in LITHOLOGIES:
        raise ValueError(f"第 {idx} 层：未知岩性「{ly.get('lithology')}」")
    if ly.get("grain", "未知") not in GRAINS:
        raise ValueError(f"第 {idx} 层：粒径取值非法")
    if ly.get("water", "未知") not in WATERS:
        raise ValueError(f"第 {idx} 层：含水状态取值非法")
    conf = ly.get("confidence", 1.0)
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ValueError(f"第 {idx} 层：置信度须在 0~1 之间")


def _save_layers(conn, hole_id, layers):
    ordered = sorted(enumerate(layers, 1),
                     key=lambda t: (t[1].get("top", 0), t[0]))
    prev_bottom = None
    for idx, ly in ordered:
        _validate_layer(ly, idx)
        if prev_bottom is not None and ly["top"] < prev_bottom - 1e-9:
            raise ValueError(f"第 {idx} 层与上一层存在深度重叠")
        prev_bottom = ly["bottom"]
    # 记录旧标志层链接（按层序号），替换后尽量保留
    old_links = conn.execute(
        "SELECT ml.marker_id, l.seq FROM marker_layer ml"
        " JOIN layer l ON l.id = ml.layer_id WHERE ml.borehole_id=?",
        (hole_id,),
    ).fetchall()
    conn.execute("DELETE FROM layer WHERE borehole_id=?", (hole_id,))
    seq_to_id = {}
    for seq, (_orig, ly) in enumerate(ordered, 1):
        cur = conn.execute(
            "INSERT INTO layer (borehole_id, seq, top, bottom, lithology,"
            " grain, water, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (hole_id, seq, ly["top"], ly["bottom"],
             ly.get("lithology", "未知"), ly.get("grain", "未知"),
             ly.get("water", "未知"), ly.get("confidence", 1.0)),
        )
        seq_to_id[seq] = cur.lastrowid
    for marker_id, seq in old_links:
        if seq in seq_to_id:
            conn.execute(
                "INSERT OR IGNORE INTO marker_layer (marker_id, borehole_id,"
                " layer_id) VALUES (?,?,?)",
                (marker_id, hole_id, seq_to_id[seq]),
            )


@app.post("/api/boreholes")
def api_borehole_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad("孔名不能为空")
    try:
        x = float(data.get("x", 0))
        elev = float(data["collar_elev"])
    except (TypeError, ValueError):
        return bad("水平位置 / 孔口高程必须是数字")
    conn = dbmod.get_db()
    try:
        if conn.execute("SELECT 1 FROM borehole WHERE name=?", (name,)).fetchone():
            return bad(f"孔名「{name}」已存在")
        cur = conn.execute(
            "INSERT INTO borehole (name, x, collar_elev, note) VALUES (?,?,?,?)",
            (name, x, elev, data.get("note", "")),
        )
        hole_id = cur.lastrowid
        try:
            _save_layers(conn, hole_id, data.get("layers", []))
        except ValueError as e:
            conn.rollback()
            return bad(str(e))
        conn.commit()
        _invalidate_result()
        row = conn.execute("SELECT * FROM borehole WHERE id=?", (hole_id,)).fetchone()
        return jsonify(_borehole(conn, row)), 201
    finally:
        conn.close()


@app.put("/api/boreholes/<int:hole_id>")
def api_borehole_update(hole_id):
    data = request.get_json(force=True, silent=True) or {}
    conn = dbmod.get_db()
    try:
        row = conn.execute("SELECT * FROM borehole WHERE id=?", (hole_id,)).fetchone()
        if row is None:
            return bad("钻孔不存在", 404)
        name = (data.get("name") or row["name"]).strip()
        dup = conn.execute(
            "SELECT 1 FROM borehole WHERE name=? AND id<>?", (name, hole_id)
        ).fetchone()
        if dup:
            return bad(f"孔名「{name}」已存在")
        try:
            x = float(data.get("x", row["x"]))
            elev = float(data.get("collar_elev", row["collar_elev"]))
        except (TypeError, ValueError):
            return bad("水平位置 / 孔口高程必须是数字")
        conn.execute(
            "UPDATE borehole SET name=?, x=?, collar_elev=?, note=? WHERE id=?",
            (name, x, elev, data.get("note", row["note"]), hole_id),
        )
        if "layers" in data:
            try:
                _save_layers(conn, hole_id, data["layers"])
            except ValueError as e:
                conn.rollback()
                return bad(str(e))
        conn.commit()
        _invalidate_result()
        row = conn.execute("SELECT * FROM borehole WHERE id=?", (hole_id,)).fetchone()
        return jsonify(_borehole(conn, row))
    finally:
        conn.close()


@app.delete("/api/boreholes/<int:hole_id>")
def api_borehole_delete(hole_id):
    conn = dbmod.get_db()
    try:
        conn.execute("DELETE FROM borehole WHERE id=?", (hole_id,))
        conn.commit()
        _invalidate_result()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 标志层
# ---------------------------------------------------------------------------

@app.post("/api/markers")
def api_marker_create():
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return bad("标志层标签不能为空")
    conn = dbmod.get_db()
    try:
        if conn.execute("SELECT 1 FROM marker WHERE label=?", (label,)).fetchone():
            return bad(f"标志层「{label}」已存在")
        cur = conn.execute(
            "INSERT INTO marker (label, description) VALUES (?,?)",
            (label, data.get("description", "")),
        )
        mid = cur.lastrowid
        _save_marker_links(conn, mid, data.get("links", {}))
        conn.commit()
        _invalidate_result()
        return jsonify({"id": mid}), 201
    finally:
        conn.close()


def _save_marker_links(conn, marker_id, links):
    """links: {borehole_id(str): layer_id}"""
    conn.execute("DELETE FROM marker_layer WHERE marker_id=?", (marker_id,))
    for hid, lid in links.items():
        row = conn.execute(
            "SELECT 1 FROM layer WHERE id=? AND borehole_id=?", (lid, hid)
        ).fetchone()
        if row is None:
            raise ValueError(f"标志层链接了不存在的层（layer {lid}）")
        conn.execute(
            "INSERT INTO marker_layer (marker_id, borehole_id, layer_id)"
            " VALUES (?,?,?)",
            (marker_id, hid, lid),
        )


@app.put("/api/markers/<int:marker_id>")
def api_marker_update(marker_id):
    data = request.get_json(force=True, silent=True) or {}
    conn = dbmod.get_db()
    try:
        if not conn.execute("SELECT 1 FROM marker WHERE id=?", (marker_id,)).fetchone():
            return bad("标志层不存在", 404)
        label = (data.get("label") or "").strip()
        if not label:
            return bad("标志层标签不能为空")
        dup = conn.execute(
            "SELECT 1 FROM marker WHERE label=? AND id<>?", (label, marker_id)
        ).fetchone()
        if dup:
            return bad(f"标志层「{label}」已存在")
        conn.execute(
            "UPDATE marker SET label=?, description=? WHERE id=?",
            (label, data.get("description", ""), marker_id),
        )
        if "links" in data:
            try:
                _save_marker_links(conn, marker_id, data["links"])
            except ValueError as e:
                conn.rollback()
                return bad(str(e))
        conn.commit()
        _invalidate_result()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.delete("/api/markers/<int:marker_id>")
def api_marker_delete(marker_id):
    conn = dbmod.get_db()
    try:
        conn.execute("DELETE FROM marker WHERE id=?", (marker_id,))
        conn.commit()
        _invalidate_result()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 权重 / 惩罚参数
# ---------------------------------------------------------------------------

@app.put("/api/settings")
def api_settings_update():
    data = request.get_json(force=True, silent=True) or {}
    conn = dbmod.get_db()
    try:
        cur = _settings(conn)
        if "weights" in data:
            w = data["weights"]
            for k in ("lithology", "grain", "water", "elev", "confidence"):
                if k in w:
                    try:
                        v = float(w[k])
                    except (TypeError, ValueError):
                        return bad(f"权重 {k} 必须是数字")
                    if v < 0:
                        return bad(f"权重 {k} 不能为负")
                    cur["weights"][k] = v
        for k in ("elev_scale", "gap_open", "gap_extend"):
            if k in data:
                try:
                    v = float(data[k])
                except (TypeError, ValueError):
                    return bad(f"{k} 必须是数字")
                if v < 0:
                    return bad(f"{k} 不能为负")
                cur[k] = v
        conn.execute("UPDATE settings SET payload=? WHERE id=1",
                     (json.dumps(cur, ensure_ascii=False),))
        conn.commit()
        # 参数变化后旧结果与其人工定案均失效（下一次重算生成新 run_id）
        _invalidate_result()
        return jsonify(cur)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 关联计算
# ---------------------------------------------------------------------------

def _layer_map(conn):
    out = {}
    for r in conn.execute("SELECT * FROM layer").fetchall():
        out[r["id"]] = dict(r)
    return out


@app.post("/api/correlate")
def api_correlate():
    data = request.get_json(force=True, silent=True) or {}
    ref_id = data.get("reference_id")
    locked_ids = data.get("marker_ids", [])
    if ref_id is None:
        return bad("请选择参考孔")
    conn = dbmod.get_db()
    try:
        holes = {r["id"]: _borehole(conn, r) for r in
                 conn.execute("SELECT * FROM borehole").fetchall()}
        if ref_id not in holes:
            return bad("参考孔不存在", 404)
        if len(holes) < 2:
            return bad("至少需要两口钻孔才能关联")
        settings = _settings(conn)

        # 标志层 -> {hole_id: layer_id}
        markers = _all_markers(conn)
        marker_by_id = {m["id"]: m for m in markers}
        layer_by_id = _layer_map(conn)

        ref = holes[ref_id]
        ref_layers = prepare_layers(ref["layers"], ref["collar_elev"])
        ref_order = {ly["id"]: i for i, ly in enumerate(ref_layers)}

        # 参考孔参与计算的锁定标志层（按参考孔层序排序）
        ref_markers = []
        missing_in_ref = []
        for mid in locked_ids:
            mk = marker_by_id.get(int(mid))
            if mk is None:
                continue
            lid = mk["links"].get(str(ref_id))
            if lid and int(lid) in ref_order:
                ref_markers.append((ref_order[int(lid)], int(lid), mk))
            else:
                missing_in_ref.append(mk["label"])
        ref_markers.sort(key=lambda t: t[0])
        ref_seqs_used = [t[0] for t in ref_markers]
        if len(set(ref_seqs_used)) != len(ref_seqs_used):
            return bad("存在两个锁定标志层被指定到参考孔的同一层，请先在标志层管理中修正")

        hole_results, warnings = [], []
        if missing_in_ref:
            warnings.append(f"标志层 {'、'.join(missing_in_ref)} 在参考孔中未指定对应层，"
                            "本次计算未锁定")
        if not ref["layers"]:
            return bad("参考孔没有任何层段，请先录入")
        for hid, hole in holes.items():
            if hid == ref_id:
                continue
            if not hole["layers"]:
                warnings.append(f"{hole['name']} 尚无层段，已跳过")
                continue
            hole_layers = prepare_layers(hole["layers"], hole["collar_elev"])
            hole_order = {ly["id"]: i for i, ly in enumerate(hole_layers)}
            anchors, skipped = [], []
            for _, ref_lid, mk in ref_markers:
                lid = mk["links"].get(str(hid))
                if lid and int(lid) in hole_order:
                    anchors.append((ref_order[ref_lid],
                                    hole_order[int(lid)], mk["label"]))
                else:
                    skipped.append(mk["label"])
            # 顺序一致校验（禁止交叉穿越 / 同层重复）
            anchor_pairs = [(a[0], a[1]) for a in anchors]
            if any(anchor_pairs[k][1] >= anchor_pairs[k+1][1]
                   for k in range(len(anchor_pairs) - 1)):
                return bad(
                    f"孔「{hole['name']}」的锁定标志层层序与参考孔冲突（交叉或同层重复），"
                    "请检查标志层位置"
                )
            if skipped:
                warnings.append(f"{hole['name']} 缺少标志层 "
                                f"{'、'.join(skipped)}，该标志层对此孔不生效")
            res = correlate(
                ref_layers, hole_layers, anchor_pairs,
                settings["weights"], settings["elev_scale"],
                settings["gap_open"], settings["gap_extend"],
            )
            res.update({
                "hole_id": hid,
                "hole_name": hole["name"],
                "x": hole["x"],
                "collar_elev": hole["collar_elev"],
                "anchor_labels": [a[2] for a in anchors],
                "layers_geo": [
                    {
                        "id": ly["id"], "seq": ly["seq"],
                        "lithology": ly["lithology"], "grain": ly["grain"],
                        "water": ly["water"], "confidence": ly["confidence"],
                        "ztop": round(ly["ztop"], 3),
                        "zbottom": round(ly["zbottom"], 3),
                        "zmid": round(ly["zmid"], 3),
                    } for ly in hole_layers
                ],
            })
            hole_results.append(res)

        ref_geo = [
            {
                "id": ly["id"], "seq": ly["seq"],
                "lithology": ly["lithology"], "grain": ly["grain"],
                "water": ly["water"], "confidence": ly["confidence"],
                "ztop": round(ly["ztop"], 3),
                "zbottom": round(ly["zbottom"], 3),
                "zmid": round(ly["zmid"], 3),
            } for ly in ref_layers
        ]
        if not hole_results:
            return bad("除参考孔外没有其他含层段的钻孔，无法关联")
        avg_score = round(
            sum(r["total_score"] for r in hole_results) / len(hole_results), 2
        )
        run_id = uuid.uuid4().hex
        result = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": run_id,
            "reference": {
                "hole_id": ref_id, "hole_name": ref["name"],
                "x": ref["x"], "collar_elev": ref["collar_elev"],
                "layers_geo": ref_geo,
            },
            "locked_marker_ids": [int(m) for m in locked_ids],
            "locked_markers": [
                {"id": m["id"], "label": m["label"],
                 "description": m["description"]}
                for _, _, m in ref_markers
            ],
            "parameters": {
                "weights": settings["weights"],
                "elev_scale": settings["elev_scale"],
                "gap_open": settings["gap_open"],
                "gap_extend": settings["gap_extend"],
            },
            "holes": hole_results,
            "warnings": warnings,
            "average_score": avg_score,
        }
        _save_result(conn, result, run_id)
        # 记住上次的选择，刷新页面后仍在
        settings["last_run"] = {"reference_id": ref_id,
                                "marker_ids": result["locked_marker_ids"]}
        conn.execute("UPDATE settings SET payload=? WHERE id=1",
                     (json.dumps(settings, ensure_ascii=False),))
        conn.commit()
        # 新计算尚无定案；附上空 decision / effective 视图后返回
        return jsonify(_attach_decisions(conn, result))
    finally:
        conn.close()


@app.get("/api/result")
def api_result():
    return jsonify(_load_result())


# ---------------------------------------------------------------------------
# 人工定案（并列最优归属的人工选择，从属于某次计算 run_id）
# ---------------------------------------------------------------------------

def _current_run(conn):
    """返回 (run_id, result)；无有效结果或 run_id 时抛 ValueError。"""
    row = conn.execute(
        "SELECT payload, run_id FROM run_result WHERE id=1"
    ).fetchone()
    if not row or not row["payload"] or not row["run_id"]:
        raise ValueError("当前没有有效的关联计算结果，请先重新计算")
    return row["run_id"], json.loads(row["payload"])


@app.post("/api/decisions")
def api_decisions_submit():
    """一次提交一口或多口钻孔的定案。

    body: {"run_id": "<提交时所在页面结果的 run_id>",
           "choices": [{"hole_id": 3, "solution_index": 1}, ...]}

    原子性：任一选择不属于当前计算（run_id 过期、孔不在本次结果中、
    该孔无并列、下标越界或不是当前列出的最优归属），整次提交均不生效，
    已有的其他孔定案保持不变（409/400，不写库）。
    """
    data = request.get_json(force=True, silent=True) or {}
    run_id = data.get("run_id")
    choices = data.get("choices")
    if not run_id or not isinstance(run_id, str):
        return bad("缺少 run_id（请基于当前页面结果提交定案）")
    if not isinstance(choices, list) or not choices:
        return bad("choices 必须是非空列表")

    conn = dbmod.get_db()
    try:
        try:
            cur_run, result = _current_run(conn)
        except ValueError as e:
            return bad(str(e), 409)
        if run_id != cur_run:
            # 过期页面：层段/标志层/参数已变或已重新计算，拒绝写回新结果
            return bad("提交已过期：结果已重新计算或数据已变化，定案未生效", 409)

        holes_by_id = {h["hole_id"]: h for h in result["holes"]}
        settings = _settings(conn)
        boreholes = {b["id"]: b for b in _all_boreholes(conn)}
        ref_b = boreholes[result["reference"]["hole_id"]]
        ref_layers = prepare_layers(ref_b["layers"], ref_b["collar_elev"])

        # 先在事务内完成全部校验与定案构造，任何一步失败都不写库。
        # solution_index 为 null 表示在该批次中取消该孔已有定案（恢复主解）。
        prepared = []
        seen_holes = set()
        for ch in choices:
            if not isinstance(ch, dict):
                return bad("choices 元素必须是 {hole_id, solution_index}")
            hid = ch.get("hole_id")
            idx = ch.get("solution_index")
            if not isinstance(hid, int) or not (
                    isinstance(idx, int) or idx is None):
                return bad("hole_id 必须是整数，solution_index 必须是整数或 null")
            if hid in seen_holes:
                return bad(f"钻孔 {hid} 在一次提交中出现了多次")
            seen_holes.add(hid)
            h = holes_by_id.get(hid)
            if h is None:
                return bad(f"钻孔 {hid} 不在当前关联结果中（定案未生效）", 409)
            sols = h.get("tie_solutions", [])
            if h.get("solution_count", 1) <= 1 or len(sols) <= 1:
                return bad(f"孔「{h['hole_name']}」不存在并列最优，无需定案")
            if idx is None:
                prepared.append((hid, None, None))
                continue
            if not 0 <= idx < len(sols):
                return bad(f"孔「{h['hole_name']}」方案序号 {idx} 不属于当前计算"
                           "（定案未生效）", 409)
            sol = sols[idx]
            # 归属必须确为当前列出的最优方案（按匹配对 + 两侧未匹配层核对，
            # JSON 反序列化后匹配对是 list，需逐对转 tuple）
            def _sig(s):
                return (tuple(sorted(tuple(x) for x in s["pairs"])),
                        tuple(sorted(s["unmatched_ref"])),
                        tuple(sorted(s["unmatched_hole"])))
            if _sig(sol) not in {_sig(s) for s in sols}:
                return bad(f"孔「{h['hole_name']}」所选归属不属于当前计算"
                           "（定案未生效）", 409)

            hb = boreholes.get(hid)
            if hb is None:
                return bad(f"钻孔 {hid} 已不存在（定案未生效）", 409)
            hole_layers = prepare_layers(hb["layers"], hb["collar_elev"])
            marker_pairs = {(a["ref_layer_id"], a["hole_layer_id"])
                            for a in h.get("anchors", [])}
            try:
                view = build_choice(
                    ref_layers, hole_layers,
                    [tuple(p) for p in sol["pairs"]],
                    sol["unmatched_ref"], sol["unmatched_hole"],
                    marker_pairs, settings["weights"], settings["elev_scale"],
                )
            except KeyError as e:
                return bad(f"孔「{h['hole_name']}」{e.args[0] if e.args else ''}"
                           "（定案未生效）", 409)
            prepared.append((hid, idx, view))

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for hid, idx, view in prepared:
            if idx is None:
                conn.execute(
                    "DELETE FROM decision WHERE run_id=? AND hole_id=?",
                    (cur_run, hid))
                continue
            conn.execute(
                "INSERT INTO decision (run_id, hole_id, solution_index,"
                " payload, decided_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(run_id, hole_id) DO UPDATE SET"
                " solution_index=excluded.solution_index,"
                " payload=excluded.payload, decided_at=excluded.decided_at",
                (cur_run, hid, idx,
                 json.dumps(view, ensure_ascii=False), now),
            )
        conn.commit()
        return jsonify(_attach_decisions(conn, result))
    finally:
        conn.close()


@app.delete("/api/decisions")
def api_decisions_clear():
    """清除定案以恢复自动主解。

    body（可省略）: {"run_id": "...", "hole_ids": [3, 5]}
    * 不带 hole_ids：清除本次计算的全部定案；
    * 带 hole_ids：只清除指定孔；
    * run_id 与当前结果不一致时（过期页面）返回 409，不动任何定案。
    """
    data = request.get_json(force=True, silent=True) or {}
    conn = dbmod.get_db()
    try:
        row = conn.execute(
            "SELECT payload, run_id FROM run_result WHERE id=1"
        ).fetchone()
        if not row or not row["payload"] or not row["run_id"]:
            return jsonify({"ok": True})
        cur_run = row["run_id"]
        run_id = data.get("run_id")
        if run_id is not None and run_id != cur_run:
            return bad("页面结果已过期，未执行清除", 409)
        hole_ids = data.get("hole_ids")
        if hole_ids:
            if not isinstance(hole_ids, list) or not all(
                    isinstance(x, int) for x in hole_ids):
                return bad("hole_ids 必须是整数列表")
            qmarks = ",".join("?" * len(hole_ids))
            conn.execute(
                f"DELETE FROM decision WHERE run_id=? AND hole_id IN ({qmarks})",
                (cur_run, *hole_ids),
            )
        else:
            conn.execute("DELETE FROM decision WHERE run_id=?", (cur_run,))
        conn.commit()
        return jsonify(_attach_decisions(conn, json.loads(row["payload"])))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 导出 / 演示数据
# ---------------------------------------------------------------------------

@app.get("/api/export")
def api_export():
    conn = dbmod.get_db()
    try:
        payload = {
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": "钻孔地层关联校核台",
            "boreholes": _all_boreholes(conn),
            "markers": _all_markers(conn),
            "settings": _settings(conn),
            # last_result 已按 run_id 挂上人工定案（hole.decision / effective）
            "last_result": _load_result(conn),
        }
    finally:
        conn.close()
    fname = time.strftime("correlation_%Y%m%d_%H%M%S.json")
    from flask import Response
    resp = Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    mimetype="application/json")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.post("/api/demo/reset")
def api_demo_reset():
    conn = dbmod.get_db()
    try:
        conn.execute("DELETE FROM marker_layer")
        conn.execute("DELETE FROM marker")
        conn.execute("DELETE FROM layer")
        conn.execute("DELETE FROM borehole")
        conn.commit()
        seed_if_empty(conn)
        # 清除上次运行选择（id 已因重建变化）
        settings = _settings(conn)
        settings.pop("last_run", None)
        conn.execute("UPDATE settings SET payload=? WHERE id=1",
                     (json.dumps(settings, ensure_ascii=False),))
        conn.commit()
    finally:
        conn.close()
    _invalidate_result()
    return jsonify({"ok": True})


def create_app():
    dbmod.init_db()
    conn = dbmod.get_db()
    try:
        seed_if_empty(conn)
    finally:
        conn.close()
    return app
