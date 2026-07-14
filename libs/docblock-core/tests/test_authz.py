#!/usr/bin/env python3
"""NodeAuthz(docblock_core/authz.py)單元測試。

需要一個已套用 deployments/docker/postgres/init/01_schema.sql 的 PostgreSQL;
fixture 全部寫在 tenant 'authz-test' 底下,開頭先清掉同 tenant 舊資料,
可重複執行,不影響其他 tenant。

用法:
  AUTHZ_TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:15433/docblock \
      python3 libs/docblock-core/tests/test_authz.py

測試樹(entry 格式: 主體 effect actions [inh=inherit_to_children]):

  /A                folder  owner=A  inherit_acl=F
                    dept A allow browse/query/read
                    dept C allow browse [inh=F]
    doc1            (無 entry,靠繼承)
    sub/            dept B allow browse/query/read
      doc2          (無 entry,兩層繼承)
    secret/         dept A deny browse/query/read   <- 最近節點蓋過根層 allow
      doc3
    doc4            inherit_acl=F;user u4 allow browse
  /B                folder  owner=B  inherit_acl=F
                    dept B allow browse/query/read
    doc5            user u1 allow browse/query/read;user u2 deny query
    doc6            dept B deny query;user u2 allow query   <- user > dept
    doc7            dept B deny query;dept B2 allow query   <- 同類 deny > allow

  u1=A員+A的KM  u2=B員  u3=C員  u4=A員(非KM)  u6=B+B2員
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

from docblock_core.authz import NodeAuthz

PG_DSN = os.environ.get(
    "AUTHZ_TEST_PG_DSN", "postgresql://postgres:test@127.0.0.1:15433/docblock"
)
TENANT = "authz-test"

U1 = "00000000-0000-0000-0000-000000000001"
U2 = "00000000-0000-0000-0000-000000000002"
U3 = "00000000-0000-0000-0000-000000000003"
U4 = "00000000-0000-0000-0000-000000000004"
U6 = "00000000-0000-0000-0000-000000000006"

FA = "10000000-0000-0000-0000-00000000000a"  # /A
FS = "10000000-0000-0000-0000-00000000000b"  # /A/sub
FX = "10000000-0000-0000-0000-00000000000c"  # /A/secret
FB = "10000000-0000-0000-0000-00000000000d"  # /B
D1 = "20000000-0000-0000-0000-000000000001"
D2 = "20000000-0000-0000-0000-000000000002"
D3 = "20000000-0000-0000-0000-000000000003"
D4 = "20000000-0000-0000-0000-000000000004"
D5 = "20000000-0000-0000-0000-000000000005"
D6 = "20000000-0000-0000-0000-000000000006"
D7 = "20000000-0000-0000-0000-000000000007"
BOGUS = "99999999-9999-9999-9999-999999999999"


def setup_fixture() -> None:
    nodes = [
        # (id, parent, type, name, owner, inherit_acl)
        (FA, None, "folder", "A", "A", False),
        (D1, FA, "document", "doc1", "A", True),
        (FS, FA, "folder", "sub", "A", True),
        (D2, FS, "document", "doc2", "A", True),
        (FX, FA, "folder", "secret", "A", True),
        (D3, FX, "document", "doc3", "A", True),
        (D4, FA, "document", "doc4", "A", False),
        (FB, None, "folder", "B", "B", False),
        (D5, FB, "document", "doc5", "B", True),
        (D6, FB, "document", "doc6", "B", True),
        (D7, FB, "document", "doc7", "B", True),
    ]
    entries = [
        # (node, subject_type, subject_id, actions, effect, inherit_to_children)
        (FA, "department", "A", ["browse", "query", "read"], "allow", True),
        (FA, "department", "C", ["browse"], "allow", False),
        (FS, "department", "B", ["browse", "query", "read"], "allow", True),
        (FX, "department", "A", ["browse", "query", "read"], "deny", True),
        (D4, "user", U4, ["browse"], "allow", True),
        (FB, "department", "B", ["browse", "query", "read"], "allow", True),
        (D5, "user", U1, ["browse", "query", "read"], "allow", True),
        (D5, "user", U2, ["query"], "deny", True),
        (D6, "department", "B", ["query"], "deny", True),
        (D6, "user", U2, ["query"], "allow", True),
        (D7, "department", "B", ["query"], "deny", True),
        (D7, "department", "B2", ["query"], "allow", True),
    ]
    principals = [
        (U1, "department", "A"),
        (U1, "role", "dept:A:role:KM"),
        (U2, "department", "B"),
        (U3, "department", "C"),
        (U4, "department", "A"),
        (U6, "department", "B"),
        (U6, "department", "B2"),
    ]

    with psycopg2.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM acl_entries WHERE tenant_id = %s", (TENANT,))
            cur.execute("DELETE FROM nodes WHERE tenant_id = %s", (TENANT,))
            cur.execute("DELETE FROM user_principal WHERE tenant_id = %s", (TENANT,))
            for nid, parent, ntype, name, owner, inherit in nodes:
                cur.execute(
                    """INSERT INTO nodes (id, tenant_id, parent_id, node_type, name,
                                          owner_department_id, inherit_acl)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (nid, TENANT, parent, ntype, name, owner, inherit),
                )
            for node, stype, sid, actions, effect, inh in entries:
                for action in actions:
                    cur.execute(
                        """INSERT INTO acl_entries (tenant_id, node_id, subject_type,
                                                    subject_id, action, effect,
                                                    inherit_to_children)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (TENANT, node, stype, sid, action, effect, inh),
                    )
            for uid, ptype, pid in principals:
                cur.execute(
                    """INSERT INTO user_principal (tenant_id, user_id, principal_type,
                                                   principal_id)
                       VALUES (%s,%s,%s,%s)""",
                    (TENANT, uid, ptype, pid),
                )


CASES = [
    # (說明, user, action, node, expected)
    ("部門 allow 直接命中", U4, "browse", FA, True),
    ("一層繼承", U4, "browse", D1, True),
    ("inherit_to_children=F 對本節點有效", U3, "browse", FA, True),
    ("inherit_to_children=F 不下傳", U3, "browse", D1, False),
    ("兩層繼承", U2, "browse", D2, True),
    ("兩層繼承 read", U2, "read", D2, True),
    ("無規則 action -> default deny", U2, "upload", D2, False),
    ("最近節點 deny 蓋過根層 allow(資料夾)", U4, "browse", FX, False),
    ("最近節點 deny 蓋過根層 allow(文件)", U4, "browse", D3, False),
    ("owner-KM 捷徑蓋過 deny(anti-lockout)", U1, "browse", D3, True),
    ("節點自身 user entry", U4, "browse", D4, True),
    ("inherit_acl=F 擋住上層 allow", U4, "query", D4, False),
    ("對照組:同 action 經繼承可過", U4, "query", D1, True),
    ("owner-KM:完全沒有 delete 規則也放行", U1, "delete", D1, True),
    ("非 KM 無 delete 規則 -> deny", U2, "delete", D5, False),
    ("user deny 在最近節點決定", U2, "query", D5, False),
    ("同節點無此 action 規則 -> 上層 dept allow", U2, "browse", D5, True),
    ("跨部門 user allow", U1, "browse", D5, True),
    ("完全無規則 -> default deny", U3, "query", D5, False),
    ("同節點 user allow > dept deny", U2, "query", D6, True),
    ("dept deny(無 user 規則)", U6, "query", D6, False),
    ("同類主體 deny > allow(多部門使用者)", U6, "query", D7, False),
    ("depth0 無 browse 規則 -> 上層 allow", U6, "browse", D7, True),
]


def main() -> int:
    setup_fixture()
    authz = NodeAuthz(pg_dsn=PG_DSN, tenant_id=TENANT)

    failed = 0
    for desc, user, action, node, expected in CASES:
        got = authz.evaluate_one(user_id=user, action=action, node_id=node)
        ok = got == expected
        if not ok:
            failed += 1
        print(f"{'PASS' if ok else 'FAIL'}: {desc} (expect {expected}, got {got})")

    # 不存在的節點 -> None
    got = authz.evaluate_one(user_id=U1, action="browse", node_id=BOGUS)
    ok = got is None
    failed += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}: 不存在節點回 None (got {got})")

    # 批次過濾:保序、去重、不存在視同 deny
    got_list = authz.filter_allowed(
        user_id=U4, action="query", node_ids=[D1, D4, D5, BOGUS, D1]
    )
    ok = got_list == [D1]
    failed += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}: filter_allowed 保序去重 (got {got_list})")

    # ctx 重用路徑
    ctx = authz.fetch_user_context(U1)
    ok = ctx.km_departments == ["A"] and authz.evaluate_one(
        user_id=U1, action="delete", node_id=D1, ctx=ctx
    )
    failed += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}: fetch_user_context + ctx 重用 (km={ctx.km_departments})")

    # 未知 action
    try:
        authz.evaluate(user_id=U1, action="hack", node_ids=[D1])
        print("FAIL: 未知 action 未拋錯")
        failed += 1
    except ValueError:
        print("PASS: 未知 action 拋 ValueError")

    total = len(CASES) + 4
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
