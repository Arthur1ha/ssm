# 当前对话事实与排查（2026-11-16）

浏览器展示的是原运行的有界公开事实，不是日志或模型提示词。共享 `progress_facts.py` 负责调用、参数、结果与关联字段的公开投影，沿用 `agent_progress`、`agent_reply`、`run_stage` 与现有 MQTT/SSE 通道。`stage` 仍可用于忙碌、追问和终结控制；没有实际内容时不新增聊天记录。原模型的公开决策说明可缺省，不为进展另调解释模型。

排查时先核对 `trace_id/run_id/parent_run_id/step_id/task_id`，再看源事件身份、模型轮次和顺序。`agent_id` 是执行者，`target_unit_id` 是目标；消息送达、活动成员加入和物理动作回执分别判定。工具外层成功可能包含成员失败，不能只看外层 `ok`。历史缺失证据不会重新生成说明；迟到回执只补原任务，不重新派发动作。

`make logs` 的最新会话和 `make task-trace` 默认追踪识别浏览器页作用域 `chat_` 和登录作用域 `ses_`，排除命名测试样本及 system 日志。`make trace-task ID=<session_id>`、`ID=<trace_id>` 仍支持明确查询，附加 `FOLLOW=1` 可固定目标并追踪新事件，避免新页切换排查目标。CLI 回归使用临时日志，不连接设备。

以下是改造前的历史基线，不表示当前运行入口或本次验收结果。

# 任务日志现状基线（2026-10-26）

实施前的一条灯到 Go2 请求可从灯运行 ID `32e9468268f044ab9b1bbefe4035106c` 和同伴 `request_id=dance-go2-001` 找到部分信息。`logs/api.log` 的 `cloud.agent_runtime.loop` 行只写工具结束与结果；同文件里的 `cloud.peers.gateway` 行写 `needs_clarification`。同一文件有多条 Go2 任务 HTTP 202 轮询，最后返回 200。旧日志没有统一的 `trace_id`、每轮模型输入输出、父子运行 ID 或可核对的最终发送文字，不能从旧记录反推这些字段。

| 来源 | 改造前内容和路由 | 当时大小 |
| --- | --- | ---: |
| `logs/api.log` | `make api` 的 `tee`、API 访问、共用循环与同伴网关 | 443 KiB |
| `logs/esp32.log` | API `FileHandler` 单独写灯 logger | 867 KiB |
| `logs/go2.log` | API `FileHandler` 单独写 Go2 logger，含高频状态和旧图日志 | 7.0 MiB |
| `logs/orchestrator.log` | 后台 stdout；独立进程用 `basicConfig` | 73 KiB |
| `logs/mosquitto.log` | Broker 原始日志 | 186 MiB |

现有 tmux 辅助命令只有 `go2-logs-window`，按窗口名增补 `go2-events` 和 `go2-state`；`make trace` 输出全量 MQTT，`make logs` 分文件 tail/grep。tmux socket 在实施环境内无访问权限，因此无法记录实际会话窗口列表，也不改动任何现有会话。

入口映射：`/api/intents` 在 `cloud/api/intents.py` 接收主聊天并发布 intent；灯设备页调用 `cloud/esp32/router.py` 的 `/{unit_id}/chat`，由 `cloud/esp32/multitask.py` 处理；Go2 设备页调用 `cloud/go2/router.py` 的 `/chat`，由 `cloud/go2/multitask.py` 处理。编排器 MQTT intent 与 peer 在 `cloud/orchestrator/main.py`，逐步循环在 `cloud/orchestrator/multitask.py`。历史日志中的 `[Go2/Prompt]`、`[Go2/Chat/Reply]` 和旧图 Responder 不代表当前回复路径；对应旧入口已删除。

可复用的现有标识是 `run_id`、`step_id`、peer `request_id` 和设备 `task_id`；旧记录没有可信根 `trace_id`、`parent_run_id` 或完整轮次数据。迁移前的 SQLite 文件为灯语义任务 `semantic_tasks.sqlite3`（若存在）、灯/Go2 `data/multitask.sqlite` 和编排器配置路径所指文件。它们承担幂等和恢复语义，须先迁移核对再切换运行入口，旧文件在真实验收后才可清理。

本机切换时未发现运行中的 API、编排器或 Broker；已备份灯与 Go2 的旧 SQLite 文件及附属 WAL/SHM 到各自 `data/legacy_sqlite_backup_2026-10-26/`，再迁移灯 3 个 run/4 个步骤、Go2 15 个 run/2 个步骤。迁移后逐条用 `RunStore.get` 查询核对；原 SQLite 文件仍保留。本机没有灯语义任务库或编排器运行库，部署环境若有这些文件需按 README 的步骤另行迁移。真实灯/Go2 成功与失败任务、tmux socket 及服务重启后的时间线尚未在本环境验证，旧日志在这些验收完成前保留。
