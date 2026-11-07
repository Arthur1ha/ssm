# 用户可训练的设备状态机 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户在现有设备能力内，通过自然语言训练状态转换条件、触发关系、动作参数和行为子流程，并在可交互状态机中查看、验证、修改与撤销训练效果。

**Architecture:** 保留基础设备 FSM，在统一教导存储中增加版本化训练定义；角色 LangGraph 生成修改草案，所属智能体将确认后的行为定义装配为 LangGraph 子图，所有动作仍通过现有执行器和真实回执。基础 Card 声明可训练能力，授权 API/SSE 投影用户训练与运行痕迹；单设备自主流程由所属智能体运行，跨设备流程由明确的发起智能体持有，不新增中央自主控制器。

**Tech Stack:** Python 3.13、uv、现有 LangChain / LangGraph、Pydantic、FastAPI、文件检查点与 JSON 持久化、MQTT/HTTP、React 18 无构建 PWA、pytest、现有 Node/浏览器检查脚本。

**Spec:** [用户可训练的设备状态机设计](../specs/2026-11-07-trainable-device-state-machines-design.md)。执行每项任务前同时阅读该设计与本计划。

## Global Constraints

- 不新增感知能力，不新增运动能力；只编排已有状态机、观测、技能和参数。
- 语言理解、智能决策和行为流程继续使用现有 LangChain / LangGraph；不引入第二套 Agent 运行时。
- 训练覆盖转换条件、事件触发、动作参数与行为子流程，并包含跨设备组合。
- 前端连线可点击；训练痕迹必须可见、可解释、可修改、可撤销。
- 继续使用 React 18 无构建 PWA、同源 API/SSE、文件持久化和现有设备执行器。
- MQTT 继续承担控制面；灯通过 MQTT 执行，Go2 通过现有 HTTP/WebRTC 适配执行。
- 本次交付是设计和实施计划；不执行设备动作、不部署、不修改固件。

---

日期：2026-11-07。状态：**待用户核对，尚未开始实施**。所有复选框均为后续工作；文中测试代码与命令是实施要求，不代表已运行通过。

当前工作区已存在用户修改：`app/src/components/SessionGate.jsx`、`app/styles/app.css`。实施前重新核对 `git status --short`，保留所有已有改动。后续 CSS 修改按相关差异暂存，禁止 `git add .` 混入其他工作。

## 1. 交付范围与实施顺序

这是一个端到端产品能力，按可独立验证的里程碑推进，不把 API、执行与前端割裂成无闭环的项目。设备适配分别验收，所有任务共用一个训练契约。

| 里程碑 | 任务 | 可核对结果 |
|---|---|---|
| M1：可保存且可真实限制行为 | 1–5 | 稳定边 ID、结构化条件、版本保存/加载、Go2/灯全部入口执行同一条件 |
| M2：可组合已有动作 | 6–9 | LangGraph 顺序/分支/重复/等待、灯提醒、探索节奏及失败处理 |
| M3：设备之间可组合 | 10 | 真实结果接力、跨设备并行汇合、受限训练许可 |
| M4：自然语言与图形成闭环 | 11–14 | 多轮教学、连线详情、修改预览、训练痕迹和培养记录 |
| M5：完整案例与迁移交付 | 15–16 | 六组案例、旧教导迁移、回归与部署/回退说明 |

任务有完整测试周期；任务内部动作按 2–5 分钟的小步骤执行。较长的设备适配按测试用例逐条推进，每通过一条形成可观察结果，不一次改完所有路径再测。文中 git commit 是后续实施的建议提交边界；本次仅保存计划文档，不提交用户代码。

## 2. 文件结构与职责

### 新增文件

| 路径 | 单一职责 |
|---|---|
| `cloud/agents/common/training/__init__.py` | 包入口，不放运行逻辑 |
| `cloud/agents/common/training/catalog.py` | 从真实 Card/设备声明生成训练目录和稳定哈希 |
| `cloud/agents/common/training/models.py` | 严格 Pydantic 训练、草案、流程、事件模型 |
| `cloud/agents/common/training/validation.py` | 能力、参数、流程结构、冲突检查 |
| `cloud/agents/common/training/conditions.py` | 三值条件计算与参数合并/限制 |
| `cloud/agents/common/training/service.py` | 草案、确认、撤销、版本加载与 API 单写协调 |
| `cloud/agents/common/training/projection.py` | 有效定义、差异、运行痕迹的授权视图 |
| `cloud/agents/common/training/compiler.py` | 声明式有限流程装配为原生 StateGraph |
| `cloud/agents/common/training/runtime.py` | 子图调用、现有 run index/checkpoint/effect 接入 |
| `cloud/agents/common/training/events.py` | 结构化事件、去重恢复和持久等待的投递适配 |
| `cloud/agents/common/training/grants.py` | 已确认训练的逐步骤、逐目标执行许可 |
| `cloud/agents/common/training/tools.py` | 三个角色共用教学工具的 schema 与薄适配 |
| `cloud/agents/lamp/workflows/training.py` | 灯既有工具与训练子图连接 |
| `cloud/agents/go2/workflows/training.py` | Go2 探索/互动的训练子图连接 |
| `cloud/api/training.py` | 独立于模型运行的确认、投影、回应/取消路由 |
| `app/src/components/TrainingPreview.jsx` | 草案差异与确认卡 |
| `app/src/components/TrainingHistory.jsx` | 版本、原话、应用记录与撤销 |
| `app/src/pages/TransitionInspector.jsx` | 连线/状态详情与教学输入入口 |
| `app/src/pages/TrainingFlow.jsx` | 行为子流程与跨设备流程视图 |
| `app/src/hooks/useTraining.js` | 初始快照、SSE、revision 合并及写操作 |
| `scripts/check_training_frontend.cjs` | 模拟 API/事件的前端检查 |

新 Python 行为测试与模块相邻，以 `*_checks.py` 命名；每项任务明确测试路径。必要的共享测试构造器放 `cloud/agents/common/training/check_fixtures.py`，仅测试导入。

### 复用并修改的边界

- `cloud/agents/common/teachings.py`：唯一教导存储，不新建平行规则库。
- `cloud/agents/common/checkpoint.py`、`effects.py`、`run_index.py`：复用原实现；仅有证据表明缺失接口时增加薄适配。
- `cloud/cards/schema.py`、`validation.py`、`builder.py`、`cloud/go2/card.py`、`state.py`：增加稳定 ID 与训练能力声明，不改固件状态含义。
- `cloud/go2/tools/executor.py`、`service.py`、`cloud/esp32/tool_executor.py`：统一 guard/params 边界。
- `cloud/go2/mode/free_explore.py`、`navigation/navigator.py`、`cloud/agents/lamp/workflows/{reactive,runtime}.py`：接入现有事件/导航/冷却与所有权。
- `cloud/peers/{gateway,inbound,policy}.py`、`cloud/space/execution.py`：传递并验证训练调用引用，保留原身份与普通请求政策。
- `cloud/agents/{steward,lamp,go2}/graph.py` 及各自工具声明：注册共用教学工具，继续走现有对话入口。
- `cloud/api/{teachings,intents,main}.py`：兼容旧教导、传递选择上下文、注册路由/事件。
- `app/src/{app.jsx,ApiClient.js}`、`hooks/{useSendIntent,useSsmCore}.js`、`pages/{FsmGraph,HierarchicalFsmBoard,FsmDevicePage,RulesPage}.jsx`、`utils/{stateMachine,stateSnapshot}.js`、`components/ActivityFeed.jsx`、`app/index.html`、`app/sw.js`、`app/styles/app.css`：图交互、加载顺序、缓存版本、训练投影。

## 3. 公共接口约定

以下都是本计划**拟新增**接口，不能误认为当前已存在。字典边界须经 `models.py` 校验；跨模块不直接修改模型内部字典。Python 类型中的 `CompiledStateGraph` 来自 `langgraph.graph.state`。

