"""人工定案功能回归测试。

覆盖：
* 并列方案附带的完整明细（pair_details / unmatched_*_details）；
* 定案提交的原子性：多孔一批，任一非法整批不写库；
* 只有“当前计算列出的方案”可定案：run_id 过期 -> 409，
  孔不属于本次结果 / 无并列 / 下标越界 / 同孔重复 -> 400 且不写库；
* 合并展示：自动主解与算法字段（分数、solution_count、歧义区间）原样
  保留，定案挂 decision、顶层给 finalized_hole_ids；未定案孔按主解；
* 清除：单孔清除恢复自动主解、全部清除；
* 失效：重新计算（新 run_id）、改层段、改参数、演示数据重置后，旧
  定案既不显示也不能凭旧 run_id 写回；
* 导出 JSON 中自动主解与人工定案明确区分且可序列化。

无 Flask 的最小环境只跳过 HTTP 用例（DecisionHttpTest），纯逻辑与
直接 DB 用例不依赖 Flask。

运行：python -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.correlator import correlate, prepare_layers
from app import db as dbmod
from app import decisions as decmod

ZERO_WEIGHTS = {"lithology": 0.0, "grain": 0.0, "water": 0.0,
                "elev": 0.0, "confidence": 0.0}


def make_layers(specs, collar=100.0):
    raw = []
    for seq, (lid, lith) in enumerate(specs, 1):
        raw.append({
            "id": lid, "seq": seq,
            "top": (seq - 1) * 2.0, "bottom": seq * 2.0,
            "lithology": lith, "grain": "中粒", "water": "稍湿",
            "confidence": 0.9,
        })
    return prepare_layers(raw, collar)


class TieSolutionDetailsTest(unittest.TestCase):
    """定案依赖并列方案内的完整明细。"""

    def test_tie_solutions_carry_full_details(self):
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 6)
        for sol in res["tie_solutions"]:
            self.assertEqual(
                [(p["ref_layer_id"], p["hole_layer_id"])
                 for p in sol["pair_details"]],
                sol["pairs"])
            self.assertEqual(
                [u["layer_id"] for u in sol["unmatched_ref_details"]],
                sol["unmatched_ref"])
            self.assertEqual(
                [u["layer_id"] for u in sol["unmatched_hole_details"]],
                sol["unmatched_hole"])
            self.assertEqual(len(sol["pair_details"]), len(sol["pairs"]))
            # 明细对字段完整（供剖面取 zmid/seq 等）
            for p in sol["pair_details"]:
                for k in ("ref_layer_id", "hole_layer_id", "ref_seq",
                          "hole_seq", "score", "marker"):
                    self.assertIn(k, p)
            for u in sol["unmatched_ref_details"] + sol["unmatched_hole_details"]:
                self.assertIn("zmid", u)
                self.assertIn("seq", u)


class _DbCase(unittest.TestCase):
    """直接操作 SQLite + decisions 纯逻辑（不依赖 Flask）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dbmod.DB_PATH
        dbmod.DB_PATH = Path(self._tmp.name) / "t.db"
        dbmod.init_db()
        self.conn = dbmod.get_db()

    def tearDown(self):
        self.conn.close()
        dbmod.DB_PATH = self._orig
        self._tmp.cleanup()

    # -- 构造一个两孔并列的 run_result（模拟 correlate+保存） -------------
    def _seed_run(self, run_id="run-1"):
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        res.update({"hole_id": 7, "hole_name": "H", "x": 10.0,
                    "collar_elev": 100.0, "anchor_labels": [],
                    "layers_geo": [
                        {"id": 201, "seq": 1, "lithology": "砂岩",
                         "grain": "中粒", "water": "稍湿",
                         "confidence": 0.9, "ztop": 100.0,
                         "zbottom": 98.0, "zmid": 99.0},
                        {"id": 202, "seq": 2, "lithology": "砂岩",
                         "grain": "中粒", "water": "稍湿",
                         "confidence": 0.9, "ztop": 98.0,
                         "zbottom": 96.0, "zmid": 97.0},
                    ]})
        result = {"run_id": run_id, "finalized_hole_ids": [],
                  "generated_at": "now",
                  "reference": {"hole_id": 1, "hole_name": "R"},
                  "holes": [res]}
        self.conn.execute("DELETE FROM decision")
        self.conn.execute("DELETE FROM run_result")
        self.conn.execute(
            "INSERT INTO run_result (id,payload,generated_at,run_id)"
            " VALUES (1,?,?,?)",
            (json.dumps(result, ensure_ascii=False), "now", run_id))
        self.conn.commit()
        return result

    def _commit_raw(self, run_id, items):
        """走纯逻辑校验，合法则模拟路由写库；返回 (parsed, error)。"""
        result, rid = decmod.current_run_row(self.conn)
        try:
            parsed = decmod.parse_commit(
                result, rid, {"run_id": run_id, "decisions": items})
        except decmod.DecisionError as e:
            return None, e
        for hole_id, idx in parsed:
            self.conn.execute(
                "INSERT INTO decision (run_id,hole_id,solution_index,decided_at)"
                " VALUES (?,?,?,?) ON CONFLICT(run_id,hole_id) DO UPDATE SET"
                " solution_index=excluded.solution_index,"
                " decided_at=excluded.decided_at",
                (rid, hole_id, idx, "t"))
        self.conn.commit()
        return parsed, None

    def test_valid_commit_merges_decision_without_touching_auto_fields(self):
        res = self._seed_run()
        h = res["holes"][0]
        auto_pairs = [tuple(p) for p in h["tie_solutions"][0]["pairs"]]
        target = 2
        parsed, err = self._commit_raw("run-1", [{"hole_id": 7,
                                                  "solution_index": target}])
        self.assertIsNone(err)
        self.assertEqual(parsed, [(7, target)])
        merged = decmod.load_merged(self.conn)
        mh = merged["holes"][0]
        self.assertEqual(merged["finalized_hole_ids"], [7])
        self.assertEqual(mh["decision"]["solution_index"], target)
        self.assertEqual(
            [(p["ref_layer_id"], p["hole_layer_id"])
             for p in mh["decision"]["pairs"]],
            h["tie_solutions"][target]["pairs"])
        # 自动主解与算法字段原样保留
        self.assertEqual(
            [(p["ref_layer_id"], p["hole_layer_id"]) for p in mh["pairs"]],
            auto_pairs)
        self.assertEqual(mh["solution_count"], 6)
        self.assertEqual(mh["solutions_listed"], 6)
        self.assertEqual(mh["total_score"], h["total_score"])
        self.assertEqual(mh["dp_score"], h["dp_score"])
        self.assertEqual(mh["ambiguities"], h["ambiguities"])
        self.assertEqual(len(mh["tie_solutions"]), 6)

    def test_atomic_batch_bad_item_rejects_whole_batch(self):
        self._seed_run()
        # 第二项下标越界（列出 6 条，下标 6 非法）
        _p, err = self._commit_raw("run-1", [
            {"hole_id": 7, "solution_index": 1},
            {"hole_id": 7, "solution_index": 6},
        ])
        self.assertIsNotNone(err)
        self.assertEqual(err.code, 400)
        self.assertFalse(err.stale)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM decision").fetchone()["c"],
            0, "原子性：任一非法整批不得写库")

    def test_unknown_hole_and_duplicate_rejected(self):
        self._seed_run()
        _p, err = self._commit_raw("run-1", [
            {"hole_id": 999, "solution_index": 0}])
        self.assertEqual(err.code, 400)
        _p, err = self._commit_raw("run-1", [
            {"hole_id": 7, "solution_index": 0},
            {"hole_id": 7, "solution_index": 1}])
        self.assertEqual(err.code, 400)
        _p, err = self._commit_raw("run-1", [
            {"hole_id": 7, "solution_index": -1}])
        self.assertEqual(err.code, 400)

    def test_unique_solution_hole_rejects_decision(self):
        # 正惩罚：唯一最优
        ref = make_layers([(101, "砂岩"), (102, "泥岩")])
        hole = make_layers([(201, "砂岩"), (202, "泥岩")])
        res = correlate(ref, hole, [],
                        {"lithology": 0.45, "grain": 0.15, "water": 0.1,
                         "elev": 0.2, "confidence": 0.1},
                        5.0, 2.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        with self.assertRaises(decmod.DecisionError) as cm:
            decmod.parse_commit(
                {"holes": [dict(res, hole_id=3, hole_name="U")]},
                "r", {"run_id": "r",
                      "decisions": [{"hole_id": 3, "solution_index": 0}]})
        self.assertEqual(cm.exception.code, 400)

    def test_stale_run_id_is_409_and_not_written(self):
        self._seed_run("run-1")
        _p, err = self._commit_raw("run-old", [
            {"hole_id": 7, "solution_index": 0}])
        self.assertEqual(err.code, 409)
        self.assertTrue(err.stale)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM decision").fetchone()["c"],
            0)
        # 过期页面清除也拒绝
        with self.assertRaises(decmod.DecisionError) as cm:
            decmod.parse_clear(
                *decmod.current_run_row(self.conn),
                {"run_id": "run-old", "hole_ids": [7]})
        self.assertEqual(cm.exception.code, 409)

    def test_recalc_new_run_drops_old_decisions_from_view_and_writes(self):
        self._seed_run("run-1")
        self._commit_raw("run-1", [{"hole_id": 7, "solution_index": 1}])
        self.assertEqual(decmod.load_merged(self.conn)["finalized_hole_ids"],
                         [7])
        # 重新计算：新 run 保存时清空 decision 表（模拟 routes._save_result）
        self.conn.execute("DELETE FROM decision")
        res2 = self._seed_run("run-2")
        self.assertEqual(res2["finalized_hole_ids"], [])
        merged = decmod.load_merged(self.conn)
        self.assertEqual(merged["run_id"], "run-2")
        self.assertEqual(merged["finalized_hole_ids"], [])
        self.assertNotIn("decision", merged["holes"][0])
        # 旧 run_id 不能写回新结果
        _p, err = self._commit_raw("run-1", [{"hole_id": 7,
                                              "solution_index": 0}])
        self.assertEqual(err.code, 409)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM decision").fetchone()["c"],
            0)

    def test_clear_single_and_all_restores_primary(self):
        self._seed_run()
        self._commit_raw("run-1", [{"hole_id": 7, "solution_index": 2}])
        merged = decmod.load_merged(self.conn)
        self.assertEqual(merged["finalized_hole_ids"], [7])
        # 单孔清除
        ids = decmod.parse_clear(
            *decmod.current_run_row(self.conn),
            {"run_id": "run-1", "hole_ids": [7]})
        self.assertEqual(ids, [7])
        self.conn.execute(
            "DELETE FROM decision WHERE run_id='run-1' AND hole_id IN (7)")
        self.conn.commit()
        self.assertEqual(decmod.load_merged(self.conn)["finalized_hole_ids"],
                         [])
        # 全部清除（空 hole_ids 语义）
        self._commit_raw("run-1", [{"hole_id": 7, "solution_index": 3}])
        self.assertEqual(decmod.parse_clear(
            *decmod.current_run_row(self.conn), {"run_id": "run-1"}), [])
        self.conn.execute("DELETE FROM decision WHERE run_id='run-1'")
        self.conn.commit()
        self.assertEqual(decmod.load_merged(self.conn)["finalized_hole_ids"],
                         [])

    def test_persistence_across_new_connection(self):
        """定案落库：换一个连接（模拟刷新页面 / 重启容器后）仍在。"""
        self._seed_run()
        self._commit_raw("run-1", [{"hole_id": 7, "solution_index": 4}])
        self.conn.close()
        conn2 = dbmod.get_db()
        try:
            merged = decmod.load_merged(conn2)
            self.assertEqual(merged["run_id"], "run-1")
            self.assertEqual(merged["finalized_hole_ids"], [7])
            self.assertEqual(merged["holes"][0]["decision"]["solution_index"],
                             4)
        finally:
            conn2.close()
        self.conn = dbmod.get_db()


