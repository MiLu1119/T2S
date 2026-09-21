"""
test_reward_engine.py
----------------------
用一组精心设计的测试用例验证 Reward Engine 的行为，尤其是"防作弊"检查：
确保取巧 SQL（SELECT *、恒真条件、危险操作、堆叠注入）不会被误判为高分。

这份测试的地位和 reward 逻辑本身一样重要——上线前必须全绿，
训练过程中如果观察到 reward 曲线异常增长，第一件事就是回来扩充这里的用例。
"""

from dataclasses import dataclass

from Demo_db import build_demo_db
from Reward import compute_reward


@dataclass
class Case:
    name: str
    predicted_sql: str
    ground_truth_sql: str
    expect_correct: bool
    expect_safe: bool
    expect_executed: bool


CASES = [
    # ---------- 1. 正常正确的 SQL ----------
    Case(
        name="correct_simple_filter",
        predicted_sql="SELECT name FROM employees WHERE dept_id = 1",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=True,
        expect_safe=True,
        expect_executed=True,
    ),
    # ---------- 2. 写法不同但结果等价，也应判为正确 ----------
    Case(
        name="correct_different_formulation",
        predicted_sql="""
            SELECT e.name FROM employees e
            JOIN departments d ON e.dept_id = d.dept_id
            WHERE d.dept_name = 'Sales'
        """,
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=True,
        expect_safe=True,
        expect_executed=True,
    ),
    # ---------- 3. 聚合查询正确 ----------
    Case(
        name="correct_aggregation",
        predicted_sql="SELECT dept_id, AVG(salary) FROM employees GROUP BY dept_id",
        ground_truth_sql="SELECT dept_id, AVG(salary) FROM employees GROUP BY dept_id",
        expect_correct=True,
        expect_safe=True,
        expect_executed=True,
    ),
    # ---------- 4. 取巧：SELECT * 想蒙混过关，结果集不匹配 ----------
    Case(
        name="cheat_select_star",
        predicted_sql="SELECT * FROM employees",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=True,
        expect_executed=True,
    ),
    # ---------- 5. 取巧：恒真条件想返回全表凑数 ----------
    Case(
        name="cheat_tautology",
        predicted_sql="SELECT name FROM employees WHERE 1 = 1",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=True,
        expect_executed=True,
    ),
    # ---------- 6. 语法错误（拼错关键字，应落入 syntax error 而非 safety violation）----------
    Case(
        name="syntax_error",
        predicted_sql="SELEC name FROM employees",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=True,
        expect_executed=False,
    ),
    # ---------- 6b. CTE 写法（WITH ... SELECT），确认不会被误伤成不安全 ----------
    Case(
        name="cte_query",
        predicted_sql="""
            WITH sales_dept AS (
                SELECT emp_id FROM employees WHERE dept_id = 1
            )
            SELECT name FROM employees WHERE emp_id IN (SELECT emp_id FROM sales_dept)
        """,
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=True,
        expect_safe=True,
        expect_executed=True,
    ),
    # ---------- 7. 字段不存在 ----------
    Case(
        name="unknown_column",
        predicted_sql="SELECT nonexistent_col FROM employees",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=True,
        expect_executed=False,
    ),
    # ---------- 8. 危险操作：DROP ----------
    Case(
        name="dangerous_drop",
        predicted_sql="DROP TABLE employees",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=False,
        expect_executed=False,
    ),
    # ---------- 9. 危险操作：DELETE 无 WHERE ----------
    Case(
        name="dangerous_delete_all",
        predicted_sql="DELETE FROM employees",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=False,
        expect_executed=False,
    ),
    # ---------- 10. 堆叠注入：看似 SELECT，实则夹带 DROP ----------
    Case(
        name="stacked_query_injection",
        predicted_sql="SELECT * FROM employees; DROP TABLE employees;",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=False,
        expect_executed=False,
    ),
    # ---------- 11. PRAGMA 逃逸尝试 ----------
    Case(
        name="pragma_escape",
        predicted_sql="PRAGMA table_info(employees)",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=False,
        expect_executed=False,
    ),
    # ---------- 12. 空 SQL / 空白 ----------
    Case(
        name="empty_sql",
        predicted_sql="   ",
        ground_truth_sql="SELECT name FROM employees WHERE dept_id = 1",
        expect_correct=False,
        expect_safe=False,
        expect_executed=False,
    ),
]


def run_all(db_path: str) -> bool:
    all_passed = True
    print(f"{'case':30s} {'reward':>7s}  {'safe':>5s} {'exec':>5s} {'correct':>7s}  detail")
    print("-" * 100)
    for case in CASES:
        r = compute_reward(db_path, case.predicted_sql, case.ground_truth_sql)
        ok = (
            r.is_correct == case.expect_correct
            and r.passed_safety == case.expect_safe
            and r.executed_successfully == case.expect_executed
        )
        status = "OK " if ok else "FAIL"
        all_passed = all_passed and ok
        print(
            f"[{status}] {case.name:24s} {r.total_reward:7.2f}  "
            f"{str(r.passed_safety):>5s} {str(r.executed_successfully):>5s} "
            f"{str(r.is_correct):>7s}  {r.detail}"
        )
    return all_passed


if __name__ == "__main__":
    db_path = str(build_demo_db())
    passed = run_all(db_path)
    print("-" * 100)
    if passed:
        print("全部用例通过 ✅  Reward Engine 行为符合预期，可以接入 GRPO / verl 的 reward function。")
    else:
        print("存在未通过的用例 ❌  在接入训练之前必须先修好，否则 reward 信号会误导策略优化。")
        raise SystemExit(1)