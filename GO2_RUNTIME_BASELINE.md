# Go2 运行时回归基线（2026-10-22）

本文件记录实施 `GO2_AGENT_RUNTIME_IMPROVEMENT_PLAN.md` 时实际观察到的基线与模拟验收；未连接实机或公网。

## 实施前

命令：`UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest cloud/go2/tests -q`

结果：225 passed、5 failed、1 skipped。五项原有失败分别为 `test_connection.py::test_move_velocity_sends_wireless_controller`、`test_fsm.py::test_action_state_is_interruptible`、`test_fsm.py::test_moving_has_no_self_move`、`test_occupancy_grid.py::test_obstacle_appears_in_grid`、`test_occupancy_grid.py::test_obstacle_inflation`。前 3 项的旧断言与当前 FSM 动作约束冲突，后 2 项与现有栅格解析结果冲突；本计划没有修改这些底层规则。

实施前超时链（代码实测）：设备页 `CommandBus` 对 chat 等待 60 秒、其他指令 30 秒；API 内部 Go2 HTTP 等待 25 秒；编排器普通 HTTP skill 等待 10 秒、导航 30 秒；Go2 WebRTC 单动作等待最多约 20 秒。旧链路中外层超时后，Go2 任务可能继续执行，且不能按任务 ID 查询最终状态。

实施后超时链：设备页初始 POST 最多等待 30 秒，随后按同一任务 ID 查询（最多 90 秒）；API 内部请求仍是 25 秒，Go2 默认同步窗口 8 秒、服务端截止时间 90 秒；编排器 Go2 HTTP 总预算 95 秒，超过同步窗口时查询同一任务 ID。`executing` 是服务端任务未结束，`accepted` 是设备只确认收到命令。取消或服务端截止时间到达时，只有停止已确认才报告取消或超时终态。

## 模拟回归样例

用 mock Go2 连接和 LLM 覆盖：设备页无 `skill_id` 对话、主聊天 Card 指定 `skill_id`、SSE 对话、访客持久工具拒绝、未知 skill/工具、无效 JSON、4 次工具调用、`accepted` 动作、连续运动拦截、停止未确认、非法速度与时长、`前进 2 秒` 与 `移动两步`、按会话澄清、三种模式切换及模式动作的执行前校验。任务状态测试覆盖 202 → 查询最终结果、相同 `task_id` 重试不重复执行、会话隔离、超时与取消后的停止确认。

模拟测试命令：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest cloud/go2/tests cloud/api/tests cloud/orchestrator/tests -q
```

实机验收仍需按计划顺序执行：状态查询、原地姿态动作、短距离移动、导航；每步确认停止与最终反馈。不得用模拟测试推断实际 WebRTC 动作完成范围。

实施后的全量模拟测试为 **334 passed、5 failed、1 skipped**；五项失败与实施前完全相同。另运行 `node --check app/src/CommandBus.js`、Python `compileall` 和 `git diff --check`，均通过。
