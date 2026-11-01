# LangGraph 迁移对比记录

> 状态：三角色代码入口已在当前未提交工作区切换到 LangGraph；本报告记录代码迁移，不代表实机或发布验收通过。

## 迁移前基线

- 基线 commit：`917c952d3ff8acc41fc86ecfe7ebfdb3b1f86071`。
- 测量口径：`git ls-tree -r --name-only HEAD cloud` 中的 `.py` 文件；逐文件用 `git show HEAD:<path>` 读取。物理行数为 `splitlines()` 行数；有效行数排除空行与整行 `#` 注释；测试行数为 `/tests/` 或 `test_*.py` 的物理行数。此口径不读取工作区运行数据、虚拟环境、文档与生成文件。迁移后使用相同口径，角色目录改按目标目录统计。

| 范围 | Python 文件 | 物理行 | 非空非注释行 | 测试行 |
| --- | ---: | ---: | ---: | ---: |
| 管家 Agent：`orchestrator/agentcore/`、`orchestrator/multitask.py` | 17 | 817 | 734 | 0 |
| 灯 Agent：`esp32/agentcore/`、`esp32/{agent,multitask}.py` | 20 | 1,487 | 1,316 | 0 |
| Go2 Agent：`go2/agentcore/`、`go2/multitask.py` | 24 | 3,347 | 2,827 | 0 |
| 共享 Agent：`agent_runtime/` | 8 | 750 | 649 | 0 |
| 外层 API、设备、空间、日志、测试等 | 110 | 15,989 | 13,481 | 4,269 |
| `cloud/` 合计 | 179 | 22,390 | 19,007 | 4,269 |

迁移前主聊天调用链：`api/intents.py → MQTT → orchestrator/main.py → orchestrator/multitask.py → agent_runtime/{loop,store,replies,progress}.py → 设备执行器`。灯与 Go2 设备页分别由各自 router 进入 `multitask.py`，自主路径在 `agentcore/mode/`。MQTT Card 仍是公开能力和在线状态依据，ESP32 动作必须以 MQTT `result` 为完成依据，Go2 由 HTTP/运动任务账本判定。PWA 经原 API/SSE 事件显示。

当前保存的人格资料基线（只记录提示词哈希，不复制用户文案到报告）：

| 角色 | 文件 | revision | prompt SHA-256 | traits |
| --- | --- | ---: | --- | --- |
| 管家 | `cloud/orchestrator/data/personality/profile.json` | 2 | `12ea506bcabd851b8e8774b982ecdb2f47567a16c43aa8e729dda116b90e8805` | warmth=70, clarification_tendency=50 |
| 灯 | `cloud/esp32/data/personality/profile.json` | 2 | `67f7ee54d491d326d3fb229c3a6345a0247718b44f306c315b882e3220bdc6ea` | social_initiative=70, adjustment_sensitivity=50 |
| Go2 | `cloud/go2/data/personality/profile.json` | 10 | `bc9d9fd75356c7537bf90a75c37fd733039ac4671c165041d2e9721954682a69` | curiosity=83, extraversion=45, boldness=65 |

本地记忆文件数量基线：灯 `cloud/esp32/data/memory/` 为 JSON 1、JSONL 0、Markdown 0；Go2 `cloud/go2/data/memory/` 为 JSON 1、JSONL 6、Markdown 6。计数只含各目录下的对应扩展名，不代表事件条数。角色资料与记忆目录仍由现有代码读写，本轮尚未执行数据迁移。

审计时本地 `logs/session-*.jsonl` 共 107 个文件、21,095 条可解析事件、0 条损坏行；其中带 `agent_id` 的 Go2 8,415、灯 4,132、管家旧标识 `orchestrator` 3,763 条，其余 4,785 条没有 `agent_id`。这是工作区已有日志加本轮隔离验证时的现场计数，不能当作两版同条件覆盖率比较。原 CLI 只按 session/trace 检索且长字段超过 8,192 字符截断；本轮已增加按 agent/run/task 检索，模型与工具文本在本地 0600 JSONL 中经脱敏后完整保存。

