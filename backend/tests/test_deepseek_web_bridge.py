import json

from deerflow.models.deepseek_web_bridge import (
    DeepSeekWebBridge,
    choose_best_assistant_candidate,
    choose_best_assistant_text,
    choose_best_payload_candidate,
    choose_best_payload_text,
    extract_transport_payload_candidates,
    extract_json_object,
    is_placeholder_assistant_payload_text,
    is_suspicious_short_fragment,
    salvage_tool_calls_payload,
)


def test_compute_delta_messages_returns_new_suffix():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    bridge._sticky_last_messages = [  # noqa: SLF001
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]

    current = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "tool-result", "tool_call_id": "call_1"},
        {"role": "user", "content": "u2"},
    ]

    assert bridge.compute_delta_messages(current) == [
        {"role": "tool", "content": "tool-result", "tool_call_id": "call_1"},
        {"role": "user", "content": "u2"},
    ]


def test_compute_delta_messages_falls_back_to_last_message_when_same_history():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    bridge._sticky_last_messages = [  # noqa: SLF001
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]

    current = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]

    assert bridge.compute_delta_messages(current) == [
        {"role": "assistant", "content": "a1"},
    ]


def test_reanchor_threshold_triggers_full_mode():
    bridge = DeepSeekWebBridge(
        sticky_marker="flowflow__system_prompt_v1",
        sticky_reanchor_messages=3,
    )
    bridge._sticky_initialized = True  # noqa: SLF001
    bridge._sticky_last_messages = [  # noqa: SLF001
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    bridge._sticky_messages_since_full = 2  # noqa: SLF001

    current = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]

    delta = bridge.compute_delta_messages(current)
    should_reanchor = (
        bridge.sticky_reanchor_messages is not None
        and bridge._sticky_messages_since_full + len(delta)  # noqa: SLF001
        >= bridge.sticky_reanchor_messages
    )

    assert delta == [{"role": "user", "content": "u2"}]
    assert should_reanchor is True


def test_thinking_candidate_state_detects_deepseek_toggle_selection():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")

    assert (
        bridge._thinking_candidate_state(  # noqa: SLF001
            {
                "className": "ds-atom-button ds-toggle-button ds-toggle-button--selected ds-toggle-button--md",
            }
        )
        is True
    )
    assert (
        bridge._thinking_candidate_state(  # noqa: SLF001
            {
                "className": "ds-atom-button ds-toggle-button ds-toggle-button--md",
            }
        )
        is False
    )


def test_switch_session_clears_runtime_state_and_resets_page(monkeypatch, tmp_path):
    first = tmp_path / "session-a.json"
    second = tmp_path / "session-b.json"

    bridge = DeepSeekWebBridge(
        sticky_marker="marker-a",
        session_state_path=str(first),
        reuse_persisted_chat=True,
    )
    bridge._active_session_state_path = str(first.resolve())  # noqa: SLF001
    bridge._active_sticky_marker = "marker-a"  # noqa: SLF001
    bridge._sticky_initialized = True  # noqa: SLF001
    bridge._sticky_last_messages = [{"role": "user", "content": "hi"}]  # noqa: SLF001
    bridge._sticky_messages_since_full = 3  # noqa: SLF001
    bridge._persisted_chat_url = "https://chat.deepseek.com/a/chat/s/demo"  # noqa: SLF001
    bridge._state_loaded = True  # noqa: SLF001

    saved_paths: list[str | None] = []
    reset_calls = {"count": 0}

    monkeypatch.setattr(
        bridge,
        "_save_session_state",
        lambda: saved_paths.append(bridge._active_session_state_path),  # noqa: SLF001
    )
    monkeypatch.setattr(
        bridge,
        "reset_page",
        lambda: reset_calls.__setitem__("count", reset_calls["count"] + 1),
    )

    bridge.session_state_path = str(second)
    bridge.sticky_marker = "marker-b"
    bridge._switch_session_if_needed()  # noqa: SLF001

    assert saved_paths == [str(first.resolve())]
    assert reset_calls["count"] == 1
    assert bridge._active_session_state_path == str(second.resolve())  # noqa: SLF001
    assert bridge._active_sticky_marker == "marker-b"  # noqa: SLF001
    assert bridge._sticky_initialized is False  # noqa: SLF001
    assert bridge._sticky_last_messages == []  # noqa: SLF001
    assert bridge._sticky_messages_since_full == 0  # noqa: SLF001
    assert bridge._persisted_chat_url is None  # noqa: SLF001
    assert bridge._state_loaded is False  # noqa: SLF001


