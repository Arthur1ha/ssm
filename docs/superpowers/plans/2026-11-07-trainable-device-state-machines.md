# 用户可训练的设备状态机首版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户通过自然语言修改单设备已有状态转换的条件、动作参数及单事件触发，确认后能在状态机上看见变化，并由设备执行链路真实遵守。

**Architecture:** 现有 LangGraph 生成结构化修改草案，新训练配置按稳定转换 ID 保存；原设备执行器统一检查条件并合并参数，真实回执决定结果。前端共用一份有效定义展示连线痕迹和培养记录。替换旧文字 binding 链路，不创建通用行为编译器或第二个运行时。

**Tech Stack:** Python 3.13、uv、现有 LangChain/LangGraph、Pydantic、FastAPI、原子 JSON 存储、原动作账本、MQTT/HTTP、React 18 无构建 PWA、pytest 与现有浏览器测试工具。

**Spec:** [用户可训练的设备状态机设计](../specs/2026-11-07-trainable-device-state-machines-design.md)。执行前完整阅读本文与设计。

## Global Constraints

- 不新增感知能力或运动能力，只使用已声明并实际接入的设备状态、事件和动作。
- 自然语言理解继续走现有 LangChain / LangGraph；不新增 Agent 循环或通用工作流运行时。
- 首版只做单设备已有转换的条件、参数和单事件触发；不做跨设备流程、新状态或任意多步编排。
- 点击可执行状态节点请求合法转换；点击连线查看条件、参数和训练痕迹，不触发动作。
- 设备当前物理状态只由真实反馈更新；保存、请求已接受、实际完成分别展示。
- 条件对适用范围内的按钮、聊天、管家、同伴和自主入口一致；训练不能放宽基础设备约束。
- 保留 MQTT 控制面、既有 MQTT/HTTP 执行通道、真实回执、用户让位和急停。
- 旧教学链路可以替换删除，不保留旧 binding/API 兼容要求；旧数据归档，不自动迁移或启用。
- 保留 React 18 无构建 PWA；首版复用当前设备页、状态机白板和聊天。
- 本次交付仅更新文档；实施先用隔离存储和模拟传输验证，不部署、不操作真机或生产教导数据。

---

日期：2026-11-07。状态：**首版计划待核对，未实施**。本版取代此前 16 项复杂流程计划；执行范围仅为下列 8 项，不能继续执行旧版的 workflow/parallel/wait/compiler/grants 任务。

当前工作区存在其他设备探索和前端改动。实施前执行 `git status --short`，仅修改本任务必要部分，不能覆盖、暂存或提交其他工作；不使用 `git add .`。任务中的提交是执行阶段的建议边界，本文更新不提交代码。

## 1. 顺序与交付

**产品验收以设计 §1 的两点为准：可编排的智能必须通过状态机改变执行，自然语言造成的变化必须可见、可解释、可追改。** 不再把改次数/冷却作为唯一主展示。“看到人→站立到打招呼”是首要触发示例；“只有主人可唤醒”是准入条件示例，首版先用可信模拟身份验收，不新增识别能力，也不拿管理员角色替代请求者身份。

教学后的前端必须直接定位受影响的边；该边显示用户条件，点击展开原话、条件类型、是否满足和修改/暂停/撤销入口。自动触发与准入限制必须分别验证，不能互相替代。


| 里程碑 | 任务 | 可观察结果 |
|---|---|---|
| P1a：先跑通一条边 | 1–5 | 准入条件及参数闭环；身份条件以可信模拟事实验证；触发教导能生成图上预览 |
| P1b：条件与事件组合 | 6 | 看到人时站立→招手真实派发；灯声音触发复用同一契约，模式/去重正确 |
| 首版交付 | 7–8 | 旧教学退役、完整回归、数据保留与切换说明 |

不实现探索阶段编辑、环境调速、跨设备、多步流程、动态新状态、通用等待和循环。下一阶段边界见设计 §8。

任务按依赖顺序执行；8 项均通过后才可准备部署。独立审查可并行，同文件实施不可并行。每一步按单个测试或修改切分，看到反馈后再继续，不能一次改完所有入口才测试。

## 2. 文件与接口

### 2.1 新增文件