class DecisionHttpTest(unittest.TestCase):
    """端到端 HTTP（无 Flask 自动跳过）。"""

    def setUp(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("未安装 Flask，HTTP 用例在 Docker 内运行")
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dbmod.DB_PATH
        dbmod.DB_PATH = Path(self._tmp.name) / "t.db"
        dbmod.init_db()
        from app.routes import app
        self.client = app.test_client()

    def tearDown(self):
        dbmod.DB_PATH = self._orig
        self._tmp.cleanup()

    def _hole(self, name, n, x=10):
        resp = self.client.post("/api/boreholes", json={
            "name": name, "collar_elev": 100, "x": x,
            "layers": [{
                "top": i * 2.0, "bottom": (i + 1) * 2.0,
                "lithology": "砂岩", "grain": "中粒", "water": "稍湿",
                "confidence": 0.9} for i in range(n)]})
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.get_json()["id"]

    def _tie_run(self, n=2):
        rid = self._hole("R", n, 0)
        hid = self._hole("H", n, 10)
        self.client.put("/api/settings", json={
            "weights": {"lithology": 0, "grain": 0, "water": 0,
                        "elev": 0, "confidence": 0},
            "gap_open": 0, "gap_extend": 0})
        resp = self.client.post("/api/correlate",
                                json={"reference_id": rid, "marker_ids": []})
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        return body, body["holes"][0]

    def test_commit_valid_then_export_distinguishes_auto_and_manual(self):
        body, h = self._tie_run()
        run_id, hid = body["run_id"], h["hole_id"]
        self.assertGreater(h["solution_count"], 1)
        target = 1 if h["solutions_listed"] > 1 else 0
        resp = self.client.post("/api/decisions", json={
            "run_id": run_id,
            "decisions": [{"hole_id": hid, "solution_index": target}]})
        self.assertEqual(resp.status_code, 200, resp.data)
        out = resp.get_json()
        self.assertEqual(out["finalized_hole_ids"], [hid])
        mh = out["holes"][0]
        self.assertEqual(mh["decision"]["solution_index"], target)
        # /api/result 与 /api/state 同样合并
        self.assertEqual(
            self.client.get("/api/result").get_json()["finalized_hole_ids"],
            [hid])
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["result"]["finalized_hole_ids"], [hid])
        # 导出 JSON：自动主解字段与人工定案同时存在、口径分明
        exported = json.loads(self.client.get("/api/export").data)
        eh = exported["last_result"]["holes"][0]
        self.assertEqual(eh["decision"]["solution_index"], target)
        self.assertEqual(eh["decision"]["source"], "manual")
        self.assertEqual(eh["solution_count"], h["solution_count"])
        self.assertEqual(eh["ambiguities"], h["ambiguities"])
        auto_sig = [(p["ref_layer_id"], p["hole_layer_id"])
                    for p in eh["pairs"]]
        dec_sig = [(p["ref_layer_id"], p["hole_layer_id"])
                   for p in eh["decision"]["pairs"]]
        self.assertEqual(
            auto_sig,
            [(p["ref_layer_id"], p["hole_layer_id"]) for p in h["pairs"]])
        self.assertEqual(
            dec_sig, h["tie_solutions"][target]["pairs"])

    def test_batch_atomicity_over_multiple_holes(self):
        # 三孔全并列；构造两孔合法 + 一孔非法（不存在 id）的批量
        body, h1 = self._tie_run()
        # 再加第二、三口孔并整体重算
        self._hole("H2", 2, 20)
        rid = body["reference"]["hole_id"]
        resp = self.client.post("/api/correlate",
                                json={"reference_id": rid, "marker_ids": []})
        body = resp.get_json()
        ids = [x["hole_id"] for x in body["holes"]]
        bad = self.client.post("/api/decisions", json={
            "run_id": body["run_id"],
            "decisions": [
                {"hole_id": ids[0], "solution_index": 1},
                {"hole_id": 99999, "solution_index": 0},
                {"hole_id": ids[1], "solution_index": 1},
            ]})
        self.assertEqual(bad.status_code, 400, bad.data)
        self.assertEqual(
            self.client.get("/api/result").get_json()["finalized_hole_ids"],
            [])
        # 同一批两孔合法 -> 一次提交成功
        ok = self.client.post("/api/decisions", json={
            "run_id": body["run_id"],
            "decisions": [
                {"hole_id": ids[0], "solution_index": 1},
                {"hole_id": ids[1], "solution_index": 2},
            ]})
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(sorted(ok.get_json()["finalized_hole_ids"]),
                         sorted(ids[:2]))

    def test_stale_run_after_recalc_and_edit(self):
        body, h = self._tie_run()
        old_run = body["run_id"]
        # 用旧 run 定案成功
        r = self.client.post("/api/decisions", json={
            "run_id": old_run,
            "decisions": [{"hole_id": h["hole_id"], "solution_index": 0}]})
        self.assertEqual(r.status_code, 200, r.data)
        # 重新计算 -> 新 run，旧定案消失
        body2 = self.client.post("/api/correlate", json={
            "reference_id": body["reference"]["hole_id"],
            "marker_ids": []}).get_json()
        self.assertNotEqual(body2["run_id"], old_run)
        self.assertEqual(body2["finalized_hole_ids"], [])
        self.assertNotIn("decision", body2["holes"][0])
        # 旧页面用旧 run 写回 -> 409，新结果不被污染
        stale = self.client.post("/api/decisions", json={
            "run_id": old_run,
            "decisions": [{"hole_id": h["hole_id"], "solution_index": 0}]})
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.get_json()["stale"])
        self.assertEqual(
            self.client.get("/api/result").get_json()["finalized_hole_ids"],
            [])
        # 改层段 -> 结果失效，旧 run 再提交同样 409
        holes = self.client.get("/api/state").get_json()["boreholes"]
        hrow = next(x for x in holes if x["name"] == "H")
        layers = hrow["layers"]
        layers[-1]["lithology"] = "泥岩"
        self.assertEqual(self.client.put(
            f"/api/boreholes/{hrow['id']}",
            json={"layers": layers}).status_code, 200)
        self.assertIsNone(self.client.get("/api/result").get_json())
        stale2 = self.client.post("/api/decisions", json={
            "run_id": old_run,
            "decisions": [{"hole_id": h["hole_id"], "solution_index": 0}]})
        self.assertEqual(stale2.status_code, 409)

    def test_settings_change_invalidates(self):
        body, h = self._tie_run()
        r = self.client.post("/api/decisions", json={
            "run_id": body["run_id"],
            "decisions": [{"hole_id": h["hole_id"], "solution_index": 0}]})
        self.assertEqual(r.status_code, 200, r.data)
        self.client.put("/api/settings", json={"gap_open": 3.0})
        self.assertIsNone(self.client.get("/api/result").get_json())

    def test_clear_endpoints(self):
        body, h = self._tie_run()
        self.client.post("/api/decisions", json={
            "run_id": body["run_id"],
            "decisions": [{"hole_id": h["hole_id"], "solution_index": 1}]})
        # 单孔清除
        r = self.client.delete("/api/decisions", json={
            "run_id": body["run_id"], "hole_ids": [h["hole_id"]]})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["finalized_hole_ids"], [])
        self.assertNotIn("decision",
                         self.client.get("/api/result").get_json()["holes"][0])
        # 全部清除（重定后省略 hole_ids）
        self.client.post("/api/decisions", json={
            "run_id": body["run_id"],
            "decisions": [{"hole_id": h["hole_id"], "solution_index": 1}]})
        r = self.client.delete("/api/decisions",
                               json={"run_id": body["run_id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["finalized_hole_ids"], [])
        # 过期清除 -> 409
        self.assertEqual(self.client.delete("/api/decisions", json={
            "run_id": "nope"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