```python
# catalog.py
def catalog_for(card: dict) -> dict: ...
def capability_hash(card: dict) -> str: ...

# validation.py / conditions.py
def validate_definition(definition: dict, cards: dict[str, dict]) -> dict: ...
def find_conflicts(definition: dict, records: list[dict]) -> list[dict]: ...
def evaluate_condition(condition: dict | None, facts: dict, *, now: float) -> dict: ...
def apply_transition_training(records: list[dict], target: dict,
                              requested_params: dict, facts: dict, *, now: float) -> dict: ...

# teachings.py：保留现有 API；增加 revision 写入与历史读取。
def replace_revision(unit_id: str, teaching_id: str, *, space_id: str,
                     expected_revision: int, changes: dict) -> dict: ...
def history_for(unit_id: str, teaching_id: str, *, space_id: str) -> list[dict]: ...

# service.py：只有 API 进程可以写；角色 Graph 调受认证服务适配器。
def prepare_proposal(request: dict, session: dict) -> dict: ...
def confirm_proposal(proposal_id: str, expected_revision: int, session: dict) -> dict: ...
def set_training_status(teaching_id: str, desired_status: str,
                        expected_revision: int, session: dict) -> dict: ...
def load_effective(unit_id: str, space_id: str) -> dict: ...
def acknowledge_revision(teaching_id: str, revision: int,
                         capability_hashes: dict, service_context: dict) -> dict: ...

# projection.py
def project_training(card: dict, effective: dict, facts: dict, viewer: dict) -> dict: ...
def diff_definitions(before: dict | None, after: dict | None) -> list[dict]: ...

# compiler.py：回调签名 execute(call:dict,state:dict)->Awaitable[dict]；
# read_facts()->dict；choose(options:list[dict],state:dict)->Awaitable[str]。
def compile_workflow(definition: dict, *, execute, read_facts, choose,
                     checkpointer, ledger) -> CompiledStateGraph: ...

# runtime.py：所有生产运行均使用这里的锁、初始化与恢复边界。
async def start_training(teaching_id: str, event: dict, context: dict) -> dict: ...
async def resume_training(run_id: str, event: dict, context: dict) -> dict: ...
async def cancel_training(run_id: str, reason: str, context: dict) -> dict: ...

# events.py
def normalize_event(raw: dict, source: dict) -> dict: ...
async def deliver_event(event: dict) -> list[str]: ...
async def restore_waits(unit_id: str) -> int: ...

# grants.py
def authorize_training_step(reference: dict, call: dict, context: dict) -> dict: ...

# tools.py：三个角色共享，API 调用为现有内部服务适配器。
async def propose_training(arguments: dict, context: dict, api_call) -> dict: ...
```

接口中的省略号表示签名声明，具体实现步骤见各任务，不是允许留下空实现。

关键返回值：

- `catalog_for` → `{schema_version:1, unit_id, transitions, facts, events, steps, node_types, capability_hash}`。
- 条件计算 → `{value:"true"|"false"|"unknown", reasons:[{fact,code}]}`。
- 执行判断 → `{allowed:bool, params:dict, teaching_refs:[{id,revision}], reasons:list}`。
- `load_effective` → `{unit_id, catalog_hash, projection_revision, records, loaded_revisions}`；records 只含该设备已激活版本，不含未确认草案。
- 草案 → `{proposal_id, operation, base_revision, definition, diff, conflicts, capability_hashes, expires_at}`；operation 为 create/update/disable/restore。
- 运行 → `{run_id, teaching_id, teaching_revision, owner_unit_id, status, step_id, request_id, receipt, waiting, revision}`。

## 4. API 与事件约定

现有 `GET/POST/PATCH /api/teachings` 保留兼容；新增路由在 `cloud/api/training.py`，避免让旧 Draft 可悄悄接受任意流程。

| 方法与路径 | 用途 | 关键约束 |
|---|---|---|
| `POST /api/teachings/proposals` | 建立经过校验的创建/修改草案 | 已认证管理员；Graph 服务调用须带原会话；不执行设备 |
| `GET /api/teachings/proposals/{id}` | 恢复待确认草案 | 同空间、创建会话可读；默认 30 分钟有效 |
| `POST /api/teachings/proposals/{id}/confirm` | 确认草案 | CSRF、expected_revision、能力 hash 再检查；重试幂等 |
| `PUT /api/teachings/{id}/activation` | 暂停/启用 | 管理员、CSRF、expected_revision |
| `GET /api/teachings/{id}/history` | 修订与原话 | 管理员可读；普通运行视图不泄露原话 |
| `POST /api/teachings/{id}/restore` | 从历史创建新草案 | 输入历史 revision，仍经预览确认 |
| `GET /api/devices/{unit_id}/training` | 能力、有效规则与图覆盖快照 | 当前会话过滤、无副作用 |
| `POST /api/teachings/{id}/runs` | 用户主动运行已确认流程 | 校验模式、当前会话、设备所有权；稳定 request_id |
| `GET /api/training-runs/{run_id}` | 查询运行 | 同空间并按原会话/活动可见性过滤 |
| `POST /api/training-runs/{run_id}/respond` | 回应等待 | wait_id + 允许的 response；一次消费 |
| `POST /api/training-runs/{run_id}/cancel` | 取消流程 | 用户/所属智能体授权；只取消仍持有的任务 |
| `POST /api/internal/teachings/{id}/loaded` | 所属智能体确认加载版本 | 内部服务认证及 unit_id 归属；浏览器不可伪造 |

注册固定子路由时检查 FastAPI 路径匹配，避免 `/proposals` 被既有 `/{teaching_id}` 捕获。

通过现有 `/api/events` 发布 `training.proposed / training.updated / training.activation / training.run / training.decision`。持久配置用 `projection_revision`；运行用 `run_id + revision`；设备物理状态继续使用原 epoch/revision。各游标独立合并，不互相覆盖。SSE 缺段/重连时重取训练快照一次，不做周期 REST 轮询。

## 5. 实施任务

### Task 1: 稳定转换 ID 与真实可训练能力目录

**Files:**
- Create: `cloud/agents/common/training/{__init__,catalog,check_fixtures,catalog_checks}.py`
- Modify: `cloud/cards/{schema,validation,builder}.py`, `cloud/go2/{state,card}.py`
- Test: `cloud/cards/region_contract_checks.py`, `cloud/go2/definition_contract_checks.py`

**Interfaces:** 消费现有 Card/动作 schema；产出 `catalog_for`、`capability_hash` 和 Card 的 `training_capabilities`。测试构造器 `lamp_card()` 返回由真实 builder 生成的当前版本灯 Card；`definition(kind="guard")` 返回完整 v2 定义；`workflow(calls)` 返回顺序 skill→finish 定义，calls 为 `{unit_id,skill_id,params}` 列表。

`check_fixtures.py` 的灯 fixture 从以下入口开始，后两个构造器按设计 §5 填入全部字段，顺序节点 ID 固定为 `step_0 / step_1 / finish`，使不同测试的期望引用一致：

```python
from cloud.cards.builder import build_card_from_manifest
from edge.lamp_contract import FSM_VERSION, capabilities

def lamp_card() -> dict:
    return build_card_from_manifest({
        "unit_id": "esp32_desk_led", "device_id": "esp32_desk",
        "unit": "led", "agent_type": "actuator", "fsm_version": FSM_VERSION,
        "capabilities": capabilities(),
    })
```

- [ ] **Step 1: 增加失败测试，固定稳定 ID 与哈希边界。**

```python
from copy import deepcopy
from cloud.agents.common.training.catalog import catalog_for, capability_hash
from cloud.agents.common.training.check_fixtures import lamp_card

def test_layout_and_online_do_not_change_training_hash():
    card = lamp_card()
    changed = deepcopy(card)
    changed["online"] = not card.get("online", False)
    changed["state_machine"]["layout"] = {"OFF": {"x": 0.1, "y": 0.2}}
    assert capability_hash(changed) == capability_hash(card)
    rows = catalog_for(card)["transitions"]
    assert any(row["id"] == "lighting.OFF.CMD_BLINK.BLINKING" for row in rows)
    assert all(not row["trainable"] for row in rows if row["trigger"] == "BLINK_DONE")
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/catalog_checks.py -q`；预期先因缺少目录实现失败。
- [ ] **Step 3: 从声明处增加 ID、trainable 和完成依据，生成稳定目录。** `check_fixtures.py` 只调用现有 builder 的真实入口构造 fixture，不复制设备参数范围。目录 hash 用如下规则，排序列只覆盖能力相关字段：

