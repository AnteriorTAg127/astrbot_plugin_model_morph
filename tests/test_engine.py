"""engine 模块测试：base_group、规则动作、锁定、不干预、跳过、reset、round、
fallback、trace、simulate、异常兜底。"""

from conftest import FakeAdapter, make_engine, make_store, meta_f, run


def _setup_engine():
    """返回配置好 store 的 make_engine；默认 settings 与 FakeAdapter。"""
    engine, adapter, store, states, slog = make_engine(make_store())
    return engine, adapter, store, states, slog


def test_base_group_effected():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_strong",
            "name": "Strong",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_strong"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    t = run(engine.resolve(meta_f()))
    assert t.final_group_id == "g_strong"
    assert t.final_provider_id == "prov-b"
    assert t.changed is True
    assert adapter.calls == [("prov-b", "umo:group:1")]


def test_rule_switch_group():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_cheap",
            "name": "Cheap",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-c",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["rules"] = [
        {
            "id": "r_bot",
            "name": "Bot",
            "enabled": True,
            "priority": 10,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {"op": "and", "conditions": [{"type": "at_bot", "value": True}]},
            "then": {"action": "switch_group", "group_id": "g_cheap"},
        },
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    t = run(engine.resolve(meta_f(at_bot=True)))
    assert t.matched_rule is not None
    assert t.final_group_id == "g_cheap"
    assert t.final_provider_id == "prov-c"


