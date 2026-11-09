"""Fresh, confirmed device teachings for role decisions; never execution grants."""

from copy import deepcopy
import logging

from cloud.agents.common.training import service, store
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.policy import definition_transition_ids

logger = logging.getLogger(__name__)

DECISION_GUIDANCE = (
    "training 是本轮重新读取的已确认、已启用教导，结合当前请求、适用情境和真实状态理解。"
    "有适用教导时优先沿用保存的动作与参数，不用通用常识替换用户偏好。"
    "工具可用时用 apply_training，引用上下文中的 unit_id、teaching_id 和 revision；"
    "overrides 仅包含用户本次明确修改的参数，不重填或猜测已保存参数。"
    "规则的条件和目标状态不是当前已发生的事实，执行仍须通过工具校验和真实回执。"
    "事件教导只在真实事件授权下执行；不适用的教导不影响其他请求。"
    "training 不可用或版本变化时，若提供 list_training 就重新查询；"
    "无法查询时说明读取限制，不猜测已保存方案。"
)


def decision_context(unit_id: str, *, space_id: str = "main",
                     origin_kind: str = "user_delegated") -> dict:
    """Project saved rules without history, drafts, or state-dependent filtering.

    Keep guards and ordered transitions visible even when the current state
    cannot execute them yet. The original executor revalidates every step.
    """
    result = {"unit_id": unit_id, "status": "ok", "records": [], "transitions": []}
    try:
        card = service._target(unit_id, space_id)
        catalog = catalog_for(card)
        records = store.list_records(unit_id, space_id=space_id)
    except (ValueError, OSError) as exc:
        logger.warning("Training context unavailable for %s: %s", unit_id, exc)
        return {**result, "status": "unavailable"}

    referenced = set()
    for record in records:
        if (record["desired_status"] != "enabled"
                or origin_kind not in record["definition"]["origins"]):
            continue
        try:
            service._check_catalog(record, catalog)
        except ValueError:
            continue
        result["records"].append({key: deepcopy(record[key]) for key in (
            "unit_id", "teaching_id", "revision", "name", "effect_text", "definition")})
        referenced.update(definition_transition_ids(record["definition"]))
    result["transitions"] = [{key: deepcopy(edge[key]) for key in (
        "id", "state_field", "src", "dst", "source_label", "destination_label",
        "action", "selector_params", "parent")}
        for edge in catalog["transitions"] if edge["id"] in referenced]
    return result