```python
from hashlib import sha256
import json

def stable_digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return sha256(encoded).hexdigest()
```

将 `stable_digest` 定义在 catalog.py 内；`catalog_for` 不从哈希回调自己形成递归。旧 Card 返回空可训练目录及具体不支持原因；同一动作多条源边分别保留 ID。导航训练步骤只是已有内部能力的受控组合声明，不把临时坐标工具公开成所有同伴可调用技能。

- [ ] **Step 4: 补充参数范围改变导致 hash 改变、边排序不影响 ID、完成依据不可用的技能不能串联测试。** 运行本任务三个测试文件，预期通过。
- [ ] **Step 5: 提交本任务文件。** `git commit -m "feat: declare stable trainable device transitions"`。

### Task 2: 训练模型、三值条件、参数与流程校验

**Files:**
- Create: `cloud/agents/common/training/{models,validation,conditions,definition_checks}.py`
- Modify: `cloud/agents/common/training/check_fixtures.py`

**Interfaces:** 消费 Task 1 目录；产出设计 §5 中完整 definition 模型及 `validate_definition / find_conflicts / evaluate_condition / apply_transition_training`。事实格式 `{fact_name:{value,ts,max_age_s,source}}`；条件为空为 true。`models.py` 同时定义 Task 6 所需的 `TrainingRunState(TypedDict, total=False)`；receipts 使用合并 reducer、其他字段由单一控制节点更新，parallel 分支只写各自 receipt，不并发写 step_id/status。

- [ ] **Step 1: 编写缺失事实经 not 仍为 unknown 的测试。**

```python
from cloud.agents.common.training.conditions import evaluate_condition

def test_unknown_does_not_become_permission_through_not():
    condition = {"not": {"fact": "state.navigation_active", "op": "eq", "value": True}}
    result = evaluate_condition(condition, {}, now=100)
    assert result["value"] == "unknown"
    assert result["reasons"][0]["code"] == "fact_missing"
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/definition_checks.py -q`，预期失败。
- [ ] **Step 3: 实现严格模型与三值运算；不允许 eval、任意字段路径或模型提交的执行代码。**

```python
def negate(value: str) -> str:
    return {"true": "false", "false": "true", "unknown": "unknown"}[value]

def all_values(values: list[str]) -> str:
    if "false" in values:
        return "false"
    return "unknown" if "unknown" in values else "true"

def merge_defaults(defaults: dict, requested: dict) -> dict:
    return {**defaults, **requested}
```

这些私有辅助函数在 conditions.py 定义。实现 `any` 的对偶规则；比较前校验字段类型，拒绝 bool 充当数字、NaN、Infinity；事实过期返回 unknown。对系统转换禁止训练；普通 guard 与基础条件做 AND；默认参数冲突和 limits 空交集拒绝。流程入口、引用、唯一节点 ID、分支出口、repeat 上限、parallel 不同设备、终态可达性及无任意环逐项校验。

- [ ] **Step 4: 用明确断言补齐参数优先级、超范围拒绝、未知技能/状态拒绝、来源作用域、冲突不覆盖、预算上限测试。**

```python
def test_explicit_parameters_override_only_defaults():
    from cloud.agents.common.training.conditions import merge_defaults
    assert merge_defaults({"count": 2}, {"count": 3}) == {"count": 3}
```

同一测试模块另用 `validate_definition` 对 `repeat.count=21`、`max_duration_s=601`、同设备 parallel 断言 `ValueError`，错误含精确 JSON path。运行 Task 1–2 测试，预期通过。
- [ ] **Step 5: 提交。** `git commit -m "feat: validate trainable conditions parameters and workflows"`。

### Task 3: 原教导存储增加修订、历史与并发保护

**Files:**
- Modify: `cloud/agents/common/teachings.py`
- Create: `cloud/agents/common/training/storage_checks.py`

**Interfaces:** 产出 `replace_revision / history_for`；沿用 `SSM_TEACHINGS_DIR`、`list_for/remember`。版本冲突抛 `ValueError("revision_conflict")`；API 在 Task 4 映射 409。不覆盖旧文件，不自动启用旧未绑定教导。

- [ ] **Step 1: 新增原话保留、旧版本不可覆写测试。**

```python
import pytest
from cloud.agents.common import teachings

def test_revision_conflict_keeps_both_original_and_current(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_TEACHINGS_DIR", str(tmp_path))
    item = teachings.remember("go2", "礼貌", "看到人", "打招呼", "看到人打招呼")
    revised = teachings.replace_revision("go2", item["teaching_id"], space_id="main",
        expected_revision=1, changes={"behavior": "先停稳再打招呼"})
    assert revised["revision"] == 2
    with pytest.raises(ValueError, match="revision_conflict"):
        teachings.replace_revision("go2", item["teaching_id"], space_id="main",
            expected_revision=1, changes={"behavior": "覆盖"})
    assert teachings.history_for("go2", item["teaching_id"], space_id="main")[0]["source_text"] == "看到人打招呼"
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/storage_checks.py -q`；预期缺接口失败。
- [ ] **Step 3: 用分区文件锁保护 read-modify-write，原子写入当前指针和历史。**

```python
# 在已有分区写锁内执行；写失败时保留原文件。
if item["revision"] != expected_revision:
    raise ValueError("revision_conflict")
previous = {key: value for key, value in item.items() if key != "history"}
updated = {**item, **changes, "revision": expected_revision + 1}
updated["history"] = [*item.get("history", []), previous]
```

继续原子替换并 fsync；增加与现有 checkpoint 一致的 fcntl 分区锁，不能仅依赖线程锁。对外写字段白名单，服务内部 activation 更新不增加教学 revision。保留既有 record 数量上限，单条历史上限 100；满后拒绝新修订并提供明确原因，不静默删除历史。

- [ ] **Step 4: 补充多进程同 revision 仅一方成功、损坏文件显式报错、restore 创建新 revision、重复确认只写一次测试。** 运行 Task 1–3 测试。
- [ ] **Step 5: 提交。** `git commit -m "feat: version teaching definitions with atomic history"`。

### Task 4: 草案确认、加载生效与授权训练投影 API

**Files:**
- Create: `cloud/agents/common/training/{service,projection,service_checks}.py`, `cloud/api/training.py`
- Modify: `cloud/api/{main,teachings}.py`

**Interfaces:** 实现 §3 的 service/projection 接口及 §4 中草案、activation、history、restore、device training、internal loaded 路由。run/respond/cancel 路由在 Task 7 接入；未实现前不广告对应服务。

- [ ] **Step 1: 写差异投影失败测试与 API 契约测试。**

```python
from cloud.agents.common.training.projection import diff_definitions

def test_preview_names_the_exact_parameter_change():
    before = {"defaults": {"count": 3}}
    after = {"defaults": {"count": 1}}
    assert diff_definitions(before, after) == [
        {"op": "replace", "path": "/defaults/count", "before": 3, "after": 1}]
```

API 测试使用 FastAPI dependency_overrides 注入管理员/访客和临时存储；确认前断言 executor 调用数为 0；访客写返回 403，过期草案返回 410，旧 revision/能力 hash 返回 409。

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/service_checks.py -q`；预期失败。
- [ ] **Step 3: 实现 API 单写草案文件与版本状态，添加确认请求模型。**

```python
from pydantic import BaseModel, ConfigDict, Field

class ConfirmProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)

def activation_view(desired: str, loaded: bool) -> dict:
    if desired == "disabled":
        return {"status": "paused"}
    return {"status": "active" if loaded else "pending"}
