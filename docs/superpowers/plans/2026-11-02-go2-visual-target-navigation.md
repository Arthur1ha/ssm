# Go2 视觉目标导航执行计划

> 日期：2026-11-02  
> 状态：软件首版已实现；现场标定、避障验证和实机验收待完成  
> 目标：用户说“走到画面中的白色猫头鹰旁边”时，Go2 识别当前可见目标，选择已知可通行的停靠点，规划并执行路线，停稳复查后报告结果。

## 1. 能力边界与现状

对外新增 Agent Card skill `go2_visual_navigate`（展示名“视觉目标导航”），映射内部工具 `go2_navigate_to_visible_target`。首版参数为 `{"target": "白色猫头鹰"}`，只处理当前摄像头可见、接触地面的静态目标；不把自然语言中的估计距离当作导航坐标。后续若需要用户点击画面中的位置，再扩展带 `frame_id` 和归一化图像坐标的目标类型，不在首版提前实现。

现有 `go2_navigate` 继续表示去**当前有效会话内已保存的命名地点**。新能力与它共用 `Navigator`、里程计、局部栅格地图、运动租约、停止接口和任务结果契约。`Navigator.go_to_coordinate()` 已有当前 `odom` 坐标的 A* 规划、周期重规划、机身空间检查、超时与停稳判定；新能力只补视觉目标到安全停靠点的转换及抵达复核。参考 [现有指定地点导航计划](2026-10-28-go2-navigation-closed-loop.md)，不新建第二套导航控制器或 Agent 循环。

2026-11-02 的猫头鹰任务证明当前断点：`go2_observe` 仅返回“桌下，约 3–4 米”的文字；模型随后调用一次 `go2_move`（前进 2 秒），其结果为 `accepted`，Graph 按未确认动作暂停。该指令没有产生可用于 A* 的目标坐标，导航器也没有启动。`accepted` 不能自动升级为到达成功。

### 责任分工

| 层 | 责任 |
|---|---|
| Go2 Agent / Card | 从用户话语选用 `go2_visual_navigate`；公开技能及参数；只依据最终结果回复 |
| 视觉目标定位 | 识别指定物体，返回同一帧的目标区域、地面接触点、置信度、采集时间；结合标定和传感器数据输出当前 `odom` 坐标及不确定度 |
| Navigator | 在已知自由区域选择、规划并跟踪停靠点；依据里程计/地图重规划，控制时长、速度、停止和到达判定 |
| 机载避障 | 在已验证的运动通道下过滤近身危险；不能替代路线规划或到达判定 |
| 任务状态 | `accepted`/`executing`/`succeeded`/`blocked`/`failed`/`cancelled`；只有定位、导航和复核都通过才对工具返回 `ok` |

## 2. 实施步骤

每一步先用录制数据或只读诊断验证，再进行低速、短距实机试验。每次只交付一处可观察的行为；保留工作区已有未提交改动。

### 阶段 A：确认机载避障与现有运动通道

**涉及：**`cloud/go2/connection/webrtc.py`、只读诊断或临时试验脚本、任务日志。

1. 通过现有 WebRTC 连接只读调用 `OBSTACLES_AVOID` 的 `SWITCH_GET`，记录返回码、启用状态、固件版本、连接方式；不因库中存在 API 就假定这台机器狗已启用。
2. 在有人可立即急停的开阔场地，以同一低速、短时指令分别验证当前 `SPORT_MOD Move` 与导航器使用的 `WIRELESS_CONTROLLER` 是否受机载避障过滤。记录实际位移、避障介入、停止反馈；避免同时使用两种运动通道竞争控制权。
3. 如果当前通道不能确认受机载避障保护，验证驱动已暴露的 `OBSTACLES_AVOID` 移动通道能否在当前远程 WebRTC 会话使用；只有实测成功后才选择接入。无论选择哪种通道，都保留上层地图的未知区封锁、机身空间检查、失联停止和用户急停。