| 文件 | 职责 |
|---|---|
| `cloud/agents/common/training/__init__.py` | 包入口 |
| `cloud/agents/common/training/catalog.py` | 实际设备可训练转换、参数和事件目录 |
| `cloud/agents/common/training/models.py` | 严格定义、草案和确认模型 |
| `cloud/agents/common/training/policy.py` | 条件、冷却、参数和冲突检查，纯函数 |
| `cloud/agents/common/training/store.py` | 配置、草案、版本历史与确认去重 |
| `cloud/agents/common/training/service.py` | 归属校验、提案、确认、加载投影 |
| `cloud/agents/common/training/tools.py` | 三角色共用教学/查询工具的薄适配 |
| `cloud/api/training.py` | 新 API 路由 |
| `app/src/hooks/useTraining.js` | 快照、SSE、确认与暂停/撤销 |
| `app/src/components/TrainingPreview.jsx` | 修改前后预览卡 |
| `app/src/components/TrainingDrawer.jsx` | 共用培养记录抽屉 |
| `app/src/pages/TransitionInspector.jsx` | 连线说明、条件/参数和教学定位 |
| `scripts/check_training_frontend.cjs` | 模拟网络的前端交互检查 |

测试放同目录的 `*_checks.py`，共享构造放 `check_fixtures.py`。不创建通用 compiler/runtime/events/grants 模块，也不创建新动作账本。

### 2.2 修改边界

- `cloud/cards/{schema,validation,builder}.py`、`cloud/go2/{actions,state,card}.py`：稳定边 ID 与真实可训练目录。
- `cloud/go2/tools/executor.py`、`cloud/go2/service.py`、`cloud/esp32/tool_executor.py`：普通动作/生命周期调用的统一检查。
- `cloud/agents/common/effects.py`、`cloud/esp32/physical_tasks.py`：仅在查询接口不足时补完成回执查询；原账本保持权威。
- `cloud/agents/{steward,lamp,go2}/graph.py`、提示词和工具声明/执行分派：注册共用教学工具，不重写对话循环。
- `cloud/go2/{mode/free_explore,service}.py`、`cloud/agents/go2/workflows/free_explore.py`、`cloud/agents/lamp/workflows/{reactive,runtime}.py`、`cloud/esp32/service.py`：既有人出现/声音事件只触发一条已确认转换，并经过所属设备原 Graph 和执行边界；不改探索路径选择或新增动作。
- `cloud/api/{main,intents}.py`：装配新接口、可信 selection 与事件投影。
- 当前 app、FsmDevicePage、FsmGraph、HierarchicalFsmBoard、ActivityFeed、agentMeta、ApiClient、useSendIntent、index、sw、styles：用新确认与培养记录替换旧 UI。

旧文件和所有读取/消费引用在 Task 7 删除；不在本次计划修订中删除。

### 2.3 公共契约

```python
# catalog.py
def catalog_for(card: dict) -> dict: ...
# {unit_id, hash, transitions:[{id,src,dst,action,trainable,params_schema,
# conditions,events}], state_fields, limits}

# policy.py：now 与事实由调用方注入，不在纯函数内取实时状态。
def validate_definition(definition: dict, catalog: dict) -> dict: ...
def conflicts(definition: dict, records: list[dict]) -> list[dict]: ...
def evaluate(records: list[dict], request: dict, facts: dict, *, now: float) -> dict: ...
# request={transition_id,origin,params,trigger_ref?}
# facts={state,state_fresh,requester,last_completed_at,completion_history_known}
# requester 仅服务端验证后填入；缺失或过期为 unknown。
# 返回 {allowed,params,reasons,teaching_refs}；unknown 不能当作允许。

# store.py：只供 API 配置写入；不读取旧教导根。
def list_records(unit_id: str, *, space_id: str) -> list[dict]: ...
def commit(record: dict, *, expected_revision: int, proposal_id: str) -> dict: ...
# expected_revision=0 创建；更新必须精确匹配；重复 proposal 返回原结果。

# service.py
def propose(request: dict, session: dict) -> dict: ...
def confirm(proposal_id: str, expected_revision: int, session: dict) -> dict: ...
def set_enabled(teaching_id: str, enabled: bool, expected_revision: int, session: dict) -> dict: ...
def snapshot(unit_id: str, viewer: dict) -> dict: ...
def acknowledge(unit_id: str, revisions: dict, service_context: dict) -> dict: ...

# tools.py；api_call(path,payload,context,*,method="POST") 为认证客户端薄适配。
async def propose_training(arguments: dict, context: dict, api_call) -> dict: ...
async def list_training(arguments: dict, context: dict, api_call) -> dict: ...
```