```

`activation_view` 定义在 projection.py；真实 active 还必须通过 hash/全体参与者加载检查。草案保存在同教导持久根的 `_proposals` 目录，API 唯一写入，带空间/会话/TTL；确认写入 history 并关联 proposal_id。`load_effective` 每次执行核对当前有效版本；支持的 kind 尚未完成时返回 pending/error 原因，不能激活空执行能力。`acknowledge_revision` 只允许对应服务身份确认，普通浏览器参数不能充当加载证明。

确认和运行的写入分离：API 只写教学配置/许可及 desired_status，Graph 通过原 checkpoint 写运行状态。所属智能体的已有 worker 在启动及事件处理前检查新 revision；变更通知仅提示重载，不能作为执行授权。每次执行在分区 revision 锁内核对当前启用版本并完成派发预登记，建立“此次派发先于/后于撤销”的明确顺序；释放锁后传输失败仍按原账本核对，不重新申请一次新动作。

- [ ] **Step 4: 实现有效投影、SSE 更新及重连初始快照。** 用 service_checks 验证原话仅管理员可读，普通调用者得到适用条件及阻止原因；设备物理 state 不因投影写入而改变；旧 ACK 不激活新版本，跨设备缺任一 ACK 保持 pending。
- [ ] **Step 5: 运行 Task 1–4 测试并提交。** `git commit -m "feat: preview confirm and project versioned device training"`。

### Task 5: Go2 与灯统一执行边界接入条件和参数

**Files:**
- Modify: `cloud/go2/{state,service}.py`, `cloud/go2/tools/executor.py`, `cloud/esp32/tool_executor.py`, `cloud/api/intents.py`, `cloud/agents/steward/tools/__init__.py`, `cloud/peers/gateway.py`
- Create: `cloud/agents/common/training/execution_checks.py`
- Test: `cloud/go2/{state_life,action_gateway,command_request}_checks.py`, `cloud/esp32/capability_chain_checks.py`

**Interfaces:** 消费 `load_effective / apply_transition_training`；向所有现有结果增加可选 `training_refs / blocked_transition_id`。实际调用上下文由可信入口提供，Go2State 仍不做文件 I/O。

- [ ] **Step 1: 参数化测试聊天、按钮、管家、同伴、自主请求，同一 guard 均不产生新派发。** 测试使用同一条约束固定假事实，分别调用现有入口，检查返回原因而非仅 mock 校验函数调用。

```python
from cloud.agents.common.training.conditions import apply_transition_training

def test_guard_refusal_keeps_requested_parameters_and_reason():
    record = {"teaching_id": "tb_guard", "revision": 2, "definition": {
        "kind": "guard", "target": {"unit_id": "go2", "transition_id": "activity.rest.wake.active"},
        "scope": {"origins": ["user_delegated"], "modes": [], "activity_required": False},
        "condition": {"fact": "state.connected", "op": "eq", "value": True}}}
    result = apply_transition_training([record], record["definition"]["target"], {},
        {"context.origin_kind": {"value": "user_delegated", "ts": 100, "max_age_s": 5},
         "state.connected": {"value": False, "ts": 100, "max_age_s": 5}}, now=100)
    assert result["allowed"] is False
    assert result["teaching_refs"] == [{"id": "tb_guard", "revision": 2}]
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/execution_checks.py -q`，预期入口一致性断言失败。
- [ ] **Step 3: 在已有执行锁/去重之后、预登记和副作用之前加入最终判断。**

```python
decision = apply_transition_training(effective["records"], target, request_params, facts, now=now)
if not decision["allowed"]:
    return {"result": "blocked", "error": "training_condition_unsatisfied",
            "reasons": decision["reasons"], "training_refs": decision["teaching_refs"]}
params = decision["params"]
```

上段各变量由执行器从当前 Card、有效版本和事实生成，不能由模型填写。Go2 生活控制在取得租约/停止其他任务前完成校验；在 runtime 锁内使用刚计算的事实/版本令牌验证并 commit，拒绝过期令牌。灯保留边缘 ISM 最终确认。基础反馈、自动恢复、StopMove/急停不可被教导阻止。

- [ ] **Step 4: 增加跨入口预设优先级、同 ID 重试不因默认参数变化重派、启用新版本与动作并发不漏检、拒绝不接管任务测试。** 请求显式参数保留原指纹；查询历史结果不套用新默认值。执行判断是会话相关的，不能把某用户阻止结果广播成所有人的全局 `available_actions`。
- [ ] **Step 5: 运行本任务与原能力链路测试并提交。** `git commit -m "feat: enforce trained transitions at shared device boundaries"`。

### Task 6: 编译顺序、分支、重复和受限智能选择为 LangGraph

**Files:**
- Create: `cloud/agents/common/training/{compiler,runtime,compiler_checks}.py`
- Modify: `cloud/agents/common/training/check_fixtures.py`

**Interfaces:** 实现 `compile_workflow / start_training` 的非等待部分；运行 state 必须包含 `run_id, teaching_id, teaching_revision, owner_unit_id, status, step_id, iteration, receipts, completed_steps, consumed_event_ids, waiting, revision, deadline`。`runtime.start_training` 注入这些字段与可信上下文，不接受浏览器提供物理结果。

- [ ] **Step 1: 测试第一步未确认时不得调用第二步。**

```python
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.training.compiler import compile_workflow
from cloud.agents.common.training.check_fixtures import workflow

@pytest.mark.asyncio
async def test_unknown_receipt_does_not_advance(tmp_path):
    calls = []
    async def execute(call, state):
        calls.append(call)
        return {"result": "unknown", "request_id": state["step_id"]}
    async def choose(options, state):
        return options[0]["id"]
    spec = workflow([
        {"unit_id": "go2", "skill_id": "go2_sport", "params": {"cmd": "Hello"}},
        {"unit_id": "esp32_desk_led", "skill_id": "blink_light", "params": {"count": 1}}])
    graph = compile_workflow(spec, execute=execute, read_facts=lambda: {}, choose=choose,
        checkpointer=InMemorySaver(), ledger=FileEffectLedger(tmp_path))
    result = await graph.ainvoke({"run_id": "r1", "teaching_id": "tb1", "teaching_revision": 1,
        "owner_unit_id": "go2", "status": "running", "revision": 0},
        {"configurable": {"thread_id": "training:test:r1"}})
    assert len(calls) == 1
    assert result["status"] == "unconfirmed"
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/compiler_checks.py -q`；预期失败。
- [ ] **Step 3: 按 definition 节点创建 StateGraph 节点和条件边。**

```python
from langgraph.graph import StateGraph, START, END
from cloud.agents.common.training.models import TrainingRunState

# 每个节点函数由该节点的校验后声明构造，不使用自写逐节点 while 循环。
builder = StateGraph(TrainingRunState)
for node_id, node_fn in compiled_nodes.items():
    builder.add_node(node_id, node_fn)
builder.add_edge(START, entry)
for source, targets, route in conditional_routes:
    builder.add_conditional_edges(source, route, targets)
for terminal in terminals:
    builder.add_edge(terminal, END)
