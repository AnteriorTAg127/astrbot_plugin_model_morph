"""「空 id 实体」回归测试（模块 D/R/S/L，修复恶性 bug 家族）。

背景（用户报告）：WebUI 规则引擎新建规则 → 前端 payload 携带 ``id: ""`` →
后端 ``normalize_rule`` 用 ``setdefault`` 保留空串 → 空 id 规则入库 → 删除 / 复制报
「缺少规则 id」，编辑还会静默复制出一条新规则。同类缺陷波及 temporal / groups /
lifecycle 的更新路径（更新载荷带空 id 时实体「改名换姓」，旧引用全部失效）。

本文件覆盖：
- rules：新建空 id 生成真实 id；「建→列→删」全链路；更新载荷空/异 id 不覆盖身份；
  存量空 id 规则删除/复制自愈；
- temporal：同 rules（新建 / 更新身份保护 / 存量自愈）；
- groups / lifecycle：更新载荷空 id 不覆盖身份；存量空 id 删除自愈；
- web 层端到端：rules/save(id="") → rules GET → rules/delete 成功（报告场景）。
"""

import asyncio
import json
import sys
from pathlib import Path

from conftest import make_store
from scheduler.groups import ModelGroupManager
from scheduler.lifecycle import LifecycleEngine
from scheduler.rules import RuleEngine, normalize_rule
from scheduler.temporal import TemporalEngine, PRIORITY_SCHEDULED


