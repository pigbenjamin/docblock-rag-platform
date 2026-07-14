# core/authz.py
"""Node-tree authorization(File Browser 整合 FB-2)。

取代 docblock_core.acl 的 document_acl detail/summary 三級制:權限掛在
nodes 目錄樹的 acl_entries 上(action × allow/deny),沿資料夾樹向下繼承。
document 節點的 id 就是 documents.document_id(單一 UUID),所以檢索端可以
直接拿 chunk 的 document_id 來問 query 權限,不需要多一層 node 對照。

判定規則(規格書 §7.2 + 既有的 user override 慣例):

1. **Owner-KM 捷徑**:使用者持有 node.owner_department_id 部門的 KM 角色
   (user_principal 有 ('role', 'dept:{d}:role:KM'))時,對該節點的所有
   action 一律 allow,且 deny entries 蓋不掉它——否則一條 deny 就能把
   部門 KM 鎖在自己部門的子樹外面(anti-lockout)。
2. 否則從節點沿 parent 鏈往上找:
   - 祖先節點的 entry 要 inherit_to_children = true 才算數
   - 節點 inherit_acl = false 時,不再往它的上層找(繼承起點控制)
   - 最近一個「有任何相符規則」的節點決定結果
   - 同一節點內的優先序:user 規則 > role 規則 > department 規則;
     同類主體中 deny > allow
3. 整條鏈都沒有相符規則 -> deny(default deny)。

過渡期註記:舊上傳流程建立的文件沒有 node(FB-3 之後才會在 upload 時建),
evaluate() 對不存在的 node id 不回傳結果,呼叫端自行決定當 404 或 deny。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import psycopg2

ACTIONS = (
    "browse", "query", "read", "upload",
    "update", "delete", "move", "manage_acl",
)

_KM_ROLE_PREFIX = "dept:"
_KM_ROLE_SUFFIX = ":role:KM"

# 遞迴上限:目錄樹深度的安全網(move 端點另外負責防循環,這裡防的是
# 資料異常時 recursive CTE 無限展開)
_MAX_TREE_DEPTH = 64


@dataclass
class UserAuthzContext:
    """一個使用者做授權判定所需的全部身份資訊(查一次 user_principal)。"""
    user_id: str
    departments: List[str] = field(default_factory=list)
    role_principal_ids: List[str] = field(default_factory=list)  # 如 'dept:A:role:KM'

    @property
    def km_departments(self) -> List[str]:
        out = []
        for rid in self.role_principal_ids:
            if rid.startswith(_KM_ROLE_PREFIX) and rid.endswith(_KM_ROLE_SUFFIX):
                out.append(rid[len(_KM_ROLE_PREFIX):-len(_KM_ROLE_SUFFIX)])
        return out


_EVALUATE_SQL = """
WITH RECURSIVE chain(start_id, id, parent_id, inherit_acl, depth) AS (
  SELECT n.id, n.id, n.parent_id, n.inherit_acl, 0
  FROM nodes n
  WHERE n.tenant_id = %(tenant_id)s AND n.id = ANY(%(node_ids)s::uuid[])
  UNION ALL
  SELECT c.start_id, p.id, p.parent_id, p.inherit_acl, c.depth + 1
  FROM chain c
  JOIN nodes p ON p.id = c.parent_id
  WHERE c.inherit_acl AND c.depth < %(max_depth)s
),
decisions AS (
  SELECT DISTINCT ON (c.start_id)
    c.start_id,
    e.effect
  FROM chain c
  JOIN acl_entries e
    ON e.tenant_id = %(tenant_id)s
   AND e.node_id = c.id
   AND e.action = %(action)s
   AND (c.depth = 0 OR e.inherit_to_children)
   AND (
        (e.subject_type = 'user' AND e.subject_id = %(user_id)s)
     OR (e.subject_type = 'role' AND e.subject_id = ANY(%(role_ids)s::text[]))
     OR (e.subject_type = 'department' AND e.subject_id = ANY(%(departments)s::text[]))
   )
  ORDER BY c.start_id,
           c.depth,
           CASE e.subject_type WHEN 'user' THEN 0 WHEN 'role' THEN 1 ELSE 2 END,
           CASE e.effect WHEN 'deny' THEN 0 ELSE 1 END
)
SELECT s.id::text, s.owner_department_id, d.effect
FROM nodes s
LEFT JOIN decisions d ON d.start_id = s.id
WHERE s.tenant_id = %(tenant_id)s AND s.id = ANY(%(node_ids)s::uuid[])
"""


class NodeAuthz:
    def __init__(self, *, pg_dsn: str, tenant_id: str):
        if not pg_dsn:
            raise ValueError("pg_dsn is required")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.pg_dsn = pg_dsn
        self.tenant_id = tenant_id

    def fetch_user_context(self, user_id: str) -> UserAuthzContext:
        q = """
        SELECT principal_type, principal_id
        FROM user_principal
        WHERE tenant_id = %s AND user_id = %s
        """
        with psycopg2.connect(self.pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(q, (self.tenant_id, user_id))
                rows = cur.fetchall()

        ctx = UserAuthzContext(user_id=user_id)
        for ptype, pid in rows:
            if ptype == "department":
                ctx.departments.append(pid)
            elif ptype == "role":
                ctx.role_principal_ids.append(pid)
        return ctx

    def evaluate(
        self,
        *,
        user_id: str,
        action: str,
        node_ids: Sequence[str],
        ctx: UserAuthzContext | None = None,
    ) -> Dict[str, bool]:
        """批次判定 user 對每個 node 是否可執行 action。

        回傳 {node_id: bool},只包含實際存在的節點——不存在的 id 不出現在
        結果裡,由呼叫端決定回 404 還是視同 deny。可傳入預先抓好的 ctx
        避免同一請求內重複查 user_principal。
        """
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; must be one of {ACTIONS}")

        ids = [str(n) for n in node_ids if n]
        if not ids:
            return {}

        if ctx is None:
            ctx = self.fetch_user_context(user_id)
        km_depts = set(ctx.km_departments)

        params = {
            "tenant_id": self.tenant_id,
            "node_ids": ids,
            "action": action,
            "user_id": user_id,
            "role_ids": ctx.role_principal_ids,
            "departments": ctx.departments,
            "max_depth": _MAX_TREE_DEPTH,
        }
        with psycopg2.connect(self.pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_EVALUATE_SQL, params)
                rows = cur.fetchall()

        out: Dict[str, bool] = {}
        for node_id, owner_dept, effect in rows:
            if owner_dept in km_depts:
                out[node_id] = True  # owner-KM 捷徑,deny 蓋不掉(anti-lockout)
            else:
                out[node_id] = effect == "allow"
        return out

    def evaluate_one(
        self,
        *,
        user_id: str,
        action: str,
        node_id: str,
        ctx: UserAuthzContext | None = None,
    ) -> bool | None:
        """單節點判定。節點不存在時回 None(呼叫端決定 404/403)。"""
        result = self.evaluate(user_id=user_id, action=action, node_ids=[node_id], ctx=ctx)
        return result.get(str(node_id))

    def filter_allowed(
        self,
        *,
        user_id: str,
        action: str,
        node_ids: Sequence[str],
        ctx: UserAuthzContext | None = None,
    ) -> List[str]:
        """回傳 node_ids 中 user 可執行 action 的子集(保持輸入順序)。

        檢索端用法:candidate chunks 的 document_id 就是 node id,
        filter_allowed(action='query') 之後才能進 reranker / LLM。
        不存在的節點視同 deny(過渡期舊資料、或已被刪除的文件)。
        """
        allowed = self.evaluate(user_id=user_id, action=action, node_ids=node_ids, ctx=ctx)
        seen = set()
        out: List[str] = []
        for n in node_ids:
            key = str(n)
            if key in seen:
                continue
            seen.add(key)
            if allowed.get(key):
                out.append(key)
        return out


def list_document_ids(
    *,
    pg_dsn: str,
    tenant_id: str,
    candidate_document_ids: Optional[Sequence[str]] = None,
    limit: int = 10000,
) -> List[str]:
    """All document ids for a tenant, or the given candidates intersected
    with what actually exists - unfiltered by permission. Callers narrow the
    result with NodeAuthz.filter_allowed/evaluate before using it.

    This only looks at `documents` (completed ingests), not `nodes` - a
    document mid-upload has a node but no row here yet, and isn't a valid
    search/query candidate either way.
    """
    params: List[object] = [tenant_id]
    where_candidates = ""
    if candidate_document_ids is not None:
        cand = [d for d in candidate_document_ids if d]
        if not cand:
            return []
        where_candidates = "AND document_id = ANY(%s::uuid[])"
        params.append(cand)
    params.append(limit)

    q = f"""
        SELECT document_id::text FROM documents
        WHERE tenant_id = %s {where_candidates}
        ORDER BY document_id
        LIMIT %s
    """  # noqa: S608 - where_candidates is a fixed literal, never user input

    with psycopg2.connect(pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()
    return [str(r[0]) for r in rows]