graph = builder.compile(checkpointer=checkpointer)
```

`compiled_nodes/entry/conditional_routes/terminals` 由 compiler 对已验证 definition 构造。图的入口初始化未提供的计数、receipt、等待及截止时间字段，生产身份与期限仍由 runtime 注入，不能信任外部请求。skill 节点使用现有 effect ledger 预登记；稳定 ID 包含 run/node/iteration，unknown 查询原设备账本且不重发。decision 节点调用现有角色模型的结构化输出，仅可返回声明候选 ID；模型失败保持原状态并产生明确结果。repeat 计数在 Graph state 持久化；预算取定义、会话、设备三者最小值。

- [ ] **Step 4: 测试真实 ok 才推进、branch unknown、重复精确次数、模型返回未声明选项、节点重入不重发、已有 ledger 无回执不派发。** 生产调用使用 FileCheckpointSaver 的运行锁与同步 durability，不使用测试的内存 saver。
- [ ] **Step 5: 提交。** `git commit -m "feat: compile taught behavior into existing LangGraph runtime"`。

### Task 7: 持久等待、事件恢复、中断与版本失效

**Files:**
- Create: `cloud/agents/common/training/{events,event_checks}.py`
- Modify: `cloud/agents/common/training/{runtime,compiler,service}.py`, `cloud/agents/common/run_index.py`, `cloud/api/training.py`, `cloud/api/main.py`, `cloud/orchestrator/main.py`

**Interfaces:** 实现 `normalize_event / deliver_event / restore_waits / resume_training / cancel_training`，接入 §4 run/respond/cancel 路由。wait 信息 `{wait_id,event_id?,where,deadline,timeout_target}` 保存在 checkpoint；只有所属服务注册恢复处理器。

当前 FileRunIndex 没有训练运行枚举接口：为 `put_if_absent` 增加可选 `metadata: dict | None = None`（旧调用不变），仅保存 `{kind:"training", space_id, owner_unit_id, teaching_id, teaching_revision}` 路由元数据；增加 `list_training_runs(self, agent_id: str, space_id: str) -> list[dict]`，逐条加读锁读取并按归属过滤。运行状态和等待内容仍只在 checkpoint 内，索引不另存流程进度。

- [ ] **Step 1: 使用可控时钟测试同一个 wait 的回应和超时竞争。** 两个事件并发投递，运行只能恢复一次；旧 run/step/epoch 回执不推进。normalize_event 的最小测试：

```python
import pytest
from cloud.agents.common.training.events import normalize_event

def test_text_message_cannot_claim_physical_completion():
    with pytest.raises(ValueError, match="untrusted_completion"):
        normalize_event({"kind": "action.completed", "values": {"result": "ok"}},
                        {"kind": "chat", "unit_id": "go2"})
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/event_checks.py -q`；预期失败。
- [ ] **Step 3: 把等待独立为无硬件副作用节点。**

```python
from langgraph.types import interrupt, Command

def wait_node(state: dict) -> dict:
    event = interrupt(state["waiting"])
    return {**state, "resume_event": event, "waiting": None}

# deliver_event 在运行锁内核对 wait_id、期限、来源和 consumed_event_ids 后：
await graph.ainvoke(Command(resume=event), config, durability="sync")
```

`graph/config` 从现有 run index + pinned teaching revision 恢复；生产使用前先校验事件，不能原样信任用户 JSON。使用现有 API event loop `call_later` 或灯事件队列投递期限事件；注册键为 run_id/wait_id。MQTT callback 仅验证/入队，不等待 Graph。支持 fake clock 的计时适配器，测试不得真实睡眠。

- [ ] **Step 4: 实现撤销/修订的派发栅栏与重启恢复。** 新动作派发前复核当前 revision；配置变化将旧运行置 cancelling，stop 仅作用于仍持有的任务。重启从 index 找 waiting/unknown，查询原回执；过期流程不补跑。回应有唯一 ID 和授权，重复请求只返回原结果。

版本栅栏以预登记为线性化点：修订/撤销之后禁止新的旧版本预登记，之前登记的任务按在途处理并核对/停止。为“撤销恰好发生在预登记与传输之间”增加确定性屏障测试；不得报告“已停止”直到真实结果支持。
- [ ] **Step 5: 验证无阻塞等待、重启不重发、取消后迟到完成不推进、取消不停止后来任务，运行 Task 6–7 测试并提交。** `git commit -m "feat: resume and cancel taught workflows from durable events"`。

### Task 8: 灯训练接入与有界提醒流程

**Files:**
- Create: `cloud/agents/lamp/workflows/training.py`, `cloud/agents/lamp/training_checks.py`
- Modify: `cloud/agents/lamp/workflows/{reactive,runtime}.py`, `cloud/esp32/{service,tool_executor}.py`

**Interfaces:** 灯适配定义 `async execute_training_step(call:dict,state:dict)->dict`，只调用现有 `execute_physical`；传感器事件归一化后进入 `start_training`。原 reactive Graph 继续决定未被教导明确约束的灯光行为。

- [ ] **Step 1: 写提醒流程验收测试，模拟三个 BLINK 回执与 PWA 回应。**

```python
def test_repeat_ids_allow_intentional_repetition_without_duplicate_delivery():
    from cloud.agents.lamp.workflows.training import training_effect_id
    first = training_effect_id("run1", "blink", 0)
    assert first == training_effect_id("run1", "blink", 0)
    assert first != training_effect_id("run1", "blink", 1)
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/lamp/training_checks.py -q`；预期失败。
- [ ] **Step 3: 在灯适配器定义稳定 ID 并连接现有执行器。**

```python
from hashlib import sha256

def training_effect_id(run_id: str, node_id: str, iteration: int) -> str:
    return "task_" + sha256(f"{run_id}:{node_id}:{iteration}".encode()).hexdigest()[:32]
```

已确认流程内的不同 iteration 通过该路径执行，不经过普通 reactive `_cooldown` 的同参数 300 秒抑制；同一 iteration 仍由账本去重。此区别只对有效训练运行开放，不取消普通自主冷却、manual/reactive 或让位检查。计时到期只发事件，不自己调灯；颜色仍由明确用户参数或原灯模型确定，不新增情境→颜色硬编码表。

- [ ] **Step 4: 完成案例 C 与声音触发测试。** 断言 manual 不自动开始；让位期间不派发；第二轮可按训练重复；重复传感器事件不重开流程；回应取消后不闪；自然完成恢复按 edge 原 BLINK_DONE 规则；用户中途改灯时不强行恢复旧颜色。
- [ ] **Step 5: 运行灯训练、compiler/event、原灯能力链路测试并提交。** `git commit -m "feat: teach bounded lamp reminders and event responses"`。

### Task 9: Go2 探索节奏、互动插入和有限失败分支

**Files:**
- Create: `cloud/agents/go2/workflows/training.py`, `cloud/agents/go2/training_checks.py`
- Modify: `cloud/agents/go2/workflows/free_explore.py`, `cloud/go2/mode/free_explore.py`, `cloud/go2/navigation/navigator.py`, `cloud/go2/service.py`

**Interfaces:** Go2 适配定义 `async execute_training_step(call:dict,state:dict)->dict`；现有 `_navigate` 增加可选 `speed_limit` 并传至已支持该参数的 navigator。探索循环使用 `explore_segment`、`observe`、`stop_confirmed` 三种**已有能力适配步骤**，声明归 Task 1 目录，不新增运动命令。

- [ ] **Step 1: 对 A/B/E 分别建立失败测试，先验证已有速度参数传递。**

```python
from cloud.agents.go2.workflows.training import segment_speed

def test_segment_speed_alternates_within_existing_limits():
    assert segment_speed([0.12, 0.20], completed_segments=0, upper_bound=0.30) == 0.12
    assert segment_speed([0.12, 0.20], completed_segments=1, upper_bound=0.30) == 0.20
    assert segment_speed([0.12, 0.20], completed_segments=2, upper_bound=0.30) == 0.12
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/go2/training_checks.py -q`；预期失败。
- [ ] **Step 3: 实现参数选择与现有导航衔接，不使用固定时间直行替代探索。**

```python
import math

def segment_speed(speeds: list[float], *, completed_segments: int, upper_bound: float) -> float:
    if not speeds or completed_segments < 0:
        raise ValueError("invalid_segment_profile")
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= upper_bound for v in speeds):
        raise ValueError("invalid_speed_limit")
    return speeds[completed_segments % len(speeds)]
