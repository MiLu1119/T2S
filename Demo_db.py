"""
demo_db.py
----------
构建一个小型合成 sqlite 数据库，风格上模拟 BIRD（多表关联 + 常见业务查询），
用于在没有真实 BIRD 数据（本沙盒无法访问 BIRD 官方下载源）的情况下，
先把 Reward Engine 的正确性验证跑通。

拿到真实 BIRD 数据后，唯一要改的就是 db_path 和 ground_truth_sql 的来源，
Reward Engine 本身不需要改动。
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("DEMO_DB_PATH", str(Path.cwd() / "demo.sqlite"))).resolve()


def build_demo_db(path: Path = DB_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE departments (
            dept_id INTEGER PRIMARY KEY,
            dept_name TEXT NOT NULL
        );

        CREATE TABLE employees (
            emp_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            dept_id INTEGER NOT NULL,
            salary REAL NOT NULL,
            hire_date TEXT NOT NULL,
            FOREIGN KEY (dept_id) REFERENCES departments(dept_id)
        );

        CREATE TABLE sales (
            sale_id INTEGER PRIMARY KEY,
            emp_id INTEGER NOT NULL,
            product TEXT NOT NULL,
            amount REAL NOT NULL,
            sale_date TEXT NOT NULL,
            FOREIGN KEY (emp_id) REFERENCES employees(emp_id)
        );
        """
    )

    cur.executemany(
        "INSERT INTO departments VALUES (?, ?)",
        [
            (1, "Sales"),
            (2, "Engineering"),
            (3, "Marketing"),
        ],
    )

    cur.executemany(
        "INSERT INTO employees VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Alice", 1, 85000.0, "2021-03-01"),
            (2, "Bob", 1, 92000.0, "2020-07-15"),
            (3, "Carol", 2, 120000.0, "2019-01-10"),
            (4, "Dave", 2, 115000.0, "2022-05-20"),
            (5, "Eve", 3, 78000.0, "2023-02-01"),
        ],
    )

    cur.executemany(
        "INSERT INTO sales VALUES (?, ?, ?, ?, ?)",
        [
            (1, 1, "Widget", 1500.50, "2024-01-05"),
            (2, 1, "Gadget", 2300.00, "2024-02-10"),
            (3, 2, "Widget", 800.25, "2024-01-20"),
            (4, 5, "Campaign", 5000.00, "2024-03-01"),
            (5, 2, "Gadget", 1200.00, "2024-03-15"),
        ],
    )

    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    p = build_demo_db()
    print(f"demo db built at: {p}")