## 迁移后代码快照（当前工作区）

统计口径与基线相同：只计 Git 已跟踪文件和当前未被忽略、可提交的新文件（`git ls-files -c -o --exclude-standard cloud`），并排除已删除文件；角色目录按 `cloud/agents/{steward,lamp,go2,common}/` 分类。仓库另有被 `.gitignore` 忽略的本地旧测试，虽然本次 `pytest cloud` 会运行它们，但不计入下表的可交付代码量。工作区尚未提交，数字可能随最后回归修复变化。

| 范围 | Python 文件 | 物理行 | 非空非注释行 | 测试行 |
| --- | ---: | ---: | ---: | ---: |
| 管家 Agent：`agents/steward/` | 17 | 1,057 | 927 | 254 |
| 灯 Agent：`agents/lamp/` | 24 | 1,776 | 1,527 | 293 |
| Go2 Agent：`agents/go2/` | 32 | 3,074 | 2,587 | 894 |
| 共享 Agent：`agents/common/` | 17 | 1,892 | 1,630 | 626 |
| 外层 API、设备、空间、日志、测试等 | 135 | 18,789 | 15,861 | 4,324 |
| `cloud/` 合计 | 225 | 26,588 | 22,532 | 6,391 |

按可交付文件口径，物理行比基线增加 4,198 行（18.7%），非空非注释行增加 3,525 行（18.5%）。测试物理行增加 2,122 行（49.7%）；扣除测试行后的生产物理行从 18,121 增至 20,197，增加 2,076 行（11.5%）。本地另有 46 个被忽略的旧测试文件、共 5,017 行；完整工作树为 271 个 Python 文件、31,605 行。目录整体差值包含同时进行的 API、设备与测试扩展，不能归因于 Graph 框架本身。当前 `git diff --shortstat` 为 114 个已跟踪文件、增加 697 行、删除 6,961 行；`git diff --numstat` 的逐文件明细同样只统计已跟踪文件，遗漏全部新建未跟踪的 `cloud/agents/` 等文件，因此不能用其删除量推算净代码量。

新共享实现中，`agents/common/checkpoint.py` 为 317 行，`runner.py` 为 336 行，`effects.py` 为 135 行；日志接线 `cloud/observability/events.py` 为 142 行。它们分别承担文件检查点、逐步 Graph、动作去重和事件脱敏/记录，均已计入上表，不是额外代码。

迁移后的文件与入口对应关系：

| 原入口/目录 | 当前去向 |
| --- | --- |
| `orchestrator/multitask.py`、`orchestrator/agentcore/` | `orchestrator/main.py` 调 `agents/steward/graph.py`；人格、工具、公开 skill 进入 `agents/steward/` |
| `esp32/agent.py`、`esp32/multitask.py`、`esp32/agentcore/` | `esp32/service.py` 调 `agents/lamp/graph.py`；传感器自主逻辑进入 `agents/lamp/workflows/`；设备状态、MQTT 派发与工具执行留在 `esp32/` |
| `go2/multitask.py`、`go2/agentcore/` | `go2/router.py` 调 `agents/go2/graph.py`；人格、记忆、自主工作流进入 `agents/go2/`；视觉、模式和运动执行适配留在 `go2/` |
| `agent_runtime/` | 共用循环、检查点、动作去重、回复与进度进入 `agents/common/`；配置存储进入 `personality/`，JSON 文件工具进入 `storage/`；旧目录已删除 |

主链路为 `api/intents.py → MQTT → orchestrator/main.py → agents/steward/graph.py → agents/common/runner.py → 设备工具回执`。灯与 Go2 设备页分别由 `esp32/service.py` 和 `go2/router.py` 进入对应 Graph。MQTT Card 仍为公开能力与在线状态来源。ESP32 物理动作由 MQTT `result` 核对；Go2 数据面仍为 HTTP/SSE。PWA 使用原 API/SSE 协议。

