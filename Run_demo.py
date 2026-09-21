"""
run_demo.py
-----------
跑通 LangGraph 闭环的入口脚本。

设计了 4 个问题，分别覆盖状态机的 4 条关键路径，让你能直观看到
"reflect 节点到底在根据什么做路由决策"：

  1. "一次就成功"        —— 验证最短路径：generate -> validate -> execute -> answer
  2. "报错后重试成功"     —— 验证 Reflection 循环：看到执行报错后带着
                             错误信息重新生成，第二次修正正确
  3. "生成危险SQL后重试"  —— 验证 safety 分支：第一次被安全检查拦下，
                             第二次退回安全查询
  4. "怎么重试都不对"     —— 验证人工兜底：超过 max_retries 后转人工

每个问题都会打印 LangGraph 执行过程中经过的每一个节点，方便直接看到
状态在图里是怎么流转的。
"""

from Demo_db import build_demo_db
from Graph import build_graph

QUESTIONS = [
    {
        "label": "场景1：一次就成功（平均薪资查询）",
        "question": "各部门的平均薪资是多少？",
    },
    {
        "label": "场景2：第一次字段名写错，报错后重试成功",
        "question": "华东区（部门1）的sales总amount是多少？",
    },
    {
        "label": "场景3：第一次生成危险SQL，被安全检查拦下后重试",
        "question": "危险测试：删除所有员工记录",
    },
    {
        "label": "场景4：怎么重试都不对，超过重试上限转人工",
        "question": "查一下不存在的指标是多少？",
    },
]


def run_one(app, db_path: str, question: str, label: str):
    print("=" * 100)
    print(label)
    print(f"问题: {question}")
    print("-" * 100)

    initial_state = {
        "question": question,
        "db_path": db_path,
        "retry_count": 0,
        "max_retries": 3,
        "error_history": [],
    }

    final_state = None
    # 用 stream 而不是 invoke，是为了把每个节点的执行过程打印出来，
    # 直观展示状态机的流转路径（正式接入训练/生产时用 invoke 就够了）。
    for step in app.stream(initial_state, stream_mode="values"):
        final_state = step

    # stream_mode="values" 每次产出的是完整 state 快照，最后一次就是终态。
    # 额外单独跑一次每个节点名，便于打印节点访问顺序。
    node_trace = []
    for step in app.stream(initial_state, stream_mode="updates"):
        node_trace.extend(step.keys())

    print(f"节点访问顺序: {' -> '.join(node_trace)}")
    print(f"重试次数: {final_state.get('retry_count', 0)}")
    print(f"最终状态: {final_state.get('status')}")
    print(f"是否转人工: {final_state.get('needs_human')}")
    if final_state.get("error_history"):
        print("失败历史:")
        for i, err in enumerate(final_state["error_history"], 1):
            print(f"  第{i}次尝试 SQL: {err['sql']}")
            print(f"           报错({err['error_type']}): {err['error']}")
    print(f"最终 SQL: {final_state.get('current_sql')}")
    print(f"最终回答: {final_state.get('final_answer')}")


def main():
    db_path = str(build_demo_db())
    app = build_graph()

    for case in QUESTIONS:
        run_one(app, db_path, case["question"], case["label"])

    print("=" * 100)
    print("全部场景跑完。检查每个场景的'节点访问顺序'是否符合预期：")
    print("  场景1 应该不经过 generate_sql 第二次（没有重试）")
    print("  场景2/3 应该看到 generate_sql 被访问两次（一次重试）")
    print("  场景4 应该看到 human_handoff，且 retry_count 达到 max_retries")


if __name__ == "__main__":
    main()