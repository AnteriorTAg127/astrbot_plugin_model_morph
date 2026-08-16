"""scheduler/umo.py 的单元测试（纯逻辑，不依赖 astrbot）。

覆盖 parse_umo（合法/非法/白名单/大小写/空字段）、format_umo、umo_examples、
umo_from_ids。导入方式依赖 conftest 把插件根目录加入 sys.path。
"""

import pytest

from scheduler import umo

M = umo.MESSAGE_TYPES


# ---------- parse_umo：合法 ----------
def test_parse_umo_basic_group():
    assert umo.parse_umo("aiocqhttp:GroupMessage:123456") == (
        "aiocqhttp",
        "GroupMessage",
        "123456",
    )


def test_parse_umo_friend():
    assert umo.parse_umo("aiocqhttp:FriendMessage:987654") == (
        "aiocqhttp",
        "FriendMessage",
        "987654",
    )


def test_parse_umo_other():
    assert umo.parse_umo("telegram:OtherMessage:user_777") == (
        "telegram",
        "OtherMessage",
        "user_777",
    )


def test_parse_umo_session_contains_no_colon():
    # platform=webchat，session_id=webchat!u!c1（本身不含冒号）。
    assert umo.parse_umo("webchat:GroupMessage:webchat!u!c1") == (
        "webchat",
        "GroupMessage",
        "webchat!u!c1",
    )


def test_parse_umo_session_contains_colon():
    # session_id 允许含冒号：最多 split 成 3 段，其余合并为 session_id。
    assert umo.parse_umo("a:GroupMessage:b:c") == ("a", "GroupMessage", "b:c")


def test_parse_umo_session_contains_multiple_colons():
    assert umo.parse_umo("webchat:FriendMessage:x:y:z") == (
        "webchat",
        "FriendMessage",
        "x:y:z",
    )


# ---------- parse_umo：非法 ----------
@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        123,  # 非字符串
        "只有一个冒号:但少了一段",  # 只有一段
        "aiocqhttp:GroupMessage",  # 缺 session
        "aiocqhttp:groupmessage:1",  # 大小写不符
        "aiocqhttp:GROUPmessage:1",  # 大小写不符（另一形态）
        "aiocqhttp:UnknownType:1",  # 不在白名单
        "aiocqhttp::1",  # 空 message_type
        ":GroupMessage:1",  # 空 platform
        "aiocqhttp:GroupMessage:",  # 空 session
    ],
)
def test_parse_umo_invalid(bad):
    assert umo.parse_umo(bad) is None


# ---------- format_umo ----------
def test_format_umo_concatenation():
    assert umo.format_umo("aiocqhttp", "GroupMessage", "123456") == (
        "aiocqhttp:GroupMessage:123456"
    )


def test_format_umo_session_contains_colon():
    assert umo.format_umo("a", "FriendMessage", "b:c") == "a:FriendMessage:b:c"


def test_format_umo_invalid_message_type():
    assert umo.format_umo("aiocqhttp", "groupmessage", "1") == ""
    assert umo.format_umo("aiocqhttp", "UnknownType", "1") == ""


def test_format_umo_empty_platform_or_session():
    assert umo.format_umo("", "GroupMessage", "1") == ""
    assert umo.format_umo("aiocqhttp", "GroupMessage", "") == ""


# ---------- umo_examples ----------
def test_umo_examples_structure():
    ex = umo.umo_examples("aiocqhttp")
    assert ex["group"] == "aiocqhttp:GroupMessage:<群号>"
    assert ex["friend"] == "aiocqhttp:FriendMessage:<QQ号>"


def test_umo_examples_empty_platform():
    ex = umo.umo_examples("")
    assert ex == {"group": "", "friend": ""}


# ---------- umo_from_ids ----------
def test_umo_from_ids_group_only():
    assert umo.umo_from_ids("aiocqhttp", group_id="123456") == [
        "aiocqhttp:GroupMessage:123456"
    ]


def test_umo_from_ids_user_only():
    assert umo.umo_from_ids("aiocqhttp", user_id="987654") == [
        "aiocqhttp:FriendMessage:987654"
    ]


def test_umo_from_ids_both():
    assert umo.umo_from_ids("aiocqhttp", group_id="123456", user_id="987654") == [
        "aiocqhttp:GroupMessage:123456",
        "aiocqhttp:FriendMessage:987654",
    ]


def test_umo_from_ids_both_empty():
    assert umo.umo_from_ids("aiocqhttp") == []


def test_umo_from_ids_empty_platform():
    assert umo.umo_from_ids("", group_id="123456", user_id="987654") == []


# ---------- MESSAGE_TYPES ----------
def test_message_types_constant():
    assert M == ("GroupMessage", "FriendMessage", "OtherMessage")
    assert all(isinstance(t, str) for t in M)


# ---------- v1.0.2 补充：行为确认性用例（G1-G3，锁定当前语义防回归） ----------
def test_parse_umo_platform_with_whitespace_accepted():
    """G1：platform_id 带首尾空白当前按原样接受（不 trim，与 MessageSession 语义一致）。"""
    assert umo.parse_umo("  aiocqhttp:GroupMessage:123") == (
        "  aiocqhttp",
        "GroupMessage",
        "123",
    )


def test_parse_umo_message_type_trailing_space_rejected():
    """G2a：message_type 尾随空格不在白名单 → 拒绝。"""
    assert umo.parse_umo("aiocqhttp:groupmessage :1") is None


def test_parse_umo_whitespace_session_id_accepted():
    """G2b：session_id 为纯空白当前视为非空（不 strip），保持现状锁定。"""
    assert umo.parse_umo("aiocqhttp:GroupMessage: ") == ("aiocqhttp", "GroupMessage", " ")


def test_umo_from_ids_whitespace_ids_produce_raw_umo():
    """G3：group_id/user_id 为纯空白时当前不 strip（if 判定非空即产出），锁定现状。"""
    assert umo.umo_from_ids("aiocqhttp", group_id="  ", user_id=" ") == [
        "aiocqhttp:GroupMessage:  ",
        "aiocqhttp:FriendMessage: ",
    ]