日后维护路径示例：新增管家工具需在 `agents/steward/tools/` 声明与执行，并在 `agents/steward/graph.py` 的工具 schema 中接线；公开设备 skill 要同步更新设备 Card 声明与 `agents/steward/tools/card.py` 的校验；调整 Go2 特质在 `agents/go2/personality/profile.py` 定义并在同目录 `behavior.py` 消费。三类改动分别保留公开能力、执行边界和人设契约。

## 旧记录核对与备份

切换前核查 `cloud/{orchestrator,esp32,go2}/data/runs/` 的 124 个旧 JSON 记录，全部 deadline 已过期；其中 `ok` 53、`error` 31、`partial` 13、`timeout` 4、`needs_clarification` 5、`accepted` 6、`executing` 11、`blocked` 1。旧记录没有作为新 Graph 检查点恢复。原始目录保留，另有权限 0600 的归档 `cloud/data/migration/legacy-runs-2026-11-01.tar.gz` 和同目录 manifest（忽略目录，未纳入 Git）。归档 SHA-256 为 `94050c7ace692671dedfb498505fe85367c57fdbb2f18bd2691c1eb5ea59e0eb`。该核对只说明旧运行 deadline 已过，不能证明外部设备没有仍在执行的动作。

## 框架与代码验证

- `uv.lock` 锁定 `langchain==1.4.7`、`langgraph==1.2.18`、`langgraph-checkpoint==4.3.1`；未引入数据库驱动。Ark 兼容探针验证了原生单次 tool call；结构化输出须显式使用 `method="function_calling"`，默认路径曾返回 HTTP 400。这是兼容性小样本，不能证明长时间在线稳定性。
- `FileCheckpointSaver` 通过基础 put/get/list、pending writes、跨进程写入、interrupt 恢复和官方基础一致性 5/5。运行索引按 agent/session 隔离；文件动作账本在执行前持久预留稳定 step ID，重开时不重复下发，无法核对回执则停止后续步骤并报告未知。
- 三个对话 Graph 共用逐步决策、工具结果观察和最终回复契约。测试覆盖多工具拒绝、澄清续接、重复 run、传感器自主模式门、MQTT 任务 ID 和活动流重复事件；Go2 自主动作及社交工作流另有重复运行去重测试。检查点中的工具返回仍需按真实设备隐私数据复核。
- 迁移后的三个角色 prompt SHA-256 与上表基线一致，profile revision 仍为 2/2/10；人格与记忆数据文件路径保持。原本的 107 个 `logs/session-*.jsonl` 和 21,095 条可解析事件属于工作区历史样本，不能作为新旧版本同条件性能比较。
- 尚无切换后三角色、同一真实任务条件下的脱敏逐步日志样本；现有日志探针与测试事件仅证明事件字段和脱敏逻辑，不作为端到端执行样本。
- 管家与共享定向测试 62 passed；灯定向测试 77 passed；Go2 定向测试 317 passed、1 skipped。最终全量命令 `UV_CACHE_DIR=/tmp/ssm-uv-cache uv run --no-sync python -m pytest cloud -q` 得到 **645 passed、1 skipped（104.54 秒）**。`uv lock --check` 与 `git diff --check` 通过；Python 源码中没有旧 `agent_runtime`、三角色旧 `agentcore` 或旧 `multitask` 导入。迁移中曾得到 694 passed、1 skipped，但那是在旧入口删除前，不能当成最终回归结果。

## 尚无现场验收

本工作区 `make ps` 未发现运行中的 API、编排器和设备服务。本轮没有 ESP32/Go2 实机回执、管理员 PWA 截图、SSE 现场回放，也没有完成服务重启后的端到端演练。因此当前结论仅为代码迁移和隔离测试；上线判断仍须在真实服务与设备上核对物理回执、跨角色委派、传感器 reactive、用户让位窗口和活动流。当前工作区未提交，不能填写“迁移后 commit”。