声明中的省略号仅表示接口签名，实施不能留下空函数。所有生产边界均经过 models 校验。

另在 policy.py 定义 `training_event_intent(event,records,catalog,facts,now)->dict|None`，供两个设备复用单事件匹配，不创建事件运行时。

定义固定字段：`transition_ids:list[str]、origins:list[str]、conditions:list[{field,op,value}]、cooldown_s:number|null、defaults:dict、trigger:null|{event_id,where,params}`。scope/trigger 语义见设计 §5.2；首版只允许 AND，不能偷偷支持自由表达式。显式参数优先于 defaults；trigger.params 为该事件请求的参数，不是全局默认值。

### 2.4 API

| 方法与路径 | 用途 |
|---|---|
| `GET /api/training?unit_id=...` | 当前空间培养记录，原话仅管理员可读 |
| `POST /api/training/proposals` | 创建/修改/撤销草案；模型不直接确认 |
| `GET /api/training/proposals/{id}` | 同会话恢复草案，30 分钟有效 |
| `POST /api/training/proposals/{id}/confirm` | 用户确认，CSRF、revision/hash、幂等检查 |
| `PUT /api/training/{id}/enabled` | 暂停/启用，管理员、CSRF、expected_revision |
| `GET /api/devices/{unit_id}/training` | 目录、有效训练、加载状态、边上解释 |
| `POST /api/internal/training/{unit_id}/loaded` | 内部服务认证的版本加载确认 |

撤销使用 proposal.operation=remove，确认后保留记录和历史但失效；不是删除生产文件。模型可用 operation=create/update/remove；用户直接暂停无需模型。SSE 使用 `training.proposed / training.updated / training.loaded / training.applied`；配置游标独立于物理 state，事件丢失时重取一次快照，不周期轮询。

## 3. 实施任务

### Task 1: 声明可训练边与最小条件/参数模型

**Files:** Create `cloud/agents/common/training/{__init__,catalog,models,policy,check_fixtures,policy_checks}.py`；Modify `cloud/cards/{schema,validation,builder}.py`、`cloud/go2/{actions,state,card}.py`。

**Interfaces:** 实现 catalog/validate_definition/conflicts/evaluate。check_fixtures 的 `hello_cooldown(seconds)` 返回一条 enabled、当前加载 revision=1 的完整 Hello 教导；真实 transition_id 从 Go2 声明读取。`blink_defaults(count)` 返回灯 BLINK 边组的默认参数教导。它们不复制硬件参数范围。

- [ ] **Step 1: 编写冷却和参数优先级失败测试。**

```python
from cloud.agents.common.training.policy import evaluate
from cloud.agents.common.training.check_fixtures import hello_cooldown, blink_defaults

def test_confirmed_completion_controls_cooldown():
    record = hello_cooldown(60)
    edge = record['definition']['transition_ids'][0]
    request = dict(transition_id=edge, origin='user_delegated', params={})
    facts = dict(state={'body_state':'standing'}, state_fresh=True,
                 last_completed_at={edge:100.0}, completion_history_known=True)
    assert evaluate([record], request, facts, now=130)['allowed'] is False
    assert evaluate([record], request, facts, now=160)['allowed'] is True

def test_explicit_count_overrides_trained_default():
    record = blink_defaults(2)
    request = dict(transition_id=record['definition']['transition_ids'][0],
                   origin='user_delegated', params={'count':5})
    facts = dict(state={'ism':'OFF'}, state_fresh=True,
                 last_completed_at={}, completion_history_known=True)
    assert evaluate([record], request, facts, now=100)['params']['count'] == 5
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/policy_checks.py -q`；预期缺模块/接口失败。
- [ ] **Step 3: 实现严格字段和计算规则。**

```python
# policy.py 的判定片段，实际函数汇总全部适用记录。
last = facts['last_completed_at'].get(request['transition_id'])
if cooldown_s is not None and not facts['completion_history_known']:
    reasons.append({'code':'completion_history_unknown'})
elif cooldown_s is not None and last is not None and now - last < cooldown_s:
    reasons.append({'code':'cooldown', 'remaining_s':cooldown_s-(now-last)})
params = {**defaults, **request['params']}
```

只有真实已知历史且没有完成记录时视为首次允许。条件字段须在目录中，cooldown 首版支持 1–3600 秒并在目录声明。多个同字段默认值冲突拒绝；相同 event/目标重叠首版拒绝。参数范围复用现有 schema。automatic/safety 边不可训练；边 ID 不依赖布局和数组顺序。
- [ ] **Step 4: 补可信主人/非主人/未知准入判定、触发教导不限制无关手动请求、状态未知/过期、非法字段、自动完成边被拒、冲突、边排序不改变 ID 的测试，运行本任务测试及原 Card/动作契约检查。**
- [ ] **Step 5: 审查并提交本任务文件。** `git commit -m "feat: define trainable transition conditions and parameters"`。

### Task 2: 新配置存储、确认 API 与版本加载

**Files:** Create `cloud/agents/common/training/{store,service,service_checks}.py`, `cloud/api/training.py`；Modify `cloud/api/main.py`, `cloud/.env.example`。

**Interfaces:** 实现 §2 store/service/API，`SSM_TRAINING_DIR` 独立目录。新增 fixture `training_record()` 为完整 disabled Hello 记录（revision=1、loaded_revision=null、history=[]）；生成 ID 由服务端负责。

- [ ] **Step 1: 写版本冲突和确认幂等测试。**

```python
import pytest
from cloud.agents.common.training.store import commit, list_records
from cloud.agents.common.training.check_fixtures import training_record

def test_same_proposal_commits_only_once(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    record = training_record()
    first = commit(record, expected_revision=0, proposal_id='p1')
    again = commit(record, expected_revision=0, proposal_id='p1')
    assert again['teaching_id'] == first['teaching_id']
    assert len(list_records(record['unit_id'], space_id='main')) == 1
    with pytest.raises(ValueError, match='revision_conflict'):
        commit(record, expected_revision=0, proposal_id='p2')
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/service_checks.py -q`；预期失败。
- [ ] **Step 3: 实现原子分区写入、历史和 API。**

```python
# 分区 fcntl 文件锁内，先检查 proposal 去重，再检查版本。
if current is not None and current['revision'] != expected_revision:
    raise ValueError('revision_conflict')
if current is None and expected_revision != 0:
    raise ValueError('revision_conflict')
updated = {**validated_record, 'revision': expected_revision + 1,
           'loaded_revision': None}
```

同一次临时文件+fsync+os.replace 写入记录、history 和 confirmed_proposals。新定义修订置未加载，历史不原地覆盖；每设备最多 500 条、单条历史最多 100 版，满后明确拒绝。只允许 API 写配置；原话以可信用户请求为源。能力/hash/归属确认时复核，错误字段给出具体原因。
- [ ] **Step 4: 加载确认与有效版本投影。** 只有内部服务能确认加载；旧 revision ACK 不能激活新版本。暂停/撤销先使旧定义不可派发。snapshot 区分保存、加载、当前可执行原因，隐藏普通用户不该读的原话。
- [ ] **Step 5: 验证访客拒绝写、同空间归属、过期 proposal=410、冲突=409、确认前设备调用为零、损坏文件报错、旧目录不导入；运行测试并提交。** `git commit -m "feat: confirm and persist transition training"`。

### Task 3: 在原执行入口统一应用条件和参数

**Files:** Modify `cloud/go2/tools/executor.py`, `cloud/go2/service.py`, `cloud/esp32/tool_executor.py`, `cloud/agents/common/effects.py`, `cloud/esp32/physical_tasks.py`（后两者只补需要的查询）；Create `cloud/agents/common/training/execution_checks.py`。

**Interfaces:** evaluate 的事实来自真实状态与原账本；派发结果附 `training_refs / reasons / transition_id`。不能让前端自行判定后直接绕过服务器。

- [ ] **Step 1: 使用现有执行器 mock transport 写五类入口一致性测试。** 覆盖 user/button、设备聊天、管家、peer、自主五种可信调用上下文；主人准入条件使用独立的已验证 requester 事实，代转必须保留原请求者，缺失则 unknown。训练冷却未结束时实际 transport 调用均为零，原权限可更严格。补 BLINK 未传 count 得到 2、明确传 5 保留 5 的断言。
- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/execution_checks.py -q`；预期原代码忽略训练条件/默认参数而失败。
- [ ] **Step 3: 按以下顺序接入现有锁和账本。**

```text
可信上下文/原请求幂等检查
→ 真实当前态解析稳定转换
→ 读取当前有效训练
→ 显式参数 + defaults，严格设备 schema 校验
→ 原条件 AND 训练条件
→ 执行锁内复核配置 revision 与状态
→ 原账本预登记 → 原传输 → 原真实回执
```

不能在 defaults 合并前因缺少可默认参数就拒绝，也不能把 UI/模型自动补的 Card 默认值当作用户明确指定。固定转换选择参数仍保留。保存原请求和实际派发参数供去重，训练修订不让同一个请求重新执行。
- [ ] **Step 4: 冷却完成事实和重启。** 从原账本成功结果投影最近完成时间，明确成功结束时间来源；accepted/executing/timeout 不能开始完成冷却。按适用 transition 和来源索引，事件教导另按 teaching_id 作用域过滤；原账本为权威，查询索引可重建。索引落后/无法重建为 unknown，不默认已冷却。新教导启用时建立明确历史基线，重启不能清空；只统计该教导首次生效后的受约束执行，修订冷却时长继续使用其既有完成事实。
- [ ] **Step 5: 覆盖重复请求、并发第二请求、撤销与派发竞争、重启冷却、反馈自动边和急停不受影响；运行执行测试及设备原能力链路回归并提交。** `git commit -m "feat: enforce learned conditions at device execution boundaries"`。

### Task 4: 三角色通过原 LangGraph 提出与修改教导

**Files:** Create `cloud/agents/common/training/{tools,language_checks}.py`；Modify 各角色 graph/prompts/tool schemas/execute 分派，`cloud/api/intents.py`。

**Interfaces:** propose_training `{operation,teaching_id?,base_revision?,unit_id,definition}`，可信 source_text/selection 从上下文带入；list_training `{unit_id}` 返回已有教导与目录摘要。模型无确认工具。

- [ ] **Step 1: 测试工具只创建 proposal，不执行或保存生效记录。**

```python
import pytest
from cloud.agents.common.training.tools import propose_training

@pytest.mark.asyncio
async def test_teaching_requests_preview_only():
    calls = []
    async def api_call(path, payload, context, *, method='POST'):
        calls.append((method,path))
        return {'proposal_id':'p1'}
    result = await propose_training(
        {'operation':'remove','teaching_id':'t1','base_revision':1,'unit_id':'go2'},
        {'actor':'admin','space_id':'main','source_text':'取消刚才的限制'}, api_call)
    assert calls == [('POST','/api/training/proposals')]
    assert result['proposal_id'] == 'p1'
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/language_checks.py -q`；预期缺工具失败。
- [ ] **Step 3: 注册严格工具与提示规则。**

```python
GUIDANCE = (
    '长期教导先读取已有转换与可修改条件/参数，只提出修改草案。'
    '定位唯一目标后少追问；多个目标须说明差别并澄清。'
    '慢一点、隔久一点等建议必须给出明确数值供用户确认。'
    '这次闪五次是临时命令；以后默认两次才是长期参数。'
    '改成半分钟修改原教导，不能另建竞争教导。'
    '没有能力时说明缺项，不能声称已经学会。'
)
```

保留角色人格与原 Graph；删除旧 save_rule 的模型暴露，旧协议剩余引用由 Task 7 清理。普通即时动作继续走原工具。selection 由 API 验证归属；模型不能伪造可信来源或授权。
- [ ] **Step 4: 固定 A/B/D 语料矩阵（A 此时验证提案，真实事件执行由 Task 6 补齐）：初次教学、追改、撤销、同名歧义、临时命令、非法参数、离线保存；测试脚本模型输出及实际 schema，记录真实模型人工回放结果。**
- [ ] **Step 5: 运行语言与三角色回归，审查提交。** `git commit -m "feat: teach transition policies through existing agent graphs"`。

### Task 5: 原状态机页面形成完整教学闭环

**Files:** Create `app/src/hooks/useTraining.js`, `app/src/components/{TrainingPreview,TrainingDrawer}.jsx`, `app/src/pages/TransitionInspector.jsx`, `scripts/check_training_frontend.cjs`；Modify 原 app/设备页/白板/ActivityFeed/ApiClient/useSendIntent/agentMeta/index/sw/styles。

**Interfaces:** `TrainingPreview({proposal,onConfirm,onCancel,onLocate})`、`TransitionInspector({selection,projection,onTeach,onClose})`、`TrainingDrawer({unitId,onLocate,onRevise,onSetEnabled,onRemove})`；共用 hook 返回 `{catalog,records,proposal,load,confirm,setEnabled,remove,error}`。

- [ ] **Step 1: 写真实触屏交互失败测试。** 沿用 check_go2_frontend.cjs 的实际 React/浏览器 harness 和模拟 commandBus：点击连线仅打开详情，点击合法节点恰好派发一次；确认教导不派发硬件；冷却阻止时显示剩余时间。先测试新增抽屉/预览不存在而失败，不能把已有节点控制改回预览。
- [ ] **Step 2: 运行** `node scripts/check_training_frontend.cjs --browser`；依赖使用现有 NODE_PATH/Chrome 配置，不引入 PWA 构建流程。
- [ ] **Step 3: 实现修改预览和状态文案。**

```jsx
<div className="training-change">
  <strong>{proposal.transitionLabel}</strong>
  <p>{proposal.changeText}</p>
  <p>{proposal.scopeText}</p>
  <button onClick={() => onConfirm(proposal.proposal_id, proposal.base_revision)}>
    确认教会它
  </button>
  <button onClick={onCancel}>取消</button>
