# Go2 阶段 C～G 实施与验收记录

- 日期：2026-11-06
- 执行计划：[分层状态与动作计划](2026-11-06-go2-hierarchical-state-and-actions.md)
- 前置交付：[A＋B 记录与硬件反馈限制](2026-11-06-go2-actions-ab-review.md)
- 交付状态：**C～F 软件实现完成；G 软件回归完成，H01～H12 真机验收待完成**。
- 本轮使用三个 subagent，分别负责 C／D 核心、E 接口、F 前端；主代理负责集成审查、回归与 G 交付记录。
- 保留 LangChain／LangGraph、机器狗大脑、人格、记忆、现有导航算法与身份／租约边界。不实现自然语言条件编排、自动唤醒规则或用户动作组合。

## 初始状态与验证边界

本轮开始 `git status --short` 为空。此前用户工作区增量已包含在当前版本中，本轮不回退。探测脚本 `scripts/probe_go2_actions.py` 初始 SHA-256 为 `6545ef0880436b23ca5b83af354166899643b99a82156cf8b7e0d00068ee89bc`，与 A／B 记录一致。

`make ps` 未发现运行中的业务服务。本轮无硬件反馈窗口，不自动运行交互探测、连接设备或发出真实运动；单元测试、模拟传输和浏览器夹具均只作为软件验证。

## 初始回归基线

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest \
  cloud/go2/tests cloud/agents/go2/tests \
  cloud/api/tests/test_gateway.py cloud/api/tests/test_devices.py \
  cloud/api/tests/test_teachings.py cloud/cards/tests cloud/space/tests cloud/peers/tests \
  cloud/go2/actions_checks.py cloud/go2/action_gateway_checks.py cloud/go2/actions_graph_checks.py \
  cloud/go2/activity_mode_checks.py cloud/go2/perception/visual_approach_checks.py \
  cloud/go2/perception/visual_lidar_navigate_checks.py cloud/go2/perception/visual_target_checks.py \
  cloud/peers/games_checks.py --ignore=cloud/peers/tests/test_personality_view.py -q --tb=short
```

结果：**523 passed / 18 failed / 1 skipped，42.79 秒**。与 A／B 扩展基线相同；完整本地日志 `/tmp/ssm-cg-baseline.txt`。

18 个遗留失败：autonomy_inbox 3、drive 1、episode_memory 1、mode_runtime 3、navigation_safety 5、api devices 1、cards registry 1、peers inbound 1、peers view 2。另有 `test_personality_view.py` 引用已删除 legacy 模块的收集错误，沿用 A／B 的显式排除。最终回归需逐项与此基线对照，不能将旧失败算作本次新回归，也不能因此宣称完整测试全绿。

## C：统一运行状态与遥控

`cloud/go2/state.py` 保存连接、活动、生活、身体、控制模式、自主配置和任务归属。短事务使用 RLock，通知在事务外；`fsm_state` 由这份事实单向投影。连接代次 `epoch` 与 `revision` 一起供乱序防护使用；执行阶段、遥测新鲜度及探索许可的变化也推进版本，避免只有 HTTP 看到新的门禁。

连接成功保留 `body_state=unknown`，不再自动写 standing 或发送 BalanceStand。Sport、odom、低层状态、地图回调绑定物理连接，视频采集也检查会话；旧连接事件及旧动作响应不能确认新动作。断连清理当前事实，历史确认姿态单独保留，不重放身体动作。

进入遥控只改变 `control_mode`；实际速度反馈确认移动，零输入保留遥控入口，停止确认与退出遥控分别处理。坐姿、趴姿和休息期间急停不会虚构站立。遥测陈旧时保留已知身体事实，但关闭普通身体动作，等待新鲜反馈。

## D：生活状态、休息唤醒与任务归属

统一工具 `go2_life(action)` 和 `POST /api/go2/life` 支持 `request_rest / wake / start_explore / stop_explore`，沿用现有执行上下文、角色和控制租约。忙碌的休息请求在取消任务或抢占租约前被阻止；不排队。休息保持连接与姿态，唤醒只回 `active + idle`，不自动起立或重放探索。

Graph 按 run ID 管理互动归属，覆盖等待模型、工具执行、取消、澄清和恢复；显式状态查询和生活技能保持可达。探索、导航及已有同伴游戏接入原任务事件，不增加新 Agent 循环。任务收尾检查代次与任务标识，旧任务不能结束较新的任务。休息期间自主事件在进入大脑／身体执行前被门禁阻止；原主人识别与挥手教导路径保留。

用户介入探索时，先取消并等待所属导航／自主任务清理；存在运动或在途速度流时，必须等待发送停止后的新鲜静止反馈，未确认不发起下一身体动作。开启探索复用定位与地图前置检查，失败时说明原因，不修改配置或抢租约。

普通用户交接保留探索许可，但取消旧导航和旧事件。互动终结／澄清、身体命令收尾、导航收尾或新鲜身体帧可以调用原 Drive 的恢复入口；只有可活动待命、确认站立、普通控制、无未完成身体／导航／发送任务且原控制归属仍允许时，才启动原策略的新决策。新会话控制租约、遥控模式或未确认姿态都会阻止恢复。显式停止探索、休息、唤醒、manual 和跨连接代次会清除暂停记录／继续探索许可，后续普通对话不能重新开启它。没有增加第二个决策循环或重放旧目标。

## E：对外协议与能力发现

- Card 新增可选 `regions`：连接、活动、生活、身体；身体保留旧平面拓扑。公共验证检查区域边、父级关系和循环，灯 Card 不需要新增字段。
- HTTP、SSE、MQTT state 摘要读取同一快照。只有生活状态变化也立即通知 SSE，无需等待硬件帧；跨事件循环队列通过创建方 loop 投递。
- MQTT retained Card／status 继续负责能力与在线真相。注册表拒绝旧 revision、已退役 epoch 及无版本覆盖；同版本事实一致的新鲜非 retained 心跳可以刷新接收时间。
- `/api/commands/go2` 贯通六个新动作及生活操作，保留可信身份；开启探索仍需管理员。生活请求不会在自身前置检查之前调用全局活动中断。
- 管家用户委托从统一动作目录发现身体动作及生活工具，自主同伴的旧允许范围保持。空间／同伴摘要保留并行状态和休息时的 `life_state=null`。
- 协议说明见 `protocol/topics.md`；未增加条件规则、自动唤醒或动作组合协议。

## F：PWA 展示与交互

页面顶部同时显示连接、活动、生活、身体四条状态。身体预览格包括全部新增动作与未知姿态；点击预览不执行，独立操作按钮遵守后端可用列表并显示阻止原因。休息保留身体事实，遥控入口与移动分别展示，紧急制停保持独立可达。

请求期间防重复点击，未确认结果不会投影成完成姿态。共享快照合并器拒绝过期版本／退役 epoch，切换设备时隔离旧 SSE。保留 EventSource 重连与旧平面 Card／灯页面回退，不新增设备状态轮询。脚本加载顺序和 SW 缓存版本已更新，触控按钮最小高度 44px，并补键盘摇杆与焦点样式。

可重跑前端检查位于 `scripts/check_go2_frontend.cjs`。`--preview` 导出静态审阅 HTML，`--browser` 执行实际浏览器交互与截图。所有预览均明确标记模拟状态，拦截网络请求，不能操作真机。浏览器临时依赖及 CJK 字体仅放 `/tmp`，没有修改系统包、产品字体依赖、Python 依赖或引入前端构建系统。

浏览器实测使用 **React／React DOM 18.3.1**，与产品 `app/index.html` 一致。检查通过：四区域同时高亮、预览零命令、重复点击拦截、四项 LIFE 的命令网关派发、访客禁止开启探索、休息保留身体观测、遥控键盘移动／释放归零、SSE 旧版本／跨代次拒绝、设备切换隔离、360px／768px 无横向溢出、实际模拟触控 tap、44px 触控目标，以及灯平面图回退。浏览器夹具的聊天壳、网络传输和状态是 stub，分层页面、动作／生活控件、SVG 和样式是产品组件；结果不能算真实 API／设备联调。

前端审阅截图（均为**模拟状态**）：[手机待命＋站立](go2-hierarchy-cg/mobile.png)、[手机休息＋趴卧](go2-hierarchy-cg/rest-mobile.png)、[桌面探索＋移动](go2-hierarchy-cg/desktop.png)、[平板布局](go2-hierarchy-cg/tablet.png)。已由主代理打开检查中文、布局及状态区别。静态三场景预览另外保留在 `/tmp/go2-hierarchical-preview.html`，可用检查脚本重新导出。

本机浏览器验证命令（依赖路径按验证机器替换；这些路径不属于产品依赖）：

```bash
NODE_PATH=/home/eliott/.npm/_npx/668c188756b835f3/node_modules \
  node scripts/check_go2_frontend.cjs --preview

LD_LIBRARY_PATH=/tmp/ssm-chrome-libs/usr/lib64 \
PUPPETEER_EXECUTABLE_PATH=/home/eliott/.cache/puppeteer/chrome/linux-148.0.7778.97/chrome-linux64/chrome \
NODE_PATH=/home/eliott/.npm/_npx/668c188756b835f3/node_modules \
GO2_PREVIEW_FONT=/tmp/ssm-go2-preview-subset.otf \
GO2_PREVIEW_REACT=/tmp/ssm-react-18.3.1.js \
GO2_PREVIEW_REACT_DOM=/tmp/ssm-react-dom-18.3.1.js \
  node scripts/check_go2_frontend.cjs --browser
```

本机 sandbox 限制 Chrome 的 `setsockopt`，浏览器进程在自动审批放行后完成验证；不是前端代码异常。缺少图形库／CJK 字体的问题仅通过 `/tmp` 私有运行库和测试字体子集解决。

## G：最终验证与待验

最终相关模块完整回归：**594 passed / 18 failed / 1 skipped，31.26 秒**。与初始基线按完整 FAILED node ID 集合对比，**新增失败 0，原有失败仍为同一 18 项**；本轮新增的三个检查文件共 **71 项全部通过**。没有将原有失败排除后宣称全套全绿。

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest \
  cloud/go2/tests cloud/agents/go2/tests \
  cloud/api/tests/test_gateway.py cloud/api/tests/test_devices.py \
  cloud/api/tests/test_teachings.py cloud/cards/tests cloud/space/tests cloud/peers/tests \
  cloud/go2/actions_checks.py cloud/go2/action_gateway_checks.py cloud/go2/actions_graph_checks.py \
  cloud/go2/activity_mode_checks.py cloud/go2/perception/visual_approach_checks.py \
  cloud/go2/perception/visual_lidar_navigate_checks.py cloud/go2/perception/visual_target_checks.py \
  cloud/peers/games_checks.py cloud/go2/hierarchy_interface_checks.py \
  cloud/go2/hierarchy_integration_checks.py cloud/go2/state_life_checks.py \
  --ignore=cloud/peers/tests/test_personality_view.py -q --tb=short
```

| 验证范围 | 实际结果 |
|---|---|
| C／D 广定向 | 151 passed；状态／动作／Graph／连接／HTTP／executor |
| C／D 安全收尾 | 94 passed；归属、取消、澄清恢复、普通互动恢复与显式停止区别、无地图拒绝、空帧／NaN、停稳交接等 |
| 最后休息边沿及旧夹具迁移 | 71 passed；未完成导航清理／在途速度阻止休息，旧测试注入可信事实而非仅写 fsm_state |
| Card → 动作网关 → 分层接口 → 跨传输集成 | 106 passed；包括按新确认契约迁移的 A／B 网关夹具 |
| 三个新增检查文件 | `state_life_checks.py`、`hierarchy_interface_checks.py`、`hierarchy_integration_checks.py` 共 71 项，在最终完整合集全部通过 |
| 最终相关完整合集 | 594 passed / 同一 18 项既有失败 / 1 skipped；原 legacy personality_view 收集错误仍沿基线显式排除 |
| 前端 | Node 回归、真实 React 18.3.1 浏览器交互及四尺寸布局通过；模拟状态和传输边界见 F |
| 保护／静态检查 | `git diff --check` 通过；探测脚本最终 SHA-256 与初始一致；`make ps` 未发现业务服务，无本轮部署或重启 |

