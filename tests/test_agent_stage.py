"""v1.0.3：Agent 工具分级审批 + 规则引擎 CRUD 工具 + summary 生成测试（模块 T）。

覆盖：
- ``_HIGH_RISK_TOOLS`` 分级归类：高危写（删除/修改/规则 CRUD）在 agent_confirm=True 时一律
  暂存（C2 格式 status=staged），agent_confirm=False 时直接执行；
  低风险写（create_* / enable / disable）直接执行。
- 暂存格式（C2）与「config 未被修改」断言。
- 校验失败不暂存（validate 返回错误且 agent_confirm=True → ok:false，pending 为空）。
- 规则引擎工具 list / get / create / update / delete / toggle（含 model_keyword /
  replace_model 语义）。
- ``apply_staged`` / ``reject_staged`` / ``pending_view`` 行为与审计写入。
- ``PendingChangeStore.stage`` 扩展：summary / staged_at 落盘。
- ``_summarize_ops`` 文案（新建/删除/修改 + model_keyword/replace_model）。

reuse test_agent_tools 的既有点固定 test-fixture 替身（_make_tc / _seed_group / _schedule /
FakeTemporal / FakeLifecycles），避免重复维护。
"""

from test_agent_tools import (
    _lc_spec,
    _make_lifecycles_tc,
    _make_tc,
    _schedule,
    _seed_group,
    _seed_groups_for_lifecycle,
    _stage_spec,
)
from scheduler.agent_tools import (
    _HIGH_RISK_TOOLS,
    _summarize_ops,
    apply_staged,
    pending_view,
    reject_staged,
    tool_create_lifecycle,
    tool_create_model_group,
    tool_create_rule,
    tool_delete_model_group,
    tool_delete_rule,
    tool_disable_schedule_rule,
    tool_enable_schedule_rule,
    tool_get_rule,
    tool_list_rules,
    tool_toggle_rule,
    tool_update_lifecycle,
    tool_update_model_group,
    tool_update_rule,
    tool_update_schedule_rule,
)


# ---- 工具常量：既有高危 / 低风险工具集合 ----

_LOW_RISK_WRITE_TOOLS = {
    "tool_create_model_group",
    "tool_create_schedule_rule",
    "tool_create_lifecycle",
    "tool_enable_schedule_rule",
    "tool_disable_schedule_rule",
}


def _rule_spec(**kw) -> dict:
    """合法规则 spec（model_keyword 条件 + replace_model 动作，provider 用 deepseek）。"""
    base = {
        "name": "测试规则",
        "enabled": True,
        "priority": 50,
        "when": {
            "op": "and",
            "conditions": [
                {
                    "type": "model_keyword",
                    "keywords": ["flash", "turbo"],
                    "mode": "any",
                }
            ],
        },
        "then": {
            "action": "replace_model",
            "provider_id": "deepseek",
            "model": "deepseek-reasoner",
        },
    }
    base.update(kw)
    return base


# ---- 分级归类：高危暂存 / 低风险直执 ----


def test_high_risk_tools_classified():
    """_HIGH_RISK_TOOLS 含删除/修改/规则 CRUD；不含创建与启停。"""
    assert {
        "tool_create_rule",
        "tool_update_rule",
        "tool_delete_rule",
        "tool_toggle_rule",
        "tool_update_model_group",
        "tool_delete_model_group",
        "tool_update_schedule_rule",
        "tool_delete_schedule_rule",
        "tool_update_model_override",
        "tool_delete_model_override",
        "tool_update_lifecycle",
        "tool_delete_lifecycle",
    } <= _HIGH_RISK_TOOLS
    assert not (_HIGH_RISK_TOOLS & _LOW_RISK_WRITE_TOOLS)