def _valid_temporal(**kw):
    """构造一条合法的 temporal 规则（与 test_temporal._rule 同构）。"""
    base = {
        "id": "",
        "name": "R",
        "enabled": True,
        "kind": "model_override",
        "group_id": "",
        "source_provider": "deepseek",
        "target_provider": "cheap",
        "target_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "schedule": {
            "type": "daily",
            "start": "20:00",
            "end": "23:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        },
        "priority": PRIORITY_SCHEDULED,
        "metadata": {"created_by": "", "created_at": "", "source": ""},
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ #
# rules
# ------------------------------------------------------------------ #


def test_normalize_rule_empty_id_regenerated():
    """空串 id 视同缺失，重新生成（setdefault 无法覆盖已存在键是根因）。"""
    rule = normalize_rule({"id": "", "name": "x"})
    assert rule["id"].startswith("r_") and len(rule["id"]) > 2
    rule2 = normalize_rule({"id": "   ", "name": "x"})
    assert rule2["id"].startswith("r_")
    rule3 = normalize_rule({"name": "x"})
    assert rule3["id"].startswith("r_")
    # 非字符串 id 同样生成
    rule4 = normalize_rule({"id": 123, "name": "x"})
    assert rule4["id"].startswith("r_")
    # 正常 id 保留并去空白
    assert normalize_rule({"id": " r_abc "})["id"] == "r_abc"
    # priority 强转 int：字符串 / 非法值不再让 list_ 排序崩溃
    assert normalize_rule({"priority": "5"})["priority"] == 5
    assert normalize_rule({"priority": "abc"})["priority"] == 0
    assert normalize_rule({"priority": None})["priority"] == 0


def test_webui_create_with_empty_id_generates_id():
    """WebUI 新建规则（payload.id == ""）→ 落库 id 为生成的真实 id。"""
    store = make_store()
    eng = RuleEngine(store)
    created = eng.create_rule({"id": "", "name": "测试", "when": {"op": "and", "conditions": []}})
    assert created["id"].startswith("r_")
    stored = store.get_rules()
    assert stored[0]["id"] == created["id"]
    assert stored[0]["id"] != ""


def test_webui_flow_create_list_delete():
    """报告场景：新建（id=""）→ 列表取 id → 删除成功。"""
    store = make_store()
    eng = RuleEngine(store)
    created = eng.create_rule({"id": "", "name": "测试"})
    listed = eng.list_()
    assert [r["id"] for r in listed] == [created["id"]]
    assert eng.delete(created["id"]) is True
    assert eng.list_() == []


def test_update_rule_empty_or_foreign_id_keeps_identity():
    """更新载荷带空 id / 异 id 不得覆盖规则身份。"""
    store = make_store()
    eng = RuleEngine(store)
    created = eng.create_rule({"id": "", "name": "A"})
    rid = created["id"]
    updated = eng.update_rule(rid, {"id": "", "name": "B"})
    assert updated["id"] == rid
    updated = eng.update_rule(rid, {"id": "r_hacked", "name": "C"})
    assert updated["id"] == rid
    assert eng.list_()[0]["name"] == "C"
    assert len(eng.list_()) == 1  # 不产生重复


def test_legacy_empty_id_rule_delete_self_heals():
    """存量空 id 规则：列表已补真实 id，按列表 id 删除成功并写回自愈。"""
    store = make_store()
    store.save({**store.load(), "rules": [{"id": "", "name": "旧规则"}]})
    eng = RuleEngine(store)
    listed = eng.list_()
    assert len(listed) == 1 and listed[0]["id"].startswith("r_")
    assert eng.delete(listed[0]["id"]) is True
    assert store.get_rules() == []
    # 写回自愈：删除后剩余规则被补 id
    store.save({**store.load(), "rules": [{"id": "", "name": "a"}, {"id": "r_ok", "name": "b"}]})
    eng2 = RuleEngine(store)
    listed2 = eng2.list_()
    ids = {r["id"] for r in listed2}
    assert "r_ok" in ids
    assert eng2.delete("r_ok") is True
    assert store.get_rules()[0]["id"].startswith("r_")  # 存量空 id 已被补上


def test_legacy_empty_id_rule_duplicate_works():
    """存量空 id 规则可被复制（新规则获得新 id，源规则保留）。"""
    store = make_store()
    store.save({**store.load(), "rules": [{"id": "", "name": "旧"}]})
    eng = RuleEngine(store)
    src_id = eng.list_()[0]["id"]
    cloned = eng.duplicate(src_id)
    assert cloned["id"].startswith("r_") and cloned["id"] != src_id
    assert len(eng.list_()) == 2


# ------------------------------------------------------------------ #
# temporal
# ------------------------------------------------------------------ #


def test_temporal_create_with_empty_id_generates():
    """temporal 新建（id=""）→ 落库 id 为生成的真实 id（Agent spec 可能携带空 id）。"""
    store = make_store()
    eng = TemporalEngine(store)
    created = eng.create(_valid_temporal())
    assert created["id"].startswith("t_")
    assert store.get_temporal_rules()[0]["id"] == created["id"]


def test_temporal_update_empty_or_foreign_id_keeps_identity():
    """temporal 更新载荷带空/异 id 不得覆盖规则身份。"""
    store = make_store()
    eng = TemporalEngine(store)
    created = eng.create(_valid_temporal())
    rid = created["id"]
    updated = eng.update_rule(rid, {"id": "", "name": "B"})
    assert updated["id"] == rid
    updated = eng.update_rule(rid, {"id": "t_hacked", "name": "C"})
    assert updated["id"] == rid
    assert len(eng.list_()) == 1
    assert eng.get(rid)["name"] == "C"


def test_temporal_legacy_empty_id_delete_and_toggle_self_heal():
    """存量空 id temporal 规则：按列表 id 删除 / 启停均可用。"""
    store = make_store()
    store.save({**store.load(), "temporal_rules": [_valid_temporal(), _valid_temporal(id="t_ok")]})
    eng = TemporalEngine(store)
    listed = eng.list_()
    # 第一条存量空 id 已被补 id
    r0, r1 = listed
    assert r0["id"].startswith("t_") and r0["id"] != "t_ok"
    assert r1["id"] == "t_ok"
    assert eng.toggle(r0["id"], False)["enabled"] is False
    assert eng.delete(r0["id"]) is True
    assert eng.delete(r1["id"]) is True
    assert store.get_temporal_rules() == []


# ------------------------------------------------------------------ #
# groups / lifecycle
# ------------------------------------------------------------------ #


def test_group_update_keeps_identity_when_payload_id_empty():
    """groups.update_group 的 raw 带空 id 时组身份不变（旧实现会生成新 id 换名）。"""
    store = make_store()
    mgr = ModelGroupManager(store)
    created = mgr.create({"id": "g_a", "name": "A", "providers": [{"provider_id": "p1"}]})
    assert created["id"] == "g_a"
    updated = mgr.update_group("g_a", {"id": "", "name": "B"})
    assert updated["id"] == "g_a"
    assert mgr.get("g_a")["name"] == "B"
    assert mgr.get("g_b") is None  # 没有凭空产生新组


def test_group_update_foreign_id_keeps_identity():
    """groups.update_group 的 raw 带异 id 时组身份仍以参数为准。"""
    store = make_store()
    mgr = ModelGroupManager(store)
    mgr.create({"id": "g_a", "name": "A", "providers": [{"provider_id": "p1"}]})
    updated = mgr.update_group("g_a", {"id": "g_other", "name": "B"})
    assert updated["id"] == "g_a"
    assert len(mgr.list_()) == 1


def test_group_legacy_empty_id_delete_self_heals():
    """存量空 id 模型组：按列表 id 删除成功。"""
    store = make_store()
    store.save({**store.load(), "groups": [{"id": "", "name": "旧组"}]})
    mgr = ModelGroupManager(store)
    gid = mgr.list_()[0]["id"]
    assert gid.startswith("g_")
    assert mgr.delete(gid) is True
    assert store.get_groups() == []


def test_lifecycle_update_keeps_identity_when_payload_id_empty():
    """lifecycle.update 的 raw 带空 id 时身份不变（旧实现会生成新 id 换名）。"""
    store = make_store()
    lc = LifecycleEngine(store)
    created = lc.create({"id": "lc_a", "name": "A"})
    assert created["id"] == "lc_a"
    updated = lc.update("lc_a", {"id": "", "name": "B"})
    assert updated["id"] == "lc_a"
    assert lc.get("lc_a")["name"] == "B"
    assert lc.get("lc_b") is None


def test_lifecycle_update_foreign_id_keeps_identity():
    """lifecycle.update 的 raw 带异 id 时身份仍以参数为准。"""
    store = make_store()
    lc = LifecycleEngine(store)
    lc.create({"id": "lc_a", "name": "A"})
    updated = lc.update("lc_a", {"id": "lc_other", "name": "B"})
    assert updated["id"] == "lc_a"
    assert len(lc.list_()) == 1


def test_lifecycle_legacy_empty_id_delete_self_heals():
    """存量空 id 生命周期：按列表 id 删除成功。"""
    store = make_store()
    store.save({**store.load(), "lifecycles": [{"id": "", "name": "旧"}]})
    lc = LifecycleEngine(store)
    lid = lc.list_()[0]["id"]
    assert lid.startswith("lc_")
    assert lc.delete(lid) is True
    assert store.get_lifecycles() == []


# ------------------------------------------------------------------ #
# web 层端到端（报告场景：WebUI 建 → 删）
# ------------------------------------------------------------------ #

_AB_ROOT = None
for _d in Path(__file__).resolve().parents:
    if (_d / "astrbot").is_dir():
        _AB_ROOT = _d
        break


if _AB_ROOT is not None:
    sys.path.insert(0, str(_AB_ROOT))

    from astrbot.api.web import bind_request_context  # noqa: E402

    from data.plugins.astrbot_plugin_model_morph.web import api as web_api  # noqa: E402
    from data.plugins.astrbot_plugin_model_morph.scheduler.audit import AuditLog  # noqa: E402

    class _FakeReq:
        def __init__(self, payload):
            self._payload = payload

        async def json(self, default=None):
            return self._payload if self._payload is not None else default

    class _FakePlugin:
        def __init__(self):
            self.store = make_store()
            self.rules = RuleEngine(self.store)
            self.audit = AuditLog(retention=20)

    def _call(handler, plugin, payload):
        async def _run():
            with bind_request_context(_FakeReq(payload)):
                return await handler(plugin)

        return asyncio.run(_run())

    def _decode(resp):
        return json.loads(resp.body.decode("utf-8", "replace"))

    NEW_RULE = {
        "id": "",
        "name": "测试规则",
        "enabled": True,
        "priority": 0,
        "scope": {"groups": [], "users": [], "sessions": [], "platforms": [],
                  "exclude_groups": [], "exclude_users": []},
        "when": {"op": "and", "conditions": []},
        "then": {"action": "switch_group", "group_id": "g_default"},
    }

    def test_web_e2e_create_list_delete_rule():
        """报告场景端到端：rules/save(id="") → rules GET → rules/delete 不再报无 id。"""
        plugin = _FakePlugin()
        resp = _call(web_api._handler_rules_save, plugin, NEW_RULE)
        body = _decode(resp)
        assert resp.status_code == 200, resp.body
        assert body["id"].startswith("r_")

        resp = _call(web_api._handler_rules_list, plugin, {})
        listed = _decode(resp)
        assert resp.status_code == 200
        assert [r["id"] for r in listed] == [body["id"]]

        resp = _call(web_api._handler_rules_delete, plugin, {"id": body["id"]})
        assert resp.status_code == 200, resp.body
        assert _decode(resp)["deleted"] is True

        remaining = plugin.store.get_rules()
        assert remaining == []

    def test_web_e2e_edit_created_rule_no_duplicate():
        """修复后：编辑刚建的规则（其 id 已生成）→ 更新而非新增重复规则。"""
        plugin = _FakePlugin()
        created = _decode(
            _call(web_api._handler_rules_save, plugin, NEW_RULE)
        )
        rid = created["id"]
        payload = dict(NEW_RULE, id=rid, name="改名")
        resp = _call(web_api._handler_rules_save, plugin, payload)
        assert resp.status_code == 200
        assert len(plugin.store.get_rules()) == 1
        assert plugin.store.get_rules()[0]["name"] == "改名"