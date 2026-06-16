"""兼容性 wrapper：cloud.go2.tools → cloud.go2.agentcore.tools.tools

供旧代码/测试使用。新代码应直接导入 cloud.go2.agentcore.tools.tools。

由于 monkeypatch 需要修改这个模块的属性，我们需要动态包装函数
以便在运行时查询这个模块的属性（而非 agentcore.tools.tools 模块的属性）。
"""
import json
from pathlib import Path as _Path

# 初始化 RULES_FILE（可被 monkeypatch 修改）
RULES_FILE = None

def _init_rules_file():
    """初始化 RULES_FILE，可被 monkeypatch 覆盖。"""
    global RULES_FILE
    if RULES_FILE is None:
        from cloud.go2.paths import RULES_FILE as _original_rules_file
        RULES_FILE = _original_rules_file

_init_rules_file()

# 从 agentcore.tools.tools 导入其他函数和常量
from cloud.go2.agentcore.tools.tools import (
    TOOL_FN_MAP,
    TOOL_DESCRIPTIONS,
    get_text_llm,
    get_vision_llm,
    go2_sport,
    go2_move,
    go2_observe,
    go2_status,
    go2_tag_location,
    go2_navigate_to,
    go2_list_locations,
    go2_set_led,
)

# 导入内部工具函数（用于我们的 wrapper）
from cloud.go2.agentcore.tools.tools import (
    _normalize_sport_action,
    _VALID_SPORT_CMDS,
)


def load_rules() -> list:
    """加载规则文件（兼容性包装）。"""
    if not RULES_FILE.exists():
        return []
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def save_rules(rules: list) -> None:
    """保存规则文件（兼容性包装）。"""
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def check_rules(observation: str) -> list[str]:
    """检查规则匹配并更新触发时间（兼容性包装）。"""
    import time
    rules = load_rules()
    triggered = []
    now = time.time()
    for rule in rules:
        trigger = rule["trigger"]
        if trigger in observation:
            cooldown_s = rule.get("cooldown_s", 30)
            last = rule.get("last_triggered", 0)
            if now - last >= cooldown_s:
                triggered.append(rule["action"])
                rule["last_triggered"] = now
    if triggered:
        save_rules(rules)
    return triggered


def go2_add_rule(trigger: str, action: str, cooldown_s: int = 30) -> str:
    """添加规则（兼容性包装，使用本模块的 RULES_FILE）。"""
    canonical = _normalize_sport_action(action)
    if canonical is None:
        return f"不支持的动作 {action}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    action = canonical
    rules = load_rules()
    rules = [r for r in rules if not (r["trigger"] == trigger and r["action"] == action)]
    rules.append({"trigger": trigger, "action": action,
                  "cooldown_s": cooldown_s, "last_triggered": 0})
    save_rules(rules)
    return f"已添加规则：检测到「{trigger}」时执行 {action}，冷却 {cooldown_s}s"


def go2_list_rules() -> str:
    """列出规则（兼容性包装，使用本模块的 RULES_FILE）。"""
    rules = load_rules()
    if not rules:
        return "当前没有规则"
    return "\n".join(
        f"- 检测到「{r['trigger']}」→ {r['action']}（冷却 {r['cooldown_s']}s）"
        for r in rules
    )


__all__ = [
    "RULES_FILE",
    "load_rules",
    "save_rules",
    "check_rules",
    "go2_sport",
    "go2_move",
    "go2_observe",
    "go2_status",
    "go2_add_rule",
    "go2_list_rules",
    "go2_tag_location",
    "go2_navigate_to",
    "go2_list_locations",
    "go2_set_led",
    "TOOL_FN_MAP",
    "TOOL_DESCRIPTIONS",
    "get_text_llm",
    "get_vision_llm",
]