</div>
```

changeText/scopeText 来自服务端可读投影，不能单凭模型自由文本解释已执行内容。展示保存/待加载/已加载；配置 SSE 不覆盖物理状态。刷新按 proposal_id 恢复，不重复确认；409 保留输入并提示重新核对。
- [ ] **Step 4: 实现连线标记与培养抽屉。** 每条边最多一枚摘要标记；展开原话、条件、参数、适用范围和当前限制。主聊天/设备聊天共用一张确认组件；抽屉支持定位、暂停、追改和撤销，不新增任意流程编辑器。去掉旧 pendingRule 确认 UI 与旧 binding 表单，Task 7 做剩余引用检查。
- [ ] **Step 5: 完成 B/D 的模拟全旅程和 A 的提案/图上条件展示，检查 390px/768px/1440px、键盘与拖动误触；更新缓存、运行新脚本和原节点控制回归并提交。** `git commit -m "feat: preview learned changes on the device state graph"`。

此时 P1a 闭环必须可演示：一句话设置准入条件或默认参数，确认、查看图上差异、模拟请求被限制/参数改变、追改、撤销；A 的真实事件派发由 Task 6 完成。未通过不能继续扩范围。

### Task 6: “看到人就打招呼”的真实事件链路，再复用到灯

**Files:** Modify `cloud/go2/{service,mode/free_explore}.py`, `cloud/agents/go2/workflows/free_explore.py`, `cloud/agents/lamp/workflows/{reactive,runtime}.py`, `cloud/esp32/{service,tool_executor}.py`, `cloud/agents/common/training/{policy,catalog,service,check_fixtures}.py`；Create `cloud/agents/go2/training_checks.py`, `cloud/agents/lamp/training_checks.py`。只有发现原权限不支持已确认动作时才修改 `cloud/space/execution.py` 的精确校验。

**Interfaces:** policy 的 `training_event_intent` 返回 `{teaching_id,revision,transition_id,event_id,params}` 或 None。check_fixtures 的 `person_greeting_fixture()` 返回真实 Go2 Card 目录、已加载站立→招手教导、时间戳=100 的有效 person_presence 事件与站立 facts；`lamp_sound_fixture()` 返回对应 OFF→BLINKING、count=2 的声音教导。两个 fixture 均返回 `{catalog,records,event,facts}`。

- [ ] **Step 1: 写 A/C 的单事件匹配失败测试。**

```python
from cloud.agents.common.training.policy import training_event_intent
from cloud.agents.common.training.check_fixtures import person_greeting_fixture, lamp_sound_fixture

def test_person_event_targets_the_existing_standing_greeting_edge():
    f = person_greeting_fixture()
    intent = training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=100)
    assert intent['transition_id'] in f['records'][0]['definition']['transition_ids']
    lying = {**f['facts'], 'state':{**f['facts']['state'], 'body_state':'lying'}}
    assert training_event_intent(f['event'], f['records'], f['catalog'], lying, now=100) is None

def test_sound_event_uses_its_own_parameters():
    f = lamp_sound_fixture()
    intent = training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=100)
    assert intent['params']['count'] == 2
```

- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/go2/training_checks.py cloud/agents/lamp/training_checks.py -q`；预期缺匹配实现失败。
- [ ] **Step 3: 接入机器狗已有人物检测结果到原自主 Graph。**

```text
有效检测：没人→有人
→ 生成一次 person_presence 出现事件
→ 匹配已加载的站立→招手教导
→ 原 Go2 Graph 接收绑定动作意图
→ 原执行器重新检查站立/模式/版本/准入条件
→ Hello 派发与真实结果
→ 连线显示“看到人条件命中”和真实状态变化
```

持续有人不因每帧重复触发，未知/过期帧不重置“人已离开”；重新确认离开后再出现才是新事件。此软件适配只读取已有检测结果，不增加检测器或识别具体人物。匹配事件可请求绑定动作或说明为何不执行，不允许追加起身/走近等未教导动作。休息/躺卧/模式不允许时不补跑、不自动开启探索。普通手动 Hello 不因缺少看到人事件而被拒绝。
- [ ] **Step 4: 灯复用同一匹配契约。** 声音来源/时间戳形成稳定事件关联；当前 OFF 才匹配，原 lamp Graph 最多提出一个绑定动作。重复事件不重复派发，冷却/让位/过期事件不排队；匹配教学时不再另起普通调光，未匹配维持原自主判断。
- [ ] **Step 5: 精确校验自动动作授权与冷却。** 对已确认单设备 teaching 引用、固定目标和参数作服务端校验，不冒充 admin、不开放一般自主权限。教导明确冷却用于该教导动作，避免原普通自主同参冷却吞掉已经确认的行为；其他普通冷却保持。用户追教“任何招手都间隔一分钟”时，独立普通 guard 同时约束手动和事件请求。
- [ ] **Step 6: 完成 A 的首要端到端验收，再跑 C。** 真实 runtime→Graph→模拟传输，不只测 matcher。验证收到人事件只有一个 Hello 请求、未知姿态零请求、持续有人不重复、手动仍可用、追改/暂停/撤销、动作未确认不显示完成；图上条件、原话和当前命中原因一致。运行两个设备训练及原权限测试并提交。 `git commit -m "feat: execute user-taught transitions from existing device events"`。

### Task 7: 删除旧教学链路，保留有用基础

**Files:** Delete `cloud/api/teachings.py`, `cloud/agents/common/{teachings,teaching_bindings}.py`, `app/src/pages/RulesPage.jsx`, `app/src/components/RulesDrawer.jsx`；Modify 所有旧 import/route/tool/Card/UI 引用；Create `cloud/agents/common/training/retirement_checks.py`。

**Interfaces:** 最终只有新训练配置和消费入口。旧 API 返回 404；旧数据不进入新列表、条件匹配或动作派发。新培养抽屉替换旧规则管理入口。

- [ ] **Step 1: 编写退役失败检查，列出旧生产引用。**

```bash
rg -n 'save_rule|pending_rule|onPendingRule|teaching_candidates|teaching_bindings|go2_list_teachings|teachings_endpoint|/api/teachings|RulesPage|RulesDrawer' cloud app
```

retirement_checks 用实际文件存在性与生产模块扫描检查上述符号（排除 *_checks.py 和历史文档）；隔离旧 enabled 数据目录，新列表应为空，投递事件无训练派发。测试完整 app 装配的旧 GET/POST/binding 路由均为 404，禁止真实服务 lifespan 连接硬件。
- [ ] **Step 2: 运行** `uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training/retirement_checks.py -q`；预期残留引用失败。
- [ ] **Step 3: 删除旧文件和挂接。**

```python
# 最终 app 装配仅保留新教学 router。
from cloud.api.training import router as training_router
app.include_router(training_router)
```

清理管家旧 schema/提示词/执行分支，灯旧 teaching_candidates，Go2 的特定 Hello 教导消费/Graph teaching 分支，go2_list_teachings 工具，Card 的旧 teachings_endpoint。autonomous_skills 经全仓核对仅服务旧教学时删除；普通 skills/observations 保留。Go2/ESP32 memory 的 taught 字段和资产列表改接新抽屉，其他记忆人格字段不动。
- [ ] **Step 4: 清理旧 UI 和缓存入口。** 删除 pendingRule/handleConfirmRule、teaching_saved 旧卡片、旧 binding 编辑器、index 脚本和 #rules 页面注册；菜单改开新抽屉。旧缓存或旧 pending_rule 请求只能提示刷新/重新教学，不能写入新记录。保留现有节点控制、普通聊天和设备设置。
- [ ] **Step 5: 运行退役与新旧基础回归，确认旧原文件内容未变并提交。** `git commit -m "refactor: retire legacy teaching bindings and interfaces"`。

### Task 8: 首版端到端验收与切换说明

**Files:** Create `cloud/agents/common/training/acceptance_checks.py`, `docs/superpowers/plans/training-evidence/README.md`；Modify README 中相关入口及本计划复选框（仅按实际完成更新）。

**Interfaces:** A 为首要触发场景，D 为准入逻辑/界面模拟场景，B/C 验证参数与设备复用；P2–P4 不属于本次完成标准。

- [ ] **Step 1: 运行隔离软件测试。**

```bash
uv --cache-dir /tmp/ssm-uv-cache run python -m pytest cloud/agents/common/training cloud/agents/lamp/training_checks.py cloud/agents/go2/training_checks.py -q -o python_files=*_checks.py
node scripts/check_training_frontend.cjs --browser
node scripts/check_go2_frontend.cjs --browser
git diff --check
```

原 Go2/灯/Card/权限测试按当前仓库有效路径运行，不能引用已被其他工作删除的测试路径或为通过而回退它们。fixture 隔离配置、checkpoint/effect 目录和网络；不加载生产 .env 或连接设备。
- [ ] **Step 2: 对 A/B/C 及 D 的可信模拟事实跑完整用户旅程。** 自然语言→草案→确认→加载→图上痕迹→模拟执行结果→追改→暂停/撤销；重启后配置与冷却有效。真实模型回放另记正确性和澄清次数，不把脚本模型通过当成自然语言质量通过。
- [ ] **Step 3: 编写发布切换说明。**

```text
代码准备：旧教学引用已清理，软件验收通过；当前仍不部署。
后续发布前：停止旧服务接收教学和继续派发，核对在途动作，停止旧 worker。
归档：复制实际旧教导目录，记录文件校验和及数量；原文件不改写。
完整切换：API、各角色 worker、Card 与 PWA 使用同一新版本；新训练目录为空。
验收：旧接口 404；基础控制可用；确认一条新教导后才验证其行为。
回退：停止新教学派发并核对在途结果；旧归档不挂载为 enabled 运行数据。
```

不编写自动迁移器，不把测试命令接成生产发布。真机动作验收等待后续明确授权和设备就绪。
- [ ] **Step 4: 记录实际结果与剩余项，审查并提交文档。** `git commit -m "docs: record transition-training MVP validation"`。

## 4. 覆盖与自审

| 需求 | 任务 | 证据 |
|---|---|---|
| 智能性可编排：触发和准入均改变行为 | 1–6 | A 的人物事件派发；D 的主人/非主人/未知模拟判定 |
| 用户中心：对话变化在状态图可感知 | 4–6 | 受影响边定位、用户条件/原话、命中/阻止原因、追改撤销 |
| 参数修改与跨设备类型复用 | 1–6 | B/C 闪烁参数与声音事件 |
| 条件与事件组合 | 6 | A/C 的源状态、模式、冷却、让位和事件去重矩阵 |
| 同一教导连续追改 | 2、4–6 | teaching_id 不变、revision 增加、旧定义失效 |
| 节点控制、连线查看训练痕迹 | 5 | 真实鼠标/触屏点击，不用直接调用回调替代命中测试 |
| 所有适用入口一致 | 3 | 五类入口及重复/并发请求结果 |
| 真实回执与冷却持久化 | 3、8 | accepted 不算完成，重启不清零 |
| 旧教学退役且数据保留 | 7–8 | 无旧生产引用、旧目录不触发、原文件未改 |
| 不扩展为复杂工作流 | 全部 | 无新状态、compiler/runtime/grants/跨设备流程 |

实施前再次核对当前仓库。当前探索代码正在变化，不因本计划改写 search/navigation；P2 另行设计接入实际 `search.py`。本版没有新增运行结果或预填完成复选框。
