"""人工定案：方案校验与结果合并（不依赖 Flask，可直接单测）。

口径：
* 一次提交可含一口或多口钻孔的归属选择；只要任一项不属于当前计算
  （run_id 过期 / 孔不存在 / 该孔无并列 / 下标越界），整次提交均不生效；
* 定案只是“从当前 tie_solutions 中选定一条展示”，不回写算法结果：
  自动主解字段（pairs / unmatched_* / 分数 / solution_count /
  solutions_listed / ambiguities）原样保留，定案内容挂在每孔的 decision
  字段，并在顶层给出 finalized_hole_ids；
* 未定案孔不携带 decision，页面与导出仍按自动主解展示。
"""


class DecisionError(Exception):
    """校验失败：message 面向用户，code 为 HTTP 状态码，stale 标记过期提交。"""

    def __init__(self, message, code=400, stale=False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.stale = stale


def current_run_row(conn):
    """读取当前结果行 (result_dict|None, run_id|None)。"""
    import json
    row = conn.execute(
        "SELECT payload, run_id FROM run_result WHERE id=1").fetchone()
    if not row or not row["payload"]:
        return None, None
    return json.loads(row["payload"]), row["run_id"]


def attach_decisions(result, run_id, chosen):
    """把 {hole_id: (solution_index, decided_at)} 合并进结果（原地补充）。

    chosen 中无法对应到现存并列方案的条目被忽略（定案只在同一 run 内写入，
    正常不会出现；该兜底保证损坏数据也不会让结果接口 500）。
    """
    finalized = []
    holes_by_id = {h["hole_id"]: h for h in result.get("holes", [])}
    for hole_id, (idx, decided_at) in chosen.items():
        h = holes_by_id.get(hole_id)
        if h is None:
            continue
        sols = h.get("tie_solutions", [])
        if not isinstance(idx, int) or not 0 <= idx < len(sols):
            continue
        sol = sols[idx]
        h["decision"] = {
            "run_id": run_id,
            "hole_id": hole_id,
            "solution_index": idx,
            "decided_at": decided_at,
            "pairs": sol["pair_details"],
            "unmatched_ref": sol["unmatched_ref_details"],
            "unmatched_hole": sol["unmatched_hole_details"],
            "score": sol["score"],
            "gap_count": sol["gap_count"],
            "matched_count": len(sol["pair_details"]),
            "source": "manual",
        }
        finalized.append(hole_id)
    result["run_id"] = run_id
    result["finalized_hole_ids"] = sorted(finalized)
    return result


def parse_commit(result, run_id, data):
    """校验一次定案提交，返回 [(hole_id, solution_index), ...]。

    result/run_id 为当前计算版本；任一非法项抛 DecisionError，调用方不得
    写库。data 为请求体 dict。
    """
    if not isinstance(data, dict):
        raise DecisionError("请求体必须为 JSON 对象")
    req_run = data.get("run_id")
    items = data.get("decisions")
    if not isinstance(req_run, str) or not isinstance(items, list) or not items:
        raise DecisionError("请求体需包含 run_id 与非空 decisions 列表")
    if result is None:
        raise DecisionError(
            "当前没有关联结果，请先重新计算", 409, stale=True)
    if run_id != req_run:
        raise DecisionError(
            "该操作来自已过期的计算结果，请重新计算后再定案",
            409, stale=True)
    holes_by_id = {h["hole_id"]: h for h in result["holes"]}
    parsed, seen = [], set()
    for k, item in enumerate(items):
        if not isinstance(item, dict):
            raise DecisionError(f"第 {k + 1} 项定案格式非法")
        hole_id = item.get("hole_id")
        idx = item.get("solution_index")
        if not isinstance(hole_id, int) or isinstance(hole_id, bool) \
                or not isinstance(idx, int) or isinstance(idx, bool):
            raise DecisionError(
                f"第 {k + 1} 项定案：hole_id / solution_index 必须为整数")
        if hole_id in seen:
            raise DecisionError(f"钻孔 {hole_id} 在同一次提交中出现多次")
        seen.add(hole_id)
        h = holes_by_id.get(hole_id)
        if h is None:
            raise DecisionError(
                f"第 {k + 1} 项定案：钻孔 {hole_id} 不属于当前计算结果")
        if h.get("solution_count", 1) <= 1:
            raise DecisionError(
                f"钻孔「{h['hole_name']}」只有唯一最优解，无需定案")
        if not 0 <= idx < h.get("solutions_listed", 0):
            raise DecisionError(
                f"钻孔「{h['hole_name']}」的方案 {idx + 1} 不在当前列出的"
                f" {h['solutions_listed']} 条并列方案中")
        parsed.append((hole_id, idx))
    return parsed


def parse_clear(result, run_id, data):
    """校验清除请求。返回待清除 hole_id 列表（空列表=全部）。"""
    if not isinstance(data, dict):
        data = {}
    if result is None:
        raise DecisionError("当前没有关联结果，无需清除定案", 409, stale=True)
    if data.get("run_id") != run_id:
        raise DecisionError(
            "该操作来自已过期的计算结果，请重新计算后再清除定案",
            409, stale=True)
    hole_ids = data.get("hole_ids")
    if hole_ids is None:
        return []
    if not isinstance(hole_ids, list) or \
            not all(isinstance(x, int) and not isinstance(x, bool)
                    for x in hole_ids):
        raise DecisionError("hole_ids 必须为整数数组")
    return hole_ids


def load_merged(conn):
    """读取当前结果并合并当前 run 的全部定案；无结果返回 None。"""
    import json
    result, run_id = current_run_row(conn)
    if result is None:
        return None
    if not run_id:
        return result
    rows = conn.execute(
        "SELECT hole_id, solution_index, decided_at FROM decision"
        " WHERE run_id=?", (run_id,)).fetchall()
    if not rows:
        result["run_id"] = run_id
        result["finalized_hole_ids"] = []
        return result
    return attach_decisions(result, run_id, {
        r["hole_id"]: (r["solution_index"], r["decided_at"]) for r in rows
    })