def test_rule_switch_provider_direct():
    store = make_store()
    cfg = store.load()
    cfg["rules"] = [
        {
            "id": "r_direct",
            "name": "Direct",
            "enabled": True,
            "priority": 10,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {
                "op": "and",
                "conditions": [{"type": "message_type", "value": "group"}],
            },
            "then": {"action": "switch_provider", "provider_id": "prov-b"},
        },
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "prov-b"
    assert t.changed is True
    assert ("prov-b", "umo:group:1") in adapter.calls


def test_rule_switch_provider_unavailable_calls_log_error():
    store = make_store()
    cfg = store.load()
    cfg["rules"] = [
        {
            "id": "r_gone",
            "name": "Gone",
            "enabled": True,
            "priority": 10,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {"op": "and", "conditions": [{"type": "at_bot", "value": True}]},
            "then": {"action": "switch_provider", "provider_id": "prov-gone"},
        },
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    t = run(engine.resolve(meta_f(at_bot=True)))
    assert t.final_provider_id is None
    assert t.changed is False
    assert adapter.calls == []  # 未切换
    assert any(e.get("type") == "error" for e in slog.to_list())


def test_rule_apply_lifecycle():
    store = make_store()
    cfg = store.load()
    cfg["lifecycles"] = [
        {
            "id": "lc1",
            "name": "New Conv",
            "enabled": True,
            "initial_group": "g_init",
            "initial_rounds": 2,
            "main_group": "g_main",
            "periodic_group": "",
            "periodic_interval": 0,
        },
    ]
    cfg["groups"] = [
        {
            "id": "g_init",
            "name": "Init",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
        {
            "id": "g_main",
            "name": "Main",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-c",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["rules"] = [
        {
            "id": "r_lc",
            "name": "ApplyLifecycle",
            "enabled": True,
            "priority": 10,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {"op": "and", "conditions": [{"type": "at_bot", "value": True}]},
            "then": {"action": "apply_lifecycle", "lifecycle_id": "lc1"},
        },
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    # round=0 时 apply_lifecycle → INITIAL/g_init → prov-b
    t = run(engine.resolve(meta_f(at_bot=True)))
    assert t.final_provider_id == "prov-b"
    assert t.stage == "INITIAL"


def test_unlock_action_clears_lock():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_base",
            "name": "Base",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_base"
    cfg["rules"] = [
        {
            "id": "r_unlock",
            "name": "Unlock",
            "enabled": True,
            "priority": 10,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {"op": "and", "conditions": [{"type": "at_bot", "value": True}]},
            "then": {"action": "unlock"},
        },
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    async def _main():
        # 先锁定到不存在的组（该组 provider 不可用）
        await states.update("umo:group:1", lock_group_id="g_nowhere")
        t = await engine.resolve(meta_f(at_bot=True))
        st = await states.get("umo:group:1")
        return t, st

    t, st = run(_main())
    assert st.lock_group_id is None
    assert st.lock_provider_id is None
    assert t.final_group_id == "g_base"  # 恢复自动 → base_group


def test_lock_priority_over_rule():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_locked",
            "name": "Locked",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
        {
            "id": "g_rule",
            "name": "Rule",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-c",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["rules"] = [
        {
            "id": "r_switch",
            "name": "Switch",
            "enabled": True,
            "priority": 100,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {"op": "and", "conditions": [{"type": "at_bot", "value": True}]},
            "then": {"action": "switch_group", "group_id": "g_rule"},
        },
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    async def _main():
        await states.update("umo:group:1", lock_group_id="g_locked")
        t = await engine.resolve(meta_f(at_bot=True))
        return t

    t = run(_main())
    assert t.final_group_id == "g_locked"  # 锁定优先，忽略规则
    assert t.final_provider_id == "prov-b"


def test_no_intervention():
    engine, adapter, store, states, slog = _setup_engine()  # base_group="", 无规则
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id is None
    assert t.changed is False
    assert t.reason == "inherit native behavior"
    assert adapter.calls == []


def test_disabled_skips():
    store = make_store()
    adapter = FakeAdapter(["prov-a", "prov-b", "prov-c"], enabled=False)
    engine, adapter, store, states, slog = make_engine(store, adapter=adapter)
    t = run(engine.resolve(meta_f()))
    assert t.skipped_reason == "disabled"
    assert adapter.calls == []


def test_third_party_skips():
    engine, adapter, store, states, slog = _setup_engine()
    adapter.local = False
    t = run(engine.resolve(meta_f()))
    assert t.skipped_reason == "third_party_runner"


def test_reset_pending():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_base",
            "name": "Base",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_base"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    async def _main():
        # 先造成 round>0
        await engine.resolve(meta_f())
        await states.update("umo:group:1", stage="MAIN", round=5)
        await states.mark_pending_reset("umo:group:1")
        await engine.resolve(meta_f())
        st = await states.get("umo:group:1")
        return st

    st = run(_main())
    assert st.round == 1  # reset 归 0 后本条消息 +1
    assert st.stage == "NEW"
    assert any(e.get("type") == "reset" for e in slog.to_list())


def test_reset_on_conversation_id_change():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_base",
            "name": "Base",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_base"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    async def _main():
        await states.update(
            "umo:group:1", round=3, conversation_id="conv-old", stage="MAIN"
        )
        await engine.resolve(meta_f())  # adapter.cid = "conv-1" != conv-old → reset
        st = await states.get("umo:group:1")
        logs = [e for e in slog.to_list() if e.get("type") == "reset"]
        return st, logs

    st, logs = run(_main())
    assert st.round == 1
    assert st.conversation_id == "conv-1"
    assert logs and logs[0].get("event") == "new"  # old_round>0 → 新会话事件


def test_round_increments():
    engine, adapter, store, states, slog = _setup_engine()
    run(engine.resolve(meta_f()))
    st = run(states.get("umo:group:1"))
    assert st.round == 1


def test_set_provider_only_on_change():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_a",
            "name": "A",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-a",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_a"
    store.save(cfg)
    # FakeAdapter current 默认 = prov-a（sorted 首个）
    engine, adapter, store, states, slog = make_engine(store)
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "prov-a"
    assert t.changed is False
    assert adapter.calls == []  # 已是目标 → 不调用 set_provider


def test_fallback_to_fallbacks():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_fall",
            "name": "Fall",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": True,
            "fallbacks": ["prov-c"],
            "providers": [
                {
                    "provider_id": "prov-a",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                },
                {
                    "provider_id": "prov-b",
                    "priority": 2,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                },
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_fall"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)
    adapter._ids = {"prov-c"}  # 组内成员都不在可用集合，fallback 到 prov-c
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "prov-c"
    assert t.final_group_id == "g_fall"


def test_trace_fields_complete():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_base",
            "name": "Base",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_base"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)
    t = run(engine.resolve(meta_f()))
    d = t.to_dict()
    for key in (
        "umo",
        "changed",
        "final_provider_id",
        "final_group_id",
        "stage",
        "matched_rule",
        "rejected_rules",
        "condition_results",
        "reason",
        "elapsed_ms",
        "skipped_reason",
    ):
        assert key in d


def test_simulate_does_not_pollute_real_state():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_base",
            "name": "Base",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-b",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g_base"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)
    run(engine.resolve(meta_f()))
    before_snap = states.snapshot()
    before_calls = list(adapter.calls)

    sim = run(
        engine.simulate(
            {
                "time_iso": "2026-06-10T02:00:00",
                "group_id": "12345",
                "sender_id": "user1",
                "umo": "umo:group:1",
                "round": 5,
                "lifecycle_event": "",
                "message_str": "hi",
                "message_type": "group",
                "at_bot": False,
            }
        )
    )
    assert isinstance(sim, dict)
    # 真实状态与切换调用不受影响
    assert states.snapshot() == before_snap
    assert adapter.calls == before_calls
    # 且 simulate 不写真实 round
    st = run(states.get("umo:group:1"))
    assert st.round == 1  # 仅真实 resolve 曾 +1


def test_exception_adapter_returns_error():
    engine, adapter, store, states, slog = _setup_engine()
    adapter.fail_on = {"provider_ids"}
    t = run(engine.resolve(meta_f()))
    assert t.skipped_reason == "error"
    assert not t.changed


def test_smoke_integration():
    """吸收主代理冒烟脚本关键断言。"""
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g_strong",
            "name": "Strong",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": True,
            "fallbacks": ["prov-c"],
            "providers": [
                {
                    "provider_id": "prov-a",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                },
                {
                    "provider_id": "prov-b",
                    "priority": 2,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                },
            ],
        },
        {
            "id": "g_cheap",
            "name": "Cheap",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "prov-c",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["rules"] = [
        {
            "id": "r_night",
            "name": "Night",
            "enabled": True,
            "priority": 100,
            "scope": {
                "groups": [],
                "users": [],
                "sessions": [],
                "platforms": [],
                "exclude_groups": [],
                "exclude_users": [],
            },
            "when": {
                "op": "and",
                "conditions": [
                    {
                        "type": "time_range",
                        "start": "00:00",
                        "end": "12:00",
                        "weekdays": [],
                    }
                ],
            },
            "then": {"action": "switch_group", "group_id": "g_cheap"},
        },
    ]
    cfg["settings"]["base_group"] = "g_strong"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store)

    # 第 1 轮：14:00 → Night 不命中 → base g_strong → prov-a（当前即 prov-a，不切换）
    t1 = run(engine.resolve(meta_f()))
    assert t1.final_group_id == "g_strong"
    assert t1.final_provider_id == "prov-a"

    # 会话隔离：第二个 umo 独立计数
    run(engine.resolve(meta_f(umo="umo:group:2")))
    s1, s2 = run(states.get("umo:group:1")), run(states.get("umo:group:2"))
    assert s1.round == 1 and s2.round == 1

    # reset 检测：pending_reset → round 归零再 +1
    run(states.mark_pending_reset("umo:group:1"))
    run(engine.resolve(meta_f()))
    s1 = run(states.get("umo:group:1"))
    assert s1.round == 1 and s1.stage == "NEW"

    # fallback：g_strong 成员不可用 → fallback prov-c
    adapter._ids = {"prov-c"}
    t4 = run(engine.resolve(meta_f(umo="umo:group:3")))
    assert t4.final_provider_id == "prov-c"