```

实际最小/最大速度在派发前按 navigator 既有常量再校验，不能采用示例数字作为设备通用范围。完成段计数保存在探索会话的训练 state；段目标仍由现有 frontier 选择；速度作为上限，不宣称精确速度或跑步。停下观察使用现有停稳确认后 observe，观察间隔为软件等待。

- [ ] **Step 4: 接入人出现→停稳→Hello→重查许可→继续探索。** 使用已有人出现事件，按训练冷却，不做身份识别。训练只在既有探索被启用并持有任务时介入，休息/停止/用户接管立即退出；不得通过普通教学自动打开探索。
- [ ] **Step 5: 接入受阻分支并测试失败类别。** 仅已确认 blocked 且原因允许换目标时从已有候选重选，最多训练规定次数；净空不足、地图过期、unknown/timeout 直接等待/求助，不盲目重试。验证同一 epoch 的任务所有权、停止后旧回执不恢复探索。
- [ ] **Step 6: 运行 Go2 训练与原 state_life/hierarchy/action 回归并提交。** `git commit -m "feat: teach exploration pacing interaction and bounded recovery"`。

### Task 10: 受限训练许可、跨设备接力与并行汇合

**Files:**
- Create: `cloud/agents/common/training/{grants,coordination_checks}.py`
- Modify: `cloud/agents/common/training/{compiler,runtime,service}.py`, `cloud/peers/{gateway,inbound,policy}.py`, `cloud/space/execution.py`

**Interfaces:** 实现 `authorize_training_step(reference,call,context)` → `{allowed,reason,teaching_id,revision,step_id}`；reference 仅含服务端生成的 run/step 引用。现有已签名请求结构增加可选 training 引用，校验签名覆盖所有新增字段；普通调用的 policy 原样保留。

- [ ] **Step 1: 测试确认过的精确步骤可执行，伪造参数/目标/版本不能扩权。**

```python
def test_a_run_reference_is_not_an_authorization_by_itself():
    from cloud.agents.common.training.grants import authorize_training_step
    result = authorize_training_step(
        {"run_id": "missing", "step_id": "blink"},
        {"unit_id": "esp32_desk_led", "skill_id": "blink_light", "params": {"count": 1}},
        {"space_id": "main", "origin_kind": "agent_autonomous"})
    assert result["allowed"] is False
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/coordination_checks.py -q`；预期失败。
- [ ] **Step 3: 从已确认 revision/活跃 run 解析精确 step，逐接收端核验。**

```python
def matches_fixed_call(expected: dict, actual: dict) -> bool:
    keys = ("unit_id", "skill_id", "params")
    return all(expected.get(key) == actual.get(key) for key in keys)
```

该函数定义在 grants.py；对于允许范围参数逐字段按已确认 limits 校验，并重新验证现有工具 schema。训练许可不授予角色、持久化、人员库或任意命令权限；设备执行器仍检查当前模式/所有权/状态。过期会话不能启动新人工流程；已启用自主教导使用明确持久训练许可，不保存旧 admin token。

- [ ] **Step 4: 实现 D 的顺序接力，测试 accepted/timeout/unknown 不能触发下一设备。** 结果绑定 run/step/request；两个设备任务 ID 分离但保留共同 trace。不能用用户看到的活动流文本作为控制事件。
- [ ] **Step 5: 实现 F 的不同设备 parallel/join，使用 LangGraph 分支和聚合。**

```python
# 已验证恰好两个不同设备分支；各分支写独立 receipt key。
builder.add_edge("parallel_start", "branch_left")
builder.add_edge("parallel_start", "branch_right")
builder.add_edge(["branch_left", "branch_right"], "join")
```

Graph 使用带 reducer 的 receipts 字段避免并发写冲突；join 仅全部真实 ok 才通过。分支失败发取消给本流程仍持有的其他任务，等待其明确结果，不谎称物理效果已回滚。同设备并行在验证阶段拒绝；等待另一设备时不占用全设备锁。

- [ ] **Step 6: 验证部分生效不启动、撤销停止后续步骤、原 peer policy 回归、并行一方失败与重启恢复；提交。** `git commit -m "feat: compose trained device workflows with scoped step grants"`。

### Task 11: 三个角色的自然语言教学与多轮修订

**Files:**
- Create: `cloud/agents/common/training/{tools,language_checks}.py`
- Modify: `cloud/agents/steward/{graph,prompts}.py`, `cloud/agents/steward/tools/__init__.py`, `cloud/agents/lamp/{graph,prompts}.py`, `cloud/agents/lamp/tools/schemas.py`, `cloud/agents/go2/graph.py`, `cloud/agents/go2/tools/declarations.py`, `cloud/api/intents.py`

**Interfaces:** 共用 `propose_training`；工具参数 `{operation,target,teaching_id?,expected_revision?,source_text,definition}`。selection 作为经 API 校验的上下文，不由模型伪造。`_public_view` 增加可训练目录摘要，按需读取详细 schema。

- [ ] **Step 1: 使用脚本化模型输出测试“教学、修改、临时命令”的分流和工具契约。**

```python
import pytest
from cloud.agents.common.training.tools import propose_training

@pytest.mark.asyncio
async def test_teaching_only_prepares_a_proposal():
    calls = []
    async def api_call(path, payload, context):
        calls.append(path)
        return {"proposal_id": "p1", "conflicts": []}
    result = await propose_training({"operation": "disable", "teaching_id": "tb1",
        "expected_revision": 2, "source_text": "取消刚才的教导"},
        {"session_id": "s1", "actor": "admin", "space_id": "main"}, api_call)
    assert calls == ["/api/teachings/proposals"]
    assert result["proposal_id"] == "p1"
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/language_checks.py -q`；预期失败。
- [ ] **Step 3: 注册 LangChain 严格工具和提示规则。**

```python
TEACHING_GUIDANCE = (
    "用户教长期习惯时先读取目标的可训练状态、转换、参数和已有教导。"
    "只提交修改草案，不在确认前控制设备。"
    "用户说刚才、慢一点、改成之后时，优先定位当前选中步骤和已有版本。"
    "多个匹配时询问具体对象；不能创建互相竞争的新规则。"
    "只能组合已声明能力；不把时间移动说成精确步数，不承诺新增跑步或识别能力。"
    "未提到长期习惯的单次命令沿用现有执行路径。"
)
```

提示由各角色已有 prompts 组装，设备角色保持人格与当前 Graph。`save_rule` 兼容旧简易草案，新训练共用新工具；模型无确认工具，confirmed 字段不得来自模型。自然语言“更智能地选择”编译为有限 decision 选项与明确约束，未确认的新行为不能在运行时追加。

- [ ] **Step 4: 建立固定语料矩阵。** 包含四类训练、六组案例每组至少一次追改、撤销、选中连线、同名多个流程、能力缺失、离线保存、访客请求；脚本模型测契约，真实模型人工回放记录草案正确性与澄清次数，不把脚本模型通过宣称为自然语言质量通过。
- [ ] **Step 5: 跑语言与既有三角色回归并提交。** `git commit -m "feat: teach and revise device behavior through existing agent graphs"`。

### Task 12: 连线点击查看详情与直接教学入口

**Files:**
- Create: `app/src/pages/TransitionInspector.jsx`, `app/src/hooks/useTraining.js`, `scripts/check_training_frontend.cjs`
- Modify: `app/src/pages/{FsmGraph,HierarchicalFsmBoard,FsmDevicePage}.jsx`, `app/src/utils/stateMachine.js`, `app/src/hooks/useSendIntent.js`, `app/index.html`, `app/sw.js`, `app/styles/app.css`

**Interfaces:** `TransitionInspector({selection,projection,onTeach,onExecute,onClose})`；`useTraining(unitId)` 返回 `{projection,runs,error,refresh,confirm,activate,respond,cancel}`，只用 apiClient。复用 FsmGraph 的 `onInspectTransition` 和当前节点控制；既有安全停止保留直接动作。

- [ ] **Step 1: 在现有 Node vm/React harness 模式下新增点击不派发测试。**

```javascript
const assert = require('node:assert/strict');
const selected = [];
const dispatched = [];
const edge = {id:'lighting.OFF.CMD_BLINK.BLINKING', src:'OFF', dst:'BLINKING',
  trigger:'CMD_BLINK', action:'BLINK', label:'闪烁', params:{count:1}};