def test_stage_approval_hint_differs_by_source():
    """approval_hint 按来源区分：SubAgent 提示聊天指令，Web 助手提示页面按钮。"""
    # SubAgent 场景（_make_tc 默认 source=subagent）
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    r = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    assert r["ok"] is True and r["status"] == "staged"
    hint = r.get("approval_hint", "")
    assert hint
    assert "/scheduler approve" in hint
    assert r["pending_id"] in hint
    # Web 场景：提示点击页面「批准」按钮，不引导聊天指令
    tc2, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc2)
    tc2.source = "web_agent"
    r2 = tool_update_model_group(
        tc2,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    assert r2["ok"] is True and r2["status"] == "staged"
    hint2 = r2.get("approval_hint", "")
    assert hint2
    assert "『批准』按钮" in hint2
    assert "/scheduler approve" not in hint2


def test_update_model_group_stages_when_confirm():
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    r = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    assert r["ok"] is True and r["status"] == "staged"
    assert r["pending_id"].startswith("p_")
    assert r["summary"]
    # config 未被修改：组内仍是旧成员
    cur = next(g for g in tc.groups_() if g["id"] == "default-chat")
    assert all(e["provider_id"] != "qwen" for e in cur.get("providers", []))


def test_update_schedule_rule_stages_when_confirm():
    tc, _, temporal = _make_tc(agent_confirm=True)
    rule = temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    r = tool_update_schedule_rule(
        tc, rule_id=rule["id"], spec={"target_provider": "qwen"}
    )
    assert r["ok"] is True and r["status"] == "staged"
    # 未生效
    assert temporal.get(rule["id"])["target_provider"] == "cheap"


def test_update_lifecycle_stages_when_confirm():
    tc, _, fs = _make_lifecycles_tc(agent_confirm=True)
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    r = tool_update_lifecycle(tc, lifecycle_id=created["id"], spec={"name": "改后"})
    assert r["ok"] is True and r["status"] == "staged"
    assert fs.get(created["id"])["name"] == "降级预设"  # 未生效


def test_low_risk_create_executes_directly():
    tc, _, _ = _make_tc()
    r = tool_create_model_group(
        tc, spec={"name": "好组", "providers": [{"provider_id": "qwen"}]}
    )
    assert r["ok"] is True and r.get("status", "") != "staged"
    assert any(g["name"] == "好组" for g in tc.groups_())


def test_low_risk_enable_disable_executes_directly():
    tc, _, temporal = _make_tc()
    rule = temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    off = tool_disable_schedule_rule(tc, rule_id=rule["id"])
    assert off["ok"] is True and off["rule"]["enabled"] is False
    on = tool_enable_schedule_rule(tc, rule_id=rule["id"])
    assert on["ok"] is True and on["rule"]["enabled"] is True


# ---- staged 返回格式（C2）----


def test_stage_return_format_c2():
    tc, store, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    before_groups = len(tc.store.load().get("groups", []))
    r = tool_delete_model_group(tc, group_id="default-chat")
    assert r["ok"] is True
    assert r["status"] == "staged"
    assert r["pending_id"].startswith("p_")
    assert isinstance(r["summary"], list) and len(r["summary"]) == 1
    # config 未被修改
    assert len(tc.store.load().get("groups", [])) == before_groups
    assert tc.groups.get("default-chat") is not None


def test_apply_staged_applies_and_audits():
    tc, store, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    r = tool_delete_model_group(tc, group_id="default-chat")
    pid = r["pending_id"]
    ap = apply_staged(tc, pid)
    assert ap["ok"] is True and ap["applied"] == 1
    assert ap["summary"] == r["summary"]
    assert tc.groups.get("default-chat") is None
    assert tc.pending.get() is None
    # 审计 approve
    assert any(
        e["action"] == "approve" and e["target"] == pid for e in tc.audit.recent()
    )
    # 再次应用：无暂存
    assert apply_staged(tc, pid)["ok"] is False


def test_apply_staged_id_mismatch_fails():
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    tool_delete_model_group(tc, group_id="default-chat")
    r = apply_staged(tc, "p_wrongid")
    assert r["ok"] is False and "不匹配" in r["error"]


def test_reject_staged_discards_and_audits():
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    r = tool_delete_model_group(tc, group_id="default-chat")
    pid = r["pending_id"]
    rg = reject_staged(tc, pid)
    assert rg["ok"] is True and rg["discarded"] is True
    assert tc.pending.get() is None
    assert tc.groups.get("default-chat") is not None  # 未删除
    assert any(
        e["action"] == "reject" and e["target"] == pid for e in tc.audit.recent()
    )
    assert reject_staged(tc, pid)["ok"] is False  # 无暂存


def test_pending_view_returns_entry():
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    assert pending_view(tc) is None
    r = tool_delete_model_group(tc, group_id="default-chat")
    view = pending_view(tc)
    assert view is not None
    assert view["pending_id"] == r["pending_id"]
    assert view["summary"] == r["summary"]
    assert view["staged_at"]
    assert view["ops"][0]["action"] == "delete_model_group"


# ---- 校验失败不暂存 ----


def test_rule_validation_failure_not_staged():
    tc, _, _ = _make_tc(agent_confirm=True)
    # replace_model 指向不存在的 provider → 校验失败
    r = tool_create_rule(
        tc,
        spec=_rule_spec(
            then={"action": "replace_model", "provider_id": "ghost", "model": "m"}
        ),
    )
    assert r["ok"] is False
    assert "provider" in r["error"] or "不存在" in r["error"]
    assert tc.pending.get() is None
    assert tc.rules_() == []


def test_rule_validation_failure_model_keyword_bad():
    tc, _, _ = _make_tc()
    # model_keyword 非法 mode
    r = tool_create_rule(
        tc,
        spec=_rule_spec(
            when={
                "op": "and",
                "conditions": [
                    {"type": "model_keyword", "keywords": ["a"], "mode": "bogus"}
                ],
            }
        ),
    )
    assert r["ok"] is False
    assert tc.pending.get() is None


def test_delete_missing_target_fails_not_staged():
    tc, _, _ = _make_tc(agent_confirm=True)
    assert tool_delete_model_group(tc, group_id="nope")["ok"] is False
    assert tc.pending.get() is None


# ---- agent_confirm=False：高危直执 ----


def test_high_risk_direct_exec_when_no_confirm():
    tc, _, _ = _make_tc(agent_confirm=False)
    _seed_group(tc)
    r = tool_delete_model_group(tc, group_id="default-chat")
    assert r["ok"] is True and r.get("deleted") is True
    assert tc.groups.get("default-chat") is None
    assert tc.pending.get() is None


def test_rule_write_stages_when_confirm_direct_when_not():
    # confirm=True → staged
    tc, store, _ = _make_tc(agent_confirm=True)
    st = tool_create_rule(tc, spec=_rule_spec())
    assert st["ok"] is True and st["status"] == "staged"
    assert tc.rules_() == []  # 未写入

    # confirm=False → 直接执行
    tc2, _, _ = _make_tc(agent_confirm=False)
    direct = tool_create_rule(tc2, spec=_rule_spec())
    assert direct["ok"] is True
    assert len(tc2.rules_()) == 1


# ---- 规则引擎工具：list / get / CRUD / toggle ----


def test_create_apply_delete_rule_roundtrip():
    tc, _, _ = _make_tc(agent_confirm=True)
    st = tool_create_rule(tc, spec=_rule_spec())
    assert st["status"] == "staged"
    # 批准后真正创建
    ap = apply_staged(tc, st["pending_id"])
    assert ap["ok"] is True and ap["applied"] == 1
    rules = tc.rules_()
    assert len(rules) == 1
    rule = rules[0]
    assert rule["when"]["conditions"][0]["type"] == "model_keyword"
    assert rule["then"]["action"] == "replace_model"

    # get_rule
    got = tool_get_rule(tc, rule_id=rule["id"])
    assert got["ok"] is True and got["rule"]["id"] == rule["id"]
    miss = tool_get_rule(tc, rule_id="nope")
    assert miss["ok"] is False and "未找到" in miss["error"]

    # list_rules 含该规则
    listed = tool_list_rules(tc)
    assert listed["ok"] is True
    assert any(r["id"] == rule["id"] for r in listed["rules"])

    # delete_rule → 暂存 → 批准后删除
    d = tool_delete_rule(tc, rule_id=rule["id"])
    assert d["status"] == "staged"
    assert any(r["id"] == rule["id"] for r in tc.rules_())  # 未删
    assert apply_staged(tc, d["pending_id"])["applied"] == 1
    assert tc.rules_() == []


def test_update_rule_stages_and_apply():
    tc, _, _ = _make_tc(agent_confirm=True)
    st = tool_create_rule(tc, spec=_rule_spec())
    apply_staged(tc, st["pending_id"])
    rule_id = tc.rules_()[0]["id"]
    up = tool_update_rule(tc, rule_id=rule_id, spec={"priority": 99})
    assert up["status"] == "staged"
    assert tc.rules_()[0]["priority"] == 50  # 未生效
    apply_staged(tc, up["pending_id"])
    assert tc.rules_()[0]["priority"] == 99


def test_toggle_rule_flips_enabled():
    tc, _, _ = _make_tc(agent_confirm=True)
    st = tool_create_rule(tc, spec=_rule_spec())
    apply_staged(tc, st["pending_id"])
    rule_id = tc.rules_()[0]["id"]
    t = tool_toggle_rule(tc, rule_id=rule_id)
    assert t["status"] == "staged"
    assert tc.rules_()[0]["enabled"] is True  # 未生效
    apply_staged(tc, t["pending_id"])
    assert tc.rules_()[0]["enabled"] is False


def test_rule_tools_direct_when_no_confirm_apply():
    tc, _, _ = _make_tc(agent_confirm=False)
    st = tool_create_rule(tc, spec=_rule_spec())
    assert st["ok"] is True and st.get("status") != "staged"
    rule_id = tc.rules_()[0]["id"]
    d = tool_delete_rule(tc, rule_id=rule_id)
    assert d["ok"] is True and tc.rules_() == []


# ---- summary 生成文案 ----


def test_summarize_create_delete_update_group():
    ops = [
        {
            "action": "create_model_group",
            "data": {
                "name": "夜间省钱",
                "providers": [{"provider_id": "a"}, {"provider_id": "b"}],
            },
        },
        {
            "action": "delete_model_group",
            "group_id": "test_group",
            "data": {
                "name": "test_group",
                "providers": [{"provider_id": "a"}, {"provider_id": "b"}],
            },
        },
        {
            "action": "update_model_group",
            "group_id": "main",
            "data": {"name": "main"},
        },
    ]
    lines = _summarize_ops(ops)
    assert len(lines) == 3
    assert "新建模型组「夜间省钱」（含 2 个成员）" in lines[0]
    assert "删除模型组「test_group」（含 2 个成员，不可恢复）" in lines[1]
    assert "修改模型组「main」" in lines[2]


def test_summarize_temporal_rule_period():
    ops = [
        {
            "action": "create_schedule_rule",
            "data": {
                "name": "夜间省钱",
                "kind": "model_override",
                "group_id": "cheap",
                "source_provider": "deepseek-chat",
                "target_provider": "gpt-5-mini",
                "schedule": {"type": "daily", "start": "23:00", "end": "07:00"},
                "priority": 200,
            },
        }
    ]
    line = _summarize_ops(ops)[0]
    assert "新建时间规则「夜间省钱」" in line
    assert "每天 23:00-07:00（跨午夜）" in line
    assert "deepseek-chat" in line and "gpt-5-mini" in line


def test_summarize_rule_model_keyword_replace_model():
    ops = [
        {
            "action": "create_rule",
            "data": {
                "name": "高峰期",
                "priority": 10,
                "when": {
                    "op": "and",
                    "conditions": [
                        {
                            "type": "model_keyword",
                            "keywords": ["flash", "turbo"],
                            "mode": "any",
                        }
                    ],
                },
                "then": {
                    "action": "replace_model",
                    "provider_id": "openai",
                    "model": "gpt-5-mini",
                },
            },
        },
        {
            "action": "delete_rule",
            "rule_id": "r_abc",
            "data": {},
        },
    ]
    lines = _summarize_ops(ops)
    assert "新建条件规则「高峰期」" in lines[0]
    assert "模型名含 [flash, turbo]（任意 1 个）" in lines[0]
    assert "替换为 openai @ gpt-5-mini" in lines[0]
    assert "删除条件规则「r_abc」（不可恢复）" in lines[1]


def test_summarize_lifecycle():
    ops = [
        {
            "action": "create_lifecycle",
            "data": {
                "name": "降级预设",
                "stages": [_stage_spec("g_main", 4), _stage_spec("g_cal", 5)],
                "final_group": "g_main",
            },
        }
    ]
    line = _summarize_ops(ops)[0]
    assert "新建生命周期「降级预设」" in line
    assert "共 2 个阶段" in line and "最终组 g_main" in line


# ---- PendingChangeStore.stage 扩展（summary / staged_at）----


def test_pending_store_stage_summary_and_staged_at():
    from scheduler.agent_tools import PendingChangeStore
    from conftest import make_store

    store = make_store()
    ps = PendingChangeStore(store.config_path().parent)
    pid = ps.stage(
        [{"action": "create_model_group", "data": {"name": "x"}}],
        {"groups": []},
        summary=["新建模型组「x」"],
    )
    entry = ps.get()
    assert entry["pending_id"] == pid
    assert entry["summary"] == ["新建模型组「x」"]
    assert entry["staged_at"]
    # 旧调用（不传 summary）仍兼容：summary 默认 []
    ps2 = PendingChangeStore(store.config_path().parent)
    ps2.stage([{"action": "create_model_group", "data": {}}], {"groups": []})
    assert ps2.get()["summary"] == []
    assert ps2.get()["staged_at"]


# ---- G-2：暂存覆盖 / 同批合并语义（v1.0.3 修复） ----
# _stage_op 依据 tc.staging_batch 与旧 pending 的 staging_batch 是否同批判定：
# - 同批（相同 batch 且非 None）：合并追加 op 与 summary，保留唯一 pending，不写 stale；
# - 跨批 / 首次：覆盖旧 pending 并写 stale 审计。
# 注意：_make_tc 默认不设 staging_batch（None），因此未显式设 batch 的调用一律视为跨批覆盖。
# G-2 原用例（同一 tc 连续两次高危写 → 覆盖 + stale）在 batch=None 下语义不变；
# 新增同批合并用例（显式设相同 batch）验证修复后的行为。


def test_stale_audit_when_pending_overwritten():
    """G-2：跨批（batch=None）同一 tc 连续两次高危写 → 第二次覆盖旧 pending：
    pending 仅剩 1 份且 pending_id 为新值；audit 含 stale（旧 id → 新 id）与 stage 两条。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    first = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    old_pid = first["pending_id"]
    second = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "cheap", "priority": 2, "enabled": True}]},
    )
    new_pid = second["pending_id"]
    assert old_pid != new_pid

    # pending 只剩 1 份，且为新的 pending_id。
    pending = tc.pending.get()
    assert pending is not None
    assert pending["pending_id"] == new_pid

    entries = tc.audit.to_list()
    stage_entries = [e for e in entries if e["action"] == "stage"]
    assert len(stage_entries) == 2  # 两次高危写各写一条 stage

    # stale 审计：target=旧 pending_id，after=新 pending_id。
    stale = [e for e in entries if e["action"] == "stale"]
    assert len(stale) == 1
    assert stale[0]["target"] == old_pid
    assert stale[0]["after"] == new_pid
    assert "覆盖" in stale[0]["detail"]


def test_same_batch_merge_pending_ops_and_no_stale():
    """同批合并：显式设相同 staging_batch 的连续两次高危写 → pending 含 2 ops + 2 summary，
    不写 stale 审计（同轮合并不是覆盖）。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    tc.staging_batch = "sub_testbatch"
    r1 = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    r2 = tool_delete_model_group(tc, group_id="default-chat")
    assert r1["ok"] is True and r2["ok"] is True
    assert r1["status"] == "staged" and r2["status"] == "staged"

    # 同批 → 第二次返回的 summary 是合并后的全部（2 条），文档文案见下。
    assert len(r1["summary"]) == 1
    assert len(r2["summary"]) == 2

    # pending 含 2 ops + 2 summary，且唯一（pending_id 一致）。
    pending = tc.pending.get()
    assert pending is not None
    assert len(pending["ops"]) == 2
    assert len(pending["summary"]) == 2
    assert pending["staging_batch"] == "sub_testbatch"
    assert pending["ops"][0]["action"] == "update_model_group"
    assert pending["ops"][1]["action"] == "delete_model_group"
    assert "修改模型组「default-chat」" in pending["summary"][0]
    assert "删除模型组" in pending["summary"][1]

    # 不写 stale 审计（同轮合并不是覆盖）；stage 审计有 2 条。
    entries = tc.audit.to_list()
    assert not [e for e in entries if e["action"] == "stale"]
    assert len([e for e in entries if e["action"] == "stage"]) == 2


def test_same_batch_apply_no_arg_applies_all_ops():
    """同批多次暂存后，apply_staged 无参批准 → 两个 op 都真实生效。"""
    tc, _, temporal = _make_tc(agent_confirm=True)
    _seed_group(tc)
    # 先建规则再删除，使 apply 后既能验证「组更新生效」又能验证「规则删除生效」。
    tc.staging_batch = "sub_batchall"
    st = tool_create_rule(tc, spec=_rule_spec())
    apply_staged(tc, st["pending_id"])
    assert len(tc.rules_()) == 1
    rule_id = tc.rules_()[0]["id"]

    # 同一批内连续两个高危写：更新模型组 + 删除规则。
    up = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    dl = tool_delete_rule(tc, rule_id=rule_id)
    assert up["status"] == "staged" and dl["status"] == "staged"
    assert len(up["summary"]) == 1 and len(dl["summary"]) == 2

    # 未生效：组未改、规则仍在。
    cur = next(g for g in tc.groups_() if g["id"] == "default-chat")
    assert all(e["provider_id"] != "qwen" for e in cur.get("providers", []))
    assert len(tc.rules_()) == 1

    # 无参批准 → 两个 op 都生效。
    ap = apply_staged(tc)
    assert ap["ok"] is True and ap["applied"] == 2
    assert len(ap["summary"]) == 2
    cur = next(g for g in tc.groups_() if g["id"] == "default-chat")
    assert any(e["provider_id"] == "qwen" for e in cur.get("providers", []))
    assert tc.rules_() == []
    assert tc.pending.get() is None


def test_cross_batch_approve_old_id_fails_mismatch():
    """跨批批准旧 id → 明确「不匹配」错误且不生效。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    # 批次 A：暂存更新 → 拿到旧 pending_id。
    tc.staging_batch = "sub_batchA"
    r1 = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    old_pid = r1["pending_id"]
    # 批次 B（跨批）：再暂存 → 覆盖旧 pending。
    tc.staging_batch = "sub_batchB"
    r2 = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "cheap", "priority": 2, "enabled": True}]},
    )
    assert r2["pending_id"] != old_pid
    # 批准旧 id → id 不匹配，且不生效。
    ap = apply_staged(tc, old_pid)
    assert ap["ok"] is False and "不匹配" in ap["error"]
    assert "无参批准" in ap["error"]
    # qwen 更新（批次 A）未应用：组内仍不含 qwen。
    cur = next(g for g in tc.groups_() if g["id"] == "default-chat")
    assert all(e["provider_id"] != "qwen" for e in cur.get("providers", []))
    # 审核失败不清理 pending：批次 B 的暂存仍在（未被吞掉、未覆盖应用）。
    assert tc.pending.get() is not None
    assert tc.pending.get()["pending_id"] == r2["pending_id"]
    assert len(tc.pending.get()["ops"]) == 1


def test_pending_view_and_store_contain_staging_batch():
    """pending_view / PendingChangeStore 条目含 staging_batch 字段；旧数据（无该键）读取兼容。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    tc.staging_batch = "web_batchview"
    r = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    view = pending_view(tc)
    assert view is not None
    assert view["staging_batch"] == "web_batchview"
    assert view["pending_id"] == r["pending_id"]

    # 存库条目含 staging_batch。
    entry = tc.pending.get()
    assert entry["staging_batch"] == "web_batchview"

    # 旧数据兼容：不带 staging_batch 调用 stage 写入的条目无该键，读取 get 返回 None。
    from conftest import make_store
    from scheduler.agent_tools import PendingChangeStore

    store = make_store()
    ps = PendingChangeStore(store.config_path().parent)
    ps.stage([{"action": "create_model_group", "data": {"name": "x"}}], {"groups": []})
    legacy = ps.get()
    assert legacy is not None and "staging_batch" not in legacy
    assert legacy.get("staging_batch") is None


# ---- G-4：_validate_rule_spec 的 ImportError / 异常回退防御 ----
# 实现：优先委托 rules.validate_rule；其抛 ImportError/AttributeError 或其它异常时
# 回退兜底校验（不抛错），再执行工具侧的 replace_model 补充校验。


def test_validate_rule_spec_import_error_fallback(monkeypatch):
    """G-4：rules.validate_rule 抛 ImportError（模拟未就绪）→ 兜底校验不抛异常，
    合法 spec 仍走暂存（ok/staged）。"""
    import scheduler.rules as rules_mod

    def _boom():
        raise ImportError("validate_rule 未就绪（模拟 Agent-R 未交付）")

    monkeypatch.setattr(rules_mod, "validate_rule", _boom)
    tc, _, _ = _make_tc(agent_confirm=True)
    r = tool_create_rule(tc, spec=_rule_spec())
    assert r["ok"] is True and r["status"] == "staged"


def test_validate_rule_spec_generic_exception_fallback(monkeypatch):
    """G-4 变体：validate_rule 抛普通异常（校验内部故障）→ 同走兜底，不抛错、合法 spec 可暂存。"""
    import scheduler.rules as rules_mod

    def _boom():
        raise RuntimeError("validate_rule 内部异常")

    monkeypatch.setattr(rules_mod, "validate_rule", _boom)
    tc, _, _ = _make_tc(agent_confirm=True)
    r = tool_create_rule(tc, spec=_rule_spec())
    assert r["ok"] is True and r["status"] == "staged"