上述定向集合有重叠，不相加为总通过数。最终日志保留在 `/tmp/ssm-cg-final-regression-verified.txt`，初始对照在 `/tmp/ssm-cg-baseline.txt`；Node 最终日志在 `/tmp/ssm-cg-final-frontend.txt`。现有被 Git 忽略的本地旧测试只迁移受新状态契约影响的注入方式／断言；可提交的检查文件包含关键契约，不依赖这些旧测试才能验证。

以下硬件验收与软件结果分开记录；真实视频／地图、完整 PWA→API→设备、狗聊天／管家真实模型与硬件回执、真实手机触控及灯聊天／颜色控制联调仍待运行服务和硬件窗口。浏览器模拟触控不等于真实手机通过。

| 真机项 | 本轮状态与需要的证据 |
|---|---|
| H01 初始站立／趴下／坐姿同步 | **待验**。现有高度范围重叠、mode=0，不能区分初始坐姿和站立；当前保守显示 unknown，普通运动不放行。未新增人为确认姿态接口，不能靠重连解锁。后续必须补可靠设备反馈适配与证据。 |
| H02 Sit → RiseSit | **待验**。指令映射沿用 A／B，但没有可靠 sitting 完成反馈；不能以测试夹具中的坐姿事实宣称自动链路通过。 |
| H03 Content | **待验**。补真实开始／结束和结束姿态，名称仍保留 Content。 |
| H04 FingerHeart | **待验**。用户已证实可执行；原文件仍缺正向回执及完成反馈，需补证。 |
| H05 FrontPounce | **待验**。补完成、回站立及实际中断能力。 |
| H06 FrontJump | **待验**。补完成、落地站稳及实际中断能力，固定延时不能作证据。 |
| H07 旧动作／移动／停止 | **待验**。软件回归不能代替运动回执及停稳证据。 |
| H08 遥控／归零／退出 | **待验**。软件与浏览器已覆盖状态解耦，仍需真机速度与页面对照。 |
| H09 休息／唤醒 | **待验**。软件已验证不发送起立／趴下、不重放探索；需观察真实连接和姿态保持。 |
| H10 探索 → 用户介入 | **待验**。软件验证取消、交接和未确认阻止；真实停步、租约及任务日志仍需对照。 |
| H11 动作期间断连／重连 | **待验**。模拟覆盖旧回调隔离和无重发，真实故障序列未执行。 |
| H12 页面／狗聊天／管家 | **待验**。统一能力发现、身份与执行边界已有软件检查，真实系统端到端回执仍未采集。 |

每项真机验收需记录 request/task/run ID、SPORT 映射、时间、响应码、原始遥测、实际观察、软件结果和页面变化。新增六动作及旧四表演缺可靠完成证据时继续返回 accepted／未确认，不自动放行依赖动作。用户可观察、可接管窗口与必要反馈条件尚不具备，因此**本轮不能宣布真机验收通过或整期完全验收**。

## 交付与回退

本轮不部署、不重启业务服务、不发布 retained Card。改动保留在工作区供审阅；不重置用户目录，不修改探测脚本、密钥、人格或长期记忆格式。

回退应只撤销本轮明确文件差异，不能使用整体 `git reset --hard`。真实部署后的回退先按原停止／租约机制收尾本会话运动；软件回退不代表物理停止。先保留兼容后端再切前端；若新 Card 已发布，回退时经正常路径重新发布旧 Card。没有新增规则数据库或记忆迁移需要回滚。