const props = {
  states:['OFF','BLINKING'], current:'OFF', transitions:[edge],
  onInspectTransition: edge => selected.push(edge.id),
  onTransition: edge => dispatched.push(edge.id),
};
const tree = renderGraph(props);
clickEdge(tree, edge.id);
assert.deepEqual(selected, [edge.id]);
assert.deepEqual(dispatched, []);
```

检查脚本内实现 `renderGraph(props)` 与 `clickEdge(tree,id)`，按 `scripts/check_go2_frontend.cjs` 的 React hook harness 遍历实际元素事件；不能仅调用 props 回调代替组件交互测试。最终断言点击连线时 selected 收到一次 ID、dispatched 为空；点击存在唯一合法直接转换的目标节点后收到一次派发。补充条件不满足、多条可用转换、当前节点、反馈未确认、拖拽与触屏实际命中测试。详情“执行一次”仅为辅助入口。

- [ ] **Step 2: 运行** `node scripts/check_training_frontend.cjs`；预期未接入训练详情及投影的断言失败；既有节点控制与连线说明语义应保持通过。依赖复用现有前端脚本环境，缺少包时使用已有工具目录的 NODE_PATH，不为 PWA 引入构建工程。
- [ ] **Step 3: 保留节点控制，扩展连线的训练详情。**

```jsx
<button type="button" onClick={() => onTeach({
  unit_id: selection.unit_id,
  region_id: selection.region_id,
  transition_id: selection.transition_id,
})}>教它改变这一步</button>
<button type="button" disabled={!projection.can_execute}
  onClick={() => onExecute(selection)}>执行一次</button>
```

点击状态节点请求合法直接转换；点击连线只查看条件与训练痕迹，不触发动作。disabled 边也允许查看详情；图拖拽超过阈值不触发点击。显式参数控件记录 dirty 字段，只提交用户真正输入的参数，未编辑字段不阻断已训练 defaults。后端仍最终判断。

- [ ] **Step 4: 接入 selection 到现有聊天、移动端底部面板与键盘焦点。** 面板开关不重置画布；触控≥44px；错误能重试；安全停止无需进详情。更新 index 脚本依赖顺序和 sw 缓存版本。
- [ ] **Step 5: 运行新脚本和** `node scripts/check_go2_frontend.cjs` **并提交。** `git commit -m "feat: inspect and teach device state transitions from the graph"`。

### Task 13: 图上修改预览、训练徽标与版本确认体验

**Files:**
- Create: `app/src/components/TrainingPreview.jsx`
- Modify: `app/src/{app.jsx,ApiClient.js}`, `app/src/hooks/{useTraining,useSendIntent}.js`, `app/src/pages/{FsmGraph,TransitionInspector}.jsx`, `app/src/components/ActivityFeed.jsx`, `app/index.html`, `app/styles/app.css`, `scripts/check_training_frontend.cjs`

**Interfaces:** `TrainingPreview({proposal,onConfirm,onCancel,onLocate})`；确认只提交 proposal_id + expected_revision。基础边 ID 与 diff target 联合定位。

- [ ] **Step 1: 测试参数变化可见、预览不执行、保存与生效文案分开。** 使用模拟 proposal 的 `/defaults/count` 3→1；模拟 confirm 返回 pending，随后 SSE active；断言相应文本逐步变化而非立刻“已学会并生效”。
- [ ] **Step 2: 运行** `node scripts/check_training_frontend.cjs`，预期新交互缺失失败。
- [ ] **Step 3: 实现可读差异组件和状态文案。**

```jsx
const activationText = {
  pending: '已保存，等待设备加载',
  active: '训练已生效',
  paused: '训练已暂停',
  error: '暂时无法生效',
};
<div className="training-change" data-path={change.path}>
  <span>{change.label}</span>
  <del>{change.beforeText}</del><span aria-hidden="true"> → </span>
  <strong>{change.afterText}</strong>
</div>
```

`change` 来自服务端 diff 的可读投影，不把 JSON path、工具 ID 暴露为主要用户文案。首次创建与撤销分别显示新增/移除，不伪造 before 值。复杂连线默认一枚摘要徽标，详情展开全部条件；隐藏颜色转换通过已有参数入口定位。

- [ ] **Step 4: 实现 409 冲突保留草案、重新读取差异与刷新恢复。** 连续两次点击确认只产生一个确认操作；保存成功但断网可按 proposal 查询真实状态，不重复创建。多轮修订复用 teaching_id，图展示前后变化。
- [ ] **Step 5: 验证手机长文案、离线保存、能力变化、冲突不丢输入并提交。** `git commit -m "feat: preview and display learned state-machine changes"`。

### Task 14: 行为子图、跨设备视图、运行解释与培养记录

**Files:**
- Create: `app/src/pages/TrainingFlow.jsx`, `app/src/components/TrainingHistory.jsx`
- Modify: `app/src/pages/{HierarchicalFsmBoard,FsmDevicePage,RulesPage}.jsx`, `app/src/hooks/useTraining.js`, `app/src/utils/stateSnapshot.js`, `app/src/components/ActivityFeed.jsx`, `app/index.html`, `app/styles/app.css`, `scripts/check_training_frontend.cjs`

**Interfaces:** `TrainingFlow({definition,run,onSelectStep})`、`TrainingHistory({records,onLocate,onRevise,onRestore,onActivate})`；新增纯函数 `mergeTrainingSnapshot(current,incoming)` 放现有 stateSnapshot.js，按配置/run 各自游标合并。

- [ ] **Step 1: 测试乱序训练事件不回退，训练阶段不改写身体状态。**

```javascript
const prior = { projection_revision: 5, runs: { r1: { revision: 8, status: 'waiting' } } };
const incoming = { projection_revision: 4, runs: { r1: { revision: 7, status: 'running' } } };
const merged = mergeTrainingSnapshot(prior, incoming);
assert.equal(merged.projection_revision, 5);
assert.equal(merged.runs.r1.status, 'waiting');
assert.equal(merged.body_state, undefined);
```

- [ ] **Step 2: 运行** `node scripts/check_training_frontend.cjs`；预期缺合并函数失败。
- [ ] **Step 3: 绘制行为阶段容器、条件结果、循环计数及跨设备依赖。**

```jsx
<section className="training-flow" aria-label={`${definition.name}的行为流程`}>
  <p>当前阶段：{run?.step_label || '尚未运行'}</p>
  <p role="status">{run?.decision_text || '等待触发'}</p>
  {run?.waiting?.responses?.map(response =>
    <button key={response.id} onClick={() => onSelectStep({
      type: 'respond', wait_id: run.waiting.wait_id, response: response.id,
    })}>{response.label}</button>)}
</section>
```

流程完整布局根据定义的节点/边生成；parallel 清楚显示两个设备及汇合，循环以容器表示不展开 20 份节点。物理当前态继续从原设备流读取，不能用 step label 推断。等待回应按钮通过 hook 调 respond API，只有服务端接受后更新状态。

- [ ] **Step 4: 完成历史与运行原因。** 显示原话、修改差异、最近应用、条件不满足/未知、未确认结果；撤销/恢复仍走草案或明确版本写入。普通用户只看授权运行摘要；历史原话依管理员权限。旧 RulesPage 指向同一记录，避免两套编辑器互相覆盖。
- [ ] **Step 5: 浏览器验收 390px/768px/1440px、键盘、减少动画、面板滚动和 SSE 重连。** 在前端脚本增加 `--browser` 模式，复用现有 puppeteer/Chrome 配置；全程模拟网络，禁止自动接真机。运行 `node scripts/check_training_frontend.cjs --browser` 与原脚本并提交。

### Task 15: 六组端到端案例、解释质量与旧教导迁移

**Files:**
- Create: `cloud/agents/common/training/{acceptance_checks,migration_checks}.py`, `scripts/migrate_training_definitions.py`
- Modify: `cloud/agents/common/{teachings,teaching_bindings}.py`, `cloud/api/teachings.py`, `cloud/agents/common/training/service.py`
- Create: `docs/superpowers/plans/training-evidence/README.md`

**Interfaces:** 迁移命令默认 `--dry-run`，`--apply` 只操作指定持久目录并创建备份；旧 v1 binding 转为等价 trigger 定义，自动状态不扩大。数据迁移和技能能力升级分开。

- [ ] **Step 1: 固定六组案例的输入、结构化修改和预期事件轨迹。**

```python
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.training.compiler import compile_workflow
from cloud.agents.common.training.check_fixtures import workflow