**验收：**明确记录“本机固件 + 当前连接方式 + 选定运动通道”的避障开关状态与实测效果；无法确认时，视觉目标导航不得进入运动阶段。宇树官方 SDK 提供 `SwitchGet`/`SwitchSet`/`Move` 与位置移动接口，但移动接口本身不提供到达回执：[官方客户端源码](https://github.com/unitreerobotics/unitree_sdk2_python/blob/master/unitree_sdk2py/go2/obstacles_avoid/obstacles_avoid_client.py)。

### 阶段 B：把画面目标变成有证据的坐标

**涉及：**`cloud/go2/connection/video.py`、`cloud/go2/perception/` 新增视觉目标定位模块、`cloud/go2/connection/sensors.py`、离线回放样本。

1. 为视频帧保存采集时间与帧 ID；按时间匹配新鲜的 `odom` 与点云，超出容差或 `odom` 会话变化则拒绝定位。视觉模型只负责识别目标和地面接触点，不允许自由文本距离直接成为运动参数。
2. 实测并保存当前摄像头内参、机身外参及其标定误差；将目标接触点的视线与实测地面平面求交，并用点云/地图检查落点一致性。目标在桌面、接触点不可见、投影射线不可靠或误差过大时返回 `target_unlocalized`。
3. 输出结构化结果：目标标识、帧 ID/时间、目标区域、`frame_id=odom`、`x/y`、位置不确定度、当前 odom 会话 ID。首版只接受明确且唯一的静态地面目标；多个相似物体、目标丢失或低置信度时停止并报告。

**验收：**在 1 m、2 m 和遮挡场景录制帧与位姿，用现场量测点核对投影误差；误差超过停靠安全余量、坐标过期或会话不匹配时不产生移动指令。单张图片的“约 3–4 米”不算距离验证。

### 阶段 C：选停靠点并复用 Navigator

**涉及：**`cloud/go2/navigation/navigator.py`、`occupancy_grid.py` 的现有只读规划接口、新增停靠点选择函数、Go2 工具执行器。

1. 在目标周围的**已知自由格**中寻找可达停靠点，按目标距离、朝向、机身占地与安全余量排序；障碍、未知区域及桌腿间狭窄空间不可选。目标位于桌下时可停在桌外可达处，并在结果中明确是“靠近目标的停靠点”。
2. 目标超出当前局部地图、起点/终点不在同一可达自由区域或地图/位姿过期时返回 `blocked`；不退化为定时直行。对于超出地图的目标，首版报告当前局部地图不足，待后续独立设计分段探路。
3. 在现有运动租约下调用 `Navigator.go_to_coordinate()`，沿用其超时、重规划、机身空间检查、停止与任务记录。机载避障使狗停下或偏离时，用实际里程计判定卡住或重规划；不能按“已发送运动命令”推定前进。

**验收：**离线地图样例覆盖目标在自由区、障碍上、桌下、未知区、地图外及路线被堵；只有可达停靠点能进入导航。实机从 1 m、2 m 开始，记录规划路线、机载避障状态、里程计轨迹、停止原因和最终位置。

### 阶段 D：抵达复核与统一调用

**涉及：**`cloud/go2/tools/{registry,tools,executor}.py`、`cloud/go2/card.py`、`cloud/agents/go2/graph.py`、`cloud/api/intents.py`、现有任务进度/SSE 通道。

1. 导航器报告 `succeeded` 且机身停稳后，从新鲜帧重新识别同一目标；核对它与停靠点的相对方向和距离。目标丢失、被误认、停靠点偏离或复核超时均不返回 `ok`。
2. 注册工具 `go2_navigate_to_visible_target`，标记为运动工具，只接收目标描述；注册 Card skill `go2_visual_navigate`，供管家通过现有 Card/同伴技能路由调用。设备页自然语言和管家委派进入同一 Go2 Graph，不由模型逐步拼接 `observe`、`move`。
3. 把视觉导航定义为**一个任务**，复用现有 run/task ID、进度、取消、超时和最终结果通道；确认外层任务期限覆盖识别、规划、运动和复核。若需要异步执行，`accepted` 只表示任务已受理，调用方必须查询终态，不能把它当作技能完成。
4. `ok` 只在“停靠点已抵达、停稳、目标复核通过”时给出；`blocked`/`failed`/`cancelled` 带结构化原因与最后可信位姿。回复应说明“到猫头鹰旁的安全停靠点”，不得声称进入桌下或接触物体。

**验收：**同一句“走到画面中的白色猫头鹰旁边”从设备页和管家委派各执行一次，均出现单个技能任务、可追踪进度、可急停和一致的终态；目标丢失、用户接管、地图过期、避障停步及外层超时均无误报到达。首次开放前，在受控场地连续 10 次无碰撞、无失联继续移动、无假成功。

## 3. 本轮不扩展的能力

- 不做跨重启的持久地图或全屋 SLAM；这是[指定地点导航计划](2026-10-28-go2-navigation-closed-loop.md)的后续范围。
- 不让视觉模型直接输出速度、时长或未经校验的坐标；不为看不见的目标、桌面目标、动态目标或地图外目标尝试盲走。
- 不改 ESP32/MQTT 控制面。Go2 Card 仍通过 MQTT 发布；实际任务继续经现有 HTTP/WebRTC 数据面执行。


## 4. 实施记录（2026-11-02）

已接入软件侧首版：Go2 Card/Graph 公布并调用 `go2_visual_navigate`，视频帧携带采集时间、帧 ID 和 odom 会话 ID；视觉模块只接收目标接触像素，使用现场标定参数求地面交点；从当前已知自由地图选安全停靠点，路线和逐步运动都封锁目标附近区域；复用 Navigator、运动租约和停稳判定，抵达后再取新帧复核。视觉任务失败直接终止，不回退到定时直行。每次任务在运动前还会只读查询当前避障开关；未提供现场证据或开关状态不可确认时，工具返回 `blocked`，不会开始运动。

现场验收仍未完成：`cloud/go2/perception/camera_calibration.json` 必须由本机当前站姿的内参、相机到机身旋转/平移、相机离地高度、重投影误差和 1 m/2 m/遮挡回放实测误差生成，并写入本机序列号；`cloud/go2/perception/avoidance_evidence.json` 必须记录本机序列号、固件版本、Remote WebRTC 下 `SWITCH_GET` 状态及 `WIRELESS_CONTROLLER` 通道实际避障试验。两个文件均未随仓库提供，且不应靠猜测填写。已有管理员只读诊断 `/api/go2/diagnostics/avoidance`，但它只查询已建立的 WebRTC 会话；现有 `connect()` 会发送 `balance_stand()`，不能作为纯只读建连。阶段 A 的两个运动通道低速试验、阶段 B 的地面点投影核验、阶段 C 的 1 m/2 m 试走，以及阶段 D 的双入口和连续 10 次受控试验仍需现场执行。首次试验前须有人可立即急停；当前代码尚未获得实机运动验收。

现场证据文件字段由 `visual_target.py` 校验：标定文件需 `measured_on_robot`、`replay_verified`、`odom_session_independent` 为 true，`posture` 为 `standing`，以及 `camera_id`、`fx/fy/cx/cy`、`rotation_body_from_camera` (3×3)、`translation_body_from_camera_m` (3)、`camera_height_above_floor_m`、`reprojection_error_px`、`position_error_m`（总投影不确定度不得大于 0.20 m）；避障文件需 `robot_serial` 与当前连接一致、`connection_method=Remote`、`motion_channel=WIRELESS_CONTROLLER`、`switch_get_enabled` 和 `motion_channel_obstacle_test_passed` 为 true，以及 `firmware_version`、`tested_at`。填写字段不替代实测；若固件、姿态、相机或运动通道发生变化，需重新验收。