def test_parse_model_payload_accepts_standard_tool_arguments():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    arguments = {
        "path": "/mnt/user-data/workspace/fetch_stock_history.py",
        "content": "#!/usr/bin/env python3\nprint('hello')\n",
        "description": "Write a script",
    }
    raw = json.dumps(
        {
            "content": "",
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": arguments,
                    "id": "call_123",
                }
            ],
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == ""
    assert payload["tool_calls"] == [
        {
            "id": "call_123",
            "name": "write_file",
            "arguments": arguments,
        }
    ]


def test_parse_model_payload_accepts_json_string_arguments():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    arguments = {
        "path": "/mnt/user-data/workspace/fetch_stock_history.py",
        "content": "print('hello')\n",
    }
    raw = json.dumps(
        {
            "content": "",
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                    "id": "call_456",
                }
            ],
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == ""
    assert payload["tool_calls"] == [
        {
            "id": "call_456",
            "name": "write_file",
            "arguments": arguments,
        }
    ]


def test_parse_model_payload_promotes_single_tool_object_to_tool_calls():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = json.dumps(
        {
            "tool": "Bash",
            "arguments": {
                "command": "ls -la",
                "description": "List files in current directory",
            },
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == ""
    assert len(payload["tool_calls"]) == 1
    assert payload["tool_calls"][0]["name"] == "Bash"
    assert payload["tool_calls"][0]["arguments"]["command"] == "ls -la"


def test_parse_model_payload_promotes_function_style_tool_call_from_content():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = json.dumps(
        {
            "content": "我来阅读项目的README文件，了解这个项目的具体内容。\nread_file({\"description\": \"阅读项目README了解详情\", \"path\": \"/mnt/workspaces/downloads/my-poor-renderer-master/README.md\"})"
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == ""
    assert payload["tool_calls"] == [
        {
            "id": payload["tool_calls"][0]["id"],
            "name": "read_file",
            "arguments": {
                "description": "阅读项目README了解详情",
                "path": "/mnt/workspaces/downloads/my-poor-renderer-master/README.md",
            },
        }
    ]
    assert payload["tool_calls"][0]["id"].startswith("call_")


def test_parse_model_payload_promotes_fenced_json_tool_block_from_content():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = json.dumps(
        {
            "content": "我来研究一下这个项目。\n```json\n{\"tool\":\"ls\",\"arguments\":{\"description\":\"查看项目根目录结构\",\"path\":\"/mnt/workspaces/downloads/my-poor-renderer-master\"}}\n```"
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == ""
    assert payload["tool_calls"] == [
        {
            "id": payload["tool_calls"][0]["id"],
            "name": "ls",
            "arguments": {
                "description": "查看项目根目录结构",
                "path": "/mnt/workspaces/downloads/my-poor-renderer-master",
            },
        }
    ]
    assert payload["tool_calls"][0]["id"].startswith("call_")


def test_parse_model_payload_keeps_ls_from_real_capture_style_payload():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = json.dumps(
        {
            "content": "正在分析项目源码结构，先列目录。",
            "tool_calls": [
                {
                    "id": "ls_1",
                    "name": "ls",
                    "arguments": {
                        "description": "查看项目根目录结构",
                        "path": "/mnt/workspaces/downloads/my-poor-renderer-master",
                    },
                }
            ],
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["tool_calls"] == [
        {
            "id": "ls_1",
            "name": "ls",
            "arguments": {
                "description": "查看项目根目录结构",
                "path": "/mnt/workspaces/downloads/my-poor-renderer-master",
            },
        }
    ]


def test_parse_model_payload_normalizes_hyphenated_tool_name():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = json.dumps(
        {
            "name": "write-file",
            "arguments": {"path": "/tmp/a.txt", "content": "hello"},
            "id": "call_custom_1",
        },
        ensure_ascii=False,
    )

    payload = bridge.parse_model_payload(raw)

    assert payload["tool_calls"] == [
        {
            "id": "call_custom_1",
            "name": "write_file",
            "arguments": {"path": "/tmp/a.txt", "content": "hello"},
        }
    ]


def test_extract_json_object_repairs_unescaped_quotes_in_tool_argument_string():
    raw = """{"content":"","tool_calls":[{"name":"bash","arguments":{"description":"Fetch data","command":"python -c "
import requests
print("hello")
"","path":"/tmp/x.py"},"id":"call_bash_1"}]}"""

    payload = extract_json_object(raw)

    assert payload["content"] == ""
    assert payload["tool_calls"][0]["name"] == "bash"
    assert payload["tool_calls"][0]["arguments"]["description"] == "Fetch data"
    assert payload["tool_calls"][0]["arguments"]["path"] == "/tmp/x.py"
    assert payload["tool_calls"][0]["arguments"]["command"] == 'python -c "\nimport requests\nprint("hello")\n"'


def test_salvage_tool_calls_payload_recovers_write_file_with_unescaped_script_quotes():
    raw = """{"content":"","tool_calls":[{"name":"write_file","arguments":{"description":"Write Python script to fetch Cambricon stock data","path":"/mnt/user-data/workspace/fetch_cambricon.py","content":"import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

stock_code = "688256"
print(f"hello {stock_code}")
"},"id":"call_6"}]}"""

    payload = salvage_tool_calls_payload(raw)

    assert payload is not None
    assert payload["content"] == ""
    assert payload["tool_calls"] == [
        {
            "id": "call_6",
            "name": "write_file",
            "arguments": {
                "description": "Write Python script to fetch Cambricon stock data",
                "path": "/mnt/user-data/workspace/fetch_cambricon.py",
                "content": 'import akshare as ak\nimport pandas as pd\nfrom datetime import datetime, timedelta\n\nstock_code = "688256"\nprint(f"hello {stock_code}")\n',
            },
        }
    ]


def test_parse_model_payload_recovers_bash_command_when_arguments_object_misses_closing_brace():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = """{"content":"","tool_calls":[{"name":"bash","arguments":{"description":"Fetch Cambricon 30-day daily price data via Python script","command":"python -c "
import requests
import json
from datetime import datetime, timedelta

# 使用新浪财经API获取寒武纪(688256)历史数据
symbol = 'sh688256'
end_date = datetime.now()
start_date = end_date - timedelta(days=30)

# 新浪财经历史数据接口
url = f'https://quotes.sina.com.cn/cn/api/jsonp_v2.php/var%20historyData_%3D/data/CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=30'

try:
 response = requests.get(url, timeout=10)
 # 解析JSONP响应
 import re
 json_str = re.search(r'(({.}))', response.text)
 if json_str:
  data = json.loads(json_str.group(1))
  print('日期,开盘,收盘,最高,最低,成交量')
  for item in data:
   print(f"{item['day']},{item['open']},{item['close']},{item['high']},{item['low']},{item['volume']}")
 else:
  print('获取数据失败')
except Exception as e:
 print(f'错误: {e}')
"","id":"call_bash_cambricon"}]}"""

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == ""
    assert payload["tool_calls"] == [
        {
            "id": "call_bash_cambricon",
            "name": "bash",
            "arguments": {
                "description": "Fetch Cambricon 30-day daily price data via Python script",
                "command": """python -c "
import requests
import json
from datetime import datetime, timedelta

# 使用新浪财经API获取寒武纪(688256)历史数据
symbol = 'sh688256'
end_date = datetime.now()
start_date = end_date - timedelta(days=30)

# 新浪财经历史数据接口
url = f'https://quotes.sina.com.cn/cn/api/jsonp_v2.php/var%20historyData_%3D/data/CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=30'

try:
 response = requests.get(url, timeout=10)
 # 解析JSONP响应
 import re
 json_str = re.search(r'(({.}))', response.text)
 if json_str:
  data = json.loads(json_str.group(1))
  print('日期,开盘,收盘,最高,最低,成交量')
  for item in data:
   print(f"{item['day']},{item['open']},{item['close']},{item['high']},{item['low']},{item['volume']}")
 else:
  print('获取数据失败')
except Exception as e:
 print(f'错误: {e}')
\"""",
            },
        }
    ]


def test_choose_best_assistant_text_prefers_full_dom_json_over_truncated_visible_text():
    rendered = """查看其他 3 个步骤
在网络上搜索 “中东 安全 冲突 巴以 伊朗 叙利亚 2026”
{"content":"根据您的需求..._省略号结尾"""
    dom = """查看其他 3 个步骤
在网络上搜索 “中东 安全 冲突 巴以 伊朗 叙利亚 2026”
{"content":"根据您的需求","tool_calls":[]}"""

    assert choose_best_assistant_text([rendered, dom]) == dom.strip()


def test_choose_best_assistant_candidate_prefers_payload_over_later_prompt_replay():
    assistant_payload = '{"content":"","tool_calls":[{"name":"write_file","arguments":{"path":"/tmp/a.py"},"id":"call_1"}]}'
    prompt_replay = """Continue the existing DeerFlow session already initialized in this chat.

Return exactly one JSON object with this schema:

{"content":"string","tool_calls":[{"name":"string","arguments":{},"id":"string"}]}
"""

    chosen = choose_best_assistant_candidate(
        [
            {"index": 4, "text": assistant_payload},
            {"index": 5, "text": prompt_replay},
        ]
    )

    assert chosen == {"index": 4, "text": assistant_payload}


def test_choose_best_assistant_candidate_prefers_latest_reply_over_longer_older_payload():
    older_payload = '{"content":"this is the first reply and it is much longer","tool_calls":[]}'
    latest_reply = '{"content":"second turn","tool_calls":[]}'

    chosen = choose_best_assistant_candidate(
        [
            {"index": 4, "text": older_payload},
            {"index": 5, "text": latest_reply},
        ]
    )

    assert chosen == {"index": 5, "text": latest_reply}


def test_choose_best_assistant_candidate_falls_back_to_latest_when_no_payload_markers_exist():
    chosen = choose_best_assistant_candidate(
        [
            {"index": 2, "text": "thinking"},
            {"index": 3, "text": "still thinking but longer"},
        ]
    )

    assert chosen == {"index": 3, "text": "still thinking but longer"}


def test_choose_best_payload_candidate_prefers_latest_dom_payload_over_longer_older_payload():
    older_payload = {
        "probeId": "old",
        "domIndex": 40,
        "text": '{"content":"this is the first reply and it is much longer","tool_calls":[]}',
    }
    latest_payload = {
        "probeId": "new",
        "domIndex": 41,
        "text": '{"content":"second turn","tool_calls":[]}',
    }

    assert choose_best_payload_candidate([older_payload, latest_payload]) == latest_payload


def test_extract_transport_payload_candidates_finds_nested_json_payload_string():
    payload = '{"content":"根据您的需求","tool_calls":[]}'
    raw = json.dumps(
        {
            "id": "resp_1",
            "choices": [
                {
                    "message": {
                        "content": payload,
                    }
                }
            ],
        },
        ensure_ascii=False,
    )

    candidates = extract_transport_payload_candidates(raw)

    assert payload in candidates
    assert choose_best_payload_text(candidates) == payload


def test_choose_best_payload_text_prefers_complete_transport_payload_over_partial_frame():
    partial = '{"content":"根据您的需求'
    complete = '{"content":"根据您的需求","tool_calls":[]}'

    assert choose_best_payload_text([partial, complete]) == complete


def test_extract_transport_payload_candidates_ignores_challenge_json():
    raw = json.dumps(
        {
            "code": 0,
            "msg": "",
            "data": {
                "biz_code": 0,
                "biz_msg": "",
                "biz_data": {
                    "challenge": {
                        "algorithm": "DeepSeekHashV1",
                        "target_path": "/api/v0/chat/completion",
                    }
                },
            },
        },
        ensure_ascii=False,
    )

    assert extract_transport_payload_candidates(raw) == []


def test_parse_model_payload_recovers_tool_calls_without_ids_from_relaxed_jsonish_text():
    bridge = DeepSeekWebBridge(sticky_marker="flowflow__system_prompt_v1")
    raw = """{"content":"明白。我将直接开始执行回测代码。","tool_calls":[{"name":"bash","arguments":{"description":"install required libraries for backtesting","command":"cd /mnt/user-data/workspace && python -m pip install pandas numpy matplotlib seaborn akshare yfinance scipy --quiet"}},{"name":"write_file","arguments":{"description":"write reversal factor backtesting script","path":"/mnt/user-data/workspace/reversal_backtest.py","content":"import pandas as pd
import numpy as np
import akshare as ak

df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date.strftime("%Y%m%d"), end_date=end_date.strftime("%Y%m%d"), adjust="qfq")
print("done")
"}}]}"""

    payload = bridge.parse_model_payload(raw)

    assert payload["content"] == "明白。我将直接开始执行回测代码。"
    assert len(payload["tool_calls"]) == 2
    assert payload["tool_calls"][0]["name"] == "bash"
    assert payload["tool_calls"][0]["arguments"]["command"].startswith(
        "cd /mnt/user-data/workspace && python -m pip install"
    )
    assert payload["tool_calls"][1]["name"] == "write_file"
    assert payload["tool_calls"][1]["arguments"]["path"] == "/mnt/user-data/workspace/reversal_backtest.py"
    assert 'period="daily"' in payload["tool_calls"][1]["arguments"]["content"]
    assert 'print("done")' in payload["tool_calls"][1]["arguments"]["content"]
    assert payload["tool_calls"][0]["id"].startswith("call_")
    assert payload["tool_calls"][1]["id"].startswith("call_")


def test_is_suspicious_short_fragment_flags_truncated_tokens_only():
    assert is_suspicious_short_fragment("我们") is True
    assert is_suspicious_short_fragment("ok") is True
    assert is_suspicious_short_fragment('{"content":"hi","tool_calls":[]}') is False
    assert is_suspicious_short_fragment("好的，已完成。") is False


def test_is_placeholder_assistant_payload_text_detects_schema_literal_output():
    assert is_placeholder_assistant_payload_text('{"content":"string","tool_calls":[]}') is True
    assert (
        is_placeholder_assistant_payload_text(
            '{"content":"","tool_calls":[{"name":"string","arguments":{},"id":"string"}]}'
        )
        is True
    )
    assert is_placeholder_assistant_payload_text('{"content":"已完成修改","tool_calls":[]}') is False