@pytest.mark.asyncio
@pytest.mark.parametrize("first_result,expected_units", [
    ("ok", ["go2", "esp32_desk_led"]), ("timeout", ["go2"]), ("unknown", ["go2"])])
async def test_relay_waits_for_actual_completion(tmp_path, first_result, expected_units):
    units = []
    async def execute(call, state):
        units.append(call["unit_id"])
        return {"result": first_result if len(units) == 1 else "ok"}
    async def choose(options, state):
        return options[0]["id"]
    definition = workflow([
        {"unit_id": "go2", "skill_id": "go2_sport", "params": {"cmd": "Hello"}},
        {"unit_id": "esp32_desk_led", "skill_id": "blink_light", "params": {"count": 1}}])
    graph = compile_workflow(definition, execute=execute, read_facts=lambda: {}, choose=choose,
        checkpointer=InMemorySaver(), ledger=FileEffectLedger(tmp_path))
    await graph.ainvoke({"run_id": "relay", "teaching_id": "tb_relay", "teaching_revision": 1,
        "owner_unit_id": "go2", "status": "running", "revision": 0},
        {"configurable": {"thread_id": "training:test:relay"}})
    assert units == expected_units
```

本测试验证真实编译子图的分支结果；另外经真实 API/角色工具/执行适配器跑完整链路，仅替换模型和设备传输，收集 `go2.dispatched → go2.ok → lamp.dispatched → lamp.ok` 证据。每组覆盖初次教学、修改一次、撤销一次；A/B/E 使用 fake 感知/导航结果，标记 simulated。

- [ ] **Step 2: 编写迁移失败测试并运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/acceptance_checks.py cloud/agents/common/training/migration_checks.py -q`。
- [ ] **Step 3: 实现旧 binding 的等价转换和单入口接管。**

```python
def migrated_status(old: dict) -> str:
    return "enabled" if old.get("execution_status") == "enabled" else "disabled"
```

`migrated_status` 定义在迁移脚本；实际迁移先校验能力和原语义。旧 unbound 仍是仅记住；旧 enabled 在新 adapter 加载确认前暂停旧执行器的对应消费，采用每条记录单一 `executor_version` 切换，确保旧 candidates 与新事件入口不双触发。无法等价迁移保留原记录与解释，不能猜测动作。dry-run 报告每条转换/跳过原因，apply 幂等，二次运行无新版本。

- [ ] **Step 4: 运行语料回放与完整故障矩阵。** 包含保存后设备离线、旧版本确认、断线重连、unknown、重复事件、等待中取消、参数冲突、撤销后迟到结果、跨设备部分失败、manual/reactive、用户让位和急停。记录真实模型提案正确率/需澄清项，不设置缺少基线的虚假百分比；任何语义错误不得通过自动近似规则掩盖。
- [ ] **Step 5: 将软件证据写入 training-evidence/README.md 并提交。** 写明测试命令、环境、结果、模拟/真机区别及未完成项。`git commit -m "test: verify trainable workflows and migrate legacy teachings"`。

### Task 16: 集成回归、交付文档与分阶段上线检查

**Files:**
- Modify: `README.md`, `protocol/topics.md`, `docs/superpowers/plans/training-evidence/README.md`
- Update: 本计划各任务复选框仅在真实完成后更新

**Interfaces:** 文档说明 Card 能力字段、签名训练调用引用和事件来源；不新增另一条 MQTT 控制总线。管理员维护指南明确保存/加载/可执行的区别及版本回退。

- [ ] **Step 1: 在隔离临时持久目录运行相关软件测试。**

```bash
uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training cloud/agents/lamp/training_checks.py cloud/agents/go2/training_checks.py -q -o python_files=*_checks.py
uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/go2/state_life_checks.py cloud/go2/definition_contract_checks.py cloud/go2/action_gateway_checks.py cloud/go2/command_request_checks.py cloud/go2/hierarchy_interface_checks.py cloud/go2/hierarchy_integration_checks.py cloud/esp32/capability_chain_checks.py cloud/peers/capability_chain_checks.py cloud/peers/result_recovery_checks.py cloud/cards/region_contract_checks.py -q
node scripts/check_training_frontend.cjs --browser
node scripts/check_go2_frontend.cjs
```

不在测试中加载生产 `.env`、连接 broker、访问人脸服务或派发设备命令。测试 fixture 显式设置临时 teaching/checkpoint/effect 目录并替换传输；如现有测试需要额外环境，记录并按现有约定隔离，不删校验来使旧 mock 通过。

- [ ] **Step 2: 验证两个完整用户旅程。** 从空教导开始完成 C、D；再修改、暂停、重启服务、恢复版本和撤销。核对图上变化、运行原因和设备模拟派发三者一致。截图包含手机连线详情、图上差异、探索子流程和跨设备汇合。
- [ ] **Step 3: 写部署前后的具体核对清单。**

```text
部署前：备份教导分区；迁移 dry-run；记录基础 Card hash 和固件版本。
部署后：刷新能力声明；核对已加载版本；先开启单设备训练，再开启跨设备许可。
回退前：暂停新训练、取消仍持有的任务、确认无待派发步骤。
回退：恢复兼容代码和备份；v2 无法识别的记录保持停用；不重放旧运行。
```

使用现有服务管理方式，不把软件检查自动连接为部署命令。真机验收在设备就绪时单独执行；按六组案例记录真实回执，不要求新增感知/运动能力。

- [ ] **Step 4: 完成用户体验验收。** 首次教学无需用户写规则表达式；教学后能在一处看到变化；后续一句话修改同一教导；每次不行动都有事实原因；等待期间可取消；基础控件和急停可达；不出现“已经生效”但执行器未加载的状态。
- [ ] **Step 5: 更新证据与状态，提交相关文档。** `git commit -m "docs: document trainable state-machine rollout and evidence"`。

## 6. 需求覆盖核对表

| 设计要求 | 实施任务 | 验收证据 |
|---|---|---|
| 基础 FSM 与训练行为阶段区分 | 1、6、14 | 稳定引用、物理状态不被流程覆盖 |
| 转换条件与所有入口一致 | 2、5 | 五类入口拒绝一致，急停/反馈不受影响 |
| 参数默认、限制和多轮修改 | 2、5、11、13 | 显式覆盖规则、冲突、同一 teaching_id 新 revision |
| 事件触发、等待、重复与中断 | 6–8 | 回应/超时竞争、去重、取消后不重发 |
| 自由探索节奏、礼貌互动、受阻求助 | 9 | A/B/E 事件轨迹与导航约束 |
| 跨设备顺序与并行 | 10 | D/F 真实完成关联、失败分支与权限回归 |
| 智能性保留在 LangGraph | 6、9、11 | 受限选择与现有角色模型，无第二循环 |
| 连线可点击且训练痕迹可见 | 12–14 | 点击不派发、差异与徽标、手机/键盘截图 |
| 版本生效与撤销 | 3、4、7、13 | pending/active、旧 ACK、执行前版本栅栏 |
| 旧教导与无构建 PWA 兼容 | 12、15、16 | 单消费迁移、脚本加载及缓存更新 |
| 无新增感知/运动能力 | 1、9、11、15 | 目录来源、拒绝虚构技能、六组能力内案例 |

## 7. 计划自审与核对重点

本计划准备阶段已核对当前代码入口、现有教导数据模型、两个设备执行边界、Card/前端结构、LangGraph 文件检查点、灯冷却与跨设备许可差异。未实施上述功能，未运行计划中的新增测试。

用户核对时重点看以下产品选择：

1. 自然语言与点击连线都进入同一教学链路，确认的是具体状态机差异。
2. 行为流程可以展开，但不会让基础物理状态图无限膨胀。
3. 首期已包含条件、参数、顺序/分支/等待/有界重复和不同设备的并行汇合。
4. 训练可持续修正、暂停与撤销，并能解释实际执行或未执行原因。
5. 六组示例均限定在已有能力内；身份、精确步数、新跑步动作不属于交付承诺。

核对通过后按任务实施；可用 executing-plans 在当前会话逐项执行，或在用户选择后使用 subagent-driven-development 分工并逐项评审。本次停在计划文档交付，供用户核对。
