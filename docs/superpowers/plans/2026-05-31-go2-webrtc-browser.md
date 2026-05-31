# Go2 Air 浏览器直连 WebRTC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 PWA 浏览器通过 WebRTC 直接控制 Unitree Go2 Air，后端仅作信令代理（隐藏账号凭据）。

**Architecture:** 浏览器创建 `RTCPeerConnection`，后端代理与 Unitree 云端完成信令（登录→获取 TURN 配置→SDP 交换），WebRTC 连接建立后浏览器直接通过 DataChannel 收发指令。新增 `#/devices/go2-air` 路由，与现有设备系统一致。

**Tech Stack:** FastAPI (信令代理), unitree-webrtc-connect (Python, PyPI), 浏览器原生 WebRTC API, Babel standalone JSX (无构建步骤)

---

## 关键常量（已从库中提取，直接使用）

```javascript
// DataChannel 消息类型
const DC_TYPE = {
  VALIDATION: "validation",
  SUBSCRIBE:  "subscribe",
  MSG:        "msg",
  REQUEST:    "req",
  HEARTBEAT:  "heartbeat",
};

// 运动控制 topic 和命令 ID
const SPORT_TOPIC = "rt/api/sport/request";
const STATE_TOPIC  = "rt/lf/sportmodestate";
const SPORT_CMD = {
  StandUp:    1004,
  StandDown:  1005,
  StopMove:   1003,
  Hello:      1016,
  Stretch:    1017,
  Dance1:     1022,
  Dance2:     1023,
  Move:       1008,
};
```

## DataChannel 协议（完整，已从源码逆向）

### 1. Validation 握手（channel 打开后机器狗主动发起）

机器狗发送：
```json
{"type": "validation", "data": "<随机字符串>"}
```

浏览器必须回复（MD5 加密后 base64）：
```javascript
function encryptValidationKey(key) {
  const prefixed = "UnitreeGo2_" + key;
  const md5hex = SparkMD5.hash(prefixed);           // SparkMD5 库
  const bytes = new Uint8Array(md5hex.match(/.{2}/g).map(b => parseInt(b, 16)));
  return btoa(String.fromCharCode(...bytes));
}
// 回复：
dc.send(JSON.stringify({type: "validation", topic: "", data: encryptValidationKey(key)}));
```

验证成功时机器狗回复：`{"type": "validation", "data": "Validation Ok."}`

### 2. Heartbeat（验证通过后每 2 秒发送）
```javascript
setInterval(() => {
  dc.send(JSON.stringify({
    type: "heartbeat", topic: "",
    data: {timeInStr: new Date().toISOString(), timeInNum: Math.floor(Date.now()/1000)}
  }));
}, 2000);
```

### 3. 订阅状态
```javascript
dc.send(JSON.stringify({type: "subscribe", topic: "rt/lf/sportmodestate"}));
// 机器狗持续推送：{type: "msg", topic: "rt/lf/sportmodestate", data: {mode, body_height, velocity, ...}}
```

### 4. 发送运动指令
```javascript
function sendCmd(dc, apiId, parameter = "") {
  const id = Date.now() % 2147483648;
  dc.send(JSON.stringify({
    type: "req",
    topic: "rt/api/sport/request",
    data: {
      header: {identity: {id, api_id: apiId}},
      parameter: typeof parameter === "string" ? parameter : JSON.stringify(parameter)
    }
  }));
}
// 示例：sendCmd(dc, 1004)                          → StandUp
// 示例：sendCmd(dc, 1008, {x: 0.3, y: 0, z: 0})  → Move forward
```

---

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| Modify | `cloud/api/main.py` | 新增 `/api/go2/ice_servers` 和 `/api/go2/connect` 端点 |
| Edit   | `cloud/.env` | 新增 GO2_* 凭据 |
| Create | `app/src/pages/Go2Page.jsx` | Go2 控制页完整组件 |
| Modify | `app/index.html` | 新增 SparkMD5 CDN + Go2Page.jsx script 标签 |
| Modify | `app/src/app.jsx` | 注入 Go2 静态设备 + 路由 go2-air |

---

## Task 1: 后端信令端点 — ICE Servers

**Files:**
- Modify: `cloud/api/main.py`

- [ ] **Step 1: 在 cloud/api/main.py 顶部新增 import 和会话缓存**

在文件头部（`import os, json, uuid...` 那一行后面）新增：

```python
import base64
import time as _time_module
from unitree_webrtc_connect.unitree_cloud import UnitreeCloud
from unitree_webrtc_connect.encryption import (
    generate_aes_key, rsa_encrypt, rsa_load_public_key, aes_encrypt, aes_decrypt,
)

# 内存缓存：存储登录后的 access_token + RSA 公钥，供 /api/go2/connect 使用
_go2_session: dict = {}
```

- [ ] **Step 2: 新增 `/api/go2/ice_servers` 端点**

在文件末尾（`device_agent_card` 函数后面）新增：

```python
@app.post("/api/go2/ice_servers")
def go2_ice_servers():
    """
    登录 Unitree 云端，返回 TURN/ICE 服务器配置。
    浏览器在创建 RTCPeerConnection 之前必须先调用此接口。
    """
    email    = os.getenv("GO2_EMAIL", "")
    password = os.getenv("GO2_PASSWORD", "")
    serial   = os.getenv("GO2_SERIAL", "")
    region   = os.getenv("GO2_REGION", "cn")

    if not email or not password or not serial:
        raise HTTPException(status_code=500, detail="GO2_EMAIL / GO2_PASSWORD / GO2_SERIAL 未配置")

    cloud = UnitreeCloud(region=region, device_type="Go2")
    access_token = cloud.login_email(email, password)

    pub_key_b64 = cloud.get_pub_key()
    pub_key_pem = base64.b64decode(pub_key_b64).decode("utf-8")
    pub_key     = rsa_load_public_key(pub_key_pem)

    # 获取 TURN 服务器信息
    aes_key_turn   = generate_aes_key()
    encrypted_turn = cloud.webrtc_account(serial, rsa_encrypt(aes_key_turn, pub_key))
    turn_info      = {}
    if encrypted_turn:
        try:
            turn_info = __import__("json").loads(aes_decrypt(encrypted_turn, aes_key_turn))
        except Exception:
            pass

    # 缓存供 connect 端点使用
    _go2_session.update({
        "access_token": access_token,
        "pub_key":      pub_key,
        "region":       region,
        "serial":       serial,
        "ts":           _time_module.time(),
    })

    ice_servers = []
    if turn_info.get("realm") and turn_info.get("user") and turn_info.get("passwd"):
        ice_servers.append({
            "urls":       turn_info["realm"],
            "username":   turn_info["user"],
            "credential": turn_info["passwd"],
        })
    ice_servers.append({"urls": "stun:stun.l.google.com:19302"})

    return {"ice_servers": ice_servers}
```

- [ ] **Step 3: 验证**

```bash
cd /home/eliott/ssm
make api-bg
sleep 3
curl -s -X POST http://127.0.0.1:8082/api/go2/ice_servers | python3 -m json.tool
```

期望输出包含 `ice_servers` 数组，至少有 Google STUN 和一个 TURN 条目。

---

## Task 2: 后端信令端点 — SDP Connect

**Files:**
- Modify: `cloud/api/main.py`

- [ ] **Step 1: 新增 `Go2ConnectRequest` 模型和 `/api/go2/connect` 端点**

紧接 Task 1 Step 2 的代码后面新增：

```python
class Go2ConnectRequest(BaseModel):
    sdp_offer: str


@app.post("/api/go2/connect")
def go2_connect(body: Go2ConnectRequest):
    """
    接收浏览器的 SDP Offer，转发给 Go2（经由 Unitree 云端信令），
    返回 Go2 的 SDP Answer。
    必须先调用 /api/go2/ice_servers 完成登录。
    """
    if not _go2_session.get("access_token"):
        raise HTTPException(status_code=400, detail="请先调用 /api/go2/ice_servers")

    # 会话超过 10 分钟视为过期
    if _time_module.time() - _go2_session.get("ts", 0) > 600:
        raise HTTPException(status_code=400, detail="登录会话已过期，请重新调用 /api/go2/ice_servers")

    access_token = _go2_session["access_token"]
    pub_key      = _go2_session["pub_key"]
    region       = _go2_session["region"]
    serial       = _go2_session["serial"]

    aes_key = generate_aes_key()
    cloud   = UnitreeCloud(region=region, device_type="Go2", access_token=access_token)

    try:
        encrypted_answer = cloud.webrtc_connect(
            sn=serial,
            sk_rsa_b64=rsa_encrypt(aes_key, pub_key),
            data_aes_b64=aes_encrypt(body.sdp_offer, aes_key),
            timeout=10,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unitree 云端信令失败: {e}")

    sdp_answer = aes_decrypt(encrypted_answer, aes_key)
    return {"sdp_answer": sdp_answer}
```

- [ ] **Step 2: 重启 API 并验证端点存在**

```bash
make restart-api
sleep 3
curl -s http://127.0.0.1:8082/openapi.json | python3 -c "
import sys, json
paths = json.load(sys.stdin)['paths']
for p in paths:
    if 'go2' in p:
        print(p)
"
```

期望输出：
```
/api/go2/ice_servers
/api/go2/connect
```

- [ ] **Step 3: Commit**

```bash
git add cloud/api/main.py
git commit -m "feat: 新增 Go2 Air WebRTC 信令代理端点 (/api/go2/ice_servers + /api/go2/connect)"
```

---

## Task 3: 更新 cloud/.env

**Files:**
- Modify: `cloud/.env`

- [ ] **Step 1: 在 cloud/.env 末尾追加以下配置（替换为真实值）**

```
# Unitree Go2 Air
GO2_EMAIL=18391362895
GO2_PASSWORD=Awf020906
GO2_SERIAL=B42D1000Q3JE0J8D
GO2_REGION=cn
```

注意：凭据已在 `test_script/test_remote_connection.py` 中存在，此处直接复用。

- [ ] **Step 2: 验证环境变量已加载**

```bash
make restart-api
sleep 3
curl -s http://127.0.0.1:8082/api/go2/ice_servers | python3 -m json.tool 2>&1 | head -10
```

期望：返回包含 `ice_servers` 的 JSON（不是 "未配置" 错误）。

---

## Task 4: 更新 index.html — 新增依赖脚本

**Files:**
- Modify: `app/index.html`
- Read the file first to know where to insert the script tags.

- [ ] **Step 1: 读取 index.html 当前内容**

```bash
cat /home/eliott/ssm/app/index.html
```

- [ ] **Step 2: 在 `</head>` 之前插入两个 script 标签**

在现有最后一个 `<script>` 标签之前（通常在 `</head>` 前）添加：

```html
<!-- MD5 实现（Go2 DataChannel 验证握手所需） -->
<script src="https://cdn.jsdelivr.net/npm/spark-md5@3.0.2/spark-md5.min.js"></script>
<!-- Go2 控制页面 -->
<script type="text/babel" src="src/pages/Go2Page.jsx"></script>
```

注意：`Go2Page.jsx` 必须在 `app.jsx` 之前加载，因为 app.jsx 会引用 `Go2DevicePage` 组件。

- [ ] **Step 3: 创建 pages 目录**

```bash
mkdir -p /home/eliott/ssm/app/src/pages
```

---

## Task 5: 创建 Go2DevicePage 组件

**Files:**
- Create: `app/src/pages/Go2Page.jsx`

- [ ] **Step 1: 创建完整的 Go2DevicePage 组件**

写入以下完整内容到 `/home/eliott/ssm/app/src/pages/Go2Page.jsx`：

```jsx
/* Go2DevicePage — Unitree Go2 Air WebRTC 直连控制页面
   依赖：SparkMD5（必须在此文件之前加载）
*/

const DC_TYPE = {
  VALIDATION: "validation",
  SUBSCRIBE:  "subscribe",
  MSG:        "msg",
  REQUEST:    "req",
  HEARTBEAT:  "heartbeat",
};

const SPORT_TOPIC = "rt/api/sport/request";
const STATE_TOPIC  = "rt/lf/sportmodestate";
const SPORT_CMD = {
  StandUp:    1004,
  StandDown:  1005,
  StopMove:   1003,
  Hello:      1016,
  Stretch:    1017,
  Dance1:     1022,
  Dance2:     1023,
  Move:       1008,
};

function encryptValidationKey(key) {
  const prefixed = "UnitreeGo2_" + key;
  const md5hex = SparkMD5.hash(prefixed);
  const bytes = new Uint8Array(md5hex.match(/.{2}/g).map(b => parseInt(b, 16)));
  return btoa(String.fromCharCode(...bytes));
}

function sendCmd(dc, apiId, parameter) {
  if (!dc || dc.readyState !== "open") return;
  const id = Date.now() % 2147483648;
  dc.send(JSON.stringify({
    type: DC_TYPE.REQUEST,
    topic: SPORT_TOPIC,
    data: {
      header: {identity: {id, api_id: apiId}},
      parameter: parameter === undefined
        ? ""
        : (typeof parameter === "string" ? parameter : JSON.stringify(parameter)),
    }
  }));
}

function Go2DevicePage({ onBack }) {
  const { useState, useEffect, useRef, useCallback } = React;

  const [status, setStatus]     = useState("idle"); // idle | preparing | connecting | connected | error
  const [error, setError]       = useState("");
  const [robotState, setRobot]  = useState(null);   // {mode, body_height, velocity}
  const [moving, setMoving]     = useState(null);   // 当前持续移动方向
  const videoRef                = useRef(null);
  const pcRef                   = useRef(null);
  const dcRef                   = useRef(null);
  const heartbeatRef            = useRef(null);
  const moveIntervalRef         = useRef(null);

  // ── 清理函数 ────────────────────────────────────────────────────
  const disconnect = useCallback(() => {
    if (heartbeatRef.current)   { clearInterval(heartbeatRef.current);  heartbeatRef.current  = null; }
    if (moveIntervalRef.current){ clearInterval(moveIntervalRef.current); moveIntervalRef.current = null; }
    if (dcRef.current)          { dcRef.current.close();  dcRef.current  = null; }
    if (pcRef.current)          { pcRef.current.close();  pcRef.current  = null; }
    if (videoRef.current)       { videoRef.current.srcObject = null; }
    setStatus("idle");
    setRobot(null);
    setMoving(null);
  }, []);

  useEffect(() => () => disconnect(), [disconnect]);

  // ── 连接流程 ────────────────────────────────────────────────────
  const connect = useCallback(async () => {
    disconnect();
    setError("");
    setStatus("preparing");

    try {
      // Step 1: 获取 ICE/TURN 配置
      const iceRes = await fetch("/api/go2/ice_servers", {method: "POST"});
      if (!iceRes.ok) throw new Error(await iceRes.text());
      const {ice_servers} = await iceRes.json();

      setStatus("connecting");

      // Step 2: 创建 RTCPeerConnection
      const pc = new RTCPeerConnection({iceServers: ice_servers});
      pcRef.current = pc;

      // 接收视频 track
      pc.ontrack = (e) => {
        if (videoRef.current && e.streams[0]) {
          videoRef.current.srcObject = e.streams[0];
        }
      };

      // 创建 DataChannel
      const dc = pc.createDataChannel("data", {ordered: true});
      dcRef.current = dc;

      dc.onopen = () => {
        console.log("[Go2] DataChannel open");
      };

      dc.onclose = () => {
        setStatus("idle");
        setRobot(null);
      };

      // DataChannel 消息处理
      dc.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }

        if (msg.type === DC_TYPE.VALIDATION) {
          if (msg.data === "Validation Ok.") {
            // 验证通过，开始订阅状态和心跳
            dc.send(JSON.stringify({type: DC_TYPE.SUBSCRIBE, topic: STATE_TOPIC}));
            heartbeatRef.current = setInterval(() => {
              if (dc.readyState === "open") {
                dc.send(JSON.stringify({
                  type: DC_TYPE.HEARTBEAT, topic: "",
                  data: {
                    timeInStr: new Date().toLocaleString("zh-CN"),
                    timeInNum: Math.floor(Date.now() / 1000),
                  }
                }));
              }
            }, 2000);
            setStatus("connected");
          } else {
            // 机器狗发来验证挑战，需要回复加密的 key
            const encKey = encryptValidationKey(msg.data);
            dc.send(JSON.stringify({type: DC_TYPE.VALIDATION, topic: "", data: encKey}));
          }
          return;
        }

        if (msg.type === DC_TYPE.MSG && msg.topic === STATE_TOPIC) {
          const d = msg.data?.data || msg.data || {};
          setRobot({
            mode:        d.mode ?? "—",
            body_height: typeof d.body_height === "number" ? d.body_height.toFixed(3) : "—",
            vx:          Array.isArray(d.velocity) ? d.velocity[0]?.toFixed(2) : "—",
            vy:          Array.isArray(d.velocity) ? d.velocity[1]?.toFixed(2) : "—",
          });
        }
      };

      // Step 3: 创建 SDP Offer，等待 ICE gathering 完成
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      await new Promise((resolve) => {
        if (pc.iceGatheringState === "complete") { resolve(); return; }
        pc.onicegatheringstatechange = () => {
          if (pc.iceGatheringState === "complete") resolve();
        };
        setTimeout(resolve, 5000); // 最多等 5s
      });

      const finalOffer = pc.localDescription.sdp;

      // Step 4: 将 Offer 发给后端信令代理，换回 Answer
      const connRes = await fetch("/api/go2/connect", {
        method:  "POST",
        headers: {"Content-Type": "application/json"},
        body:    JSON.stringify({sdp_offer: finalOffer}),
      });
      if (!connRes.ok) throw new Error(await connRes.text());
      const {sdp_answer} = await connRes.json();

      // Step 5: 设置远端描述，WebRTC 握手完成
      await pc.setRemoteDescription({type: "answer", sdp: sdp_answer});

    } catch (err) {
      setError(String(err));
      setStatus("error");
      disconnect();
    }
  }, [disconnect]);

  // ── 持续移动控制 ─────────────────────────────────────────────────
  const startMove = useCallback((x, y, z, label) => {
    if (moveIntervalRef.current) clearInterval(moveIntervalRef.current);
    setMoving(label);
    const fire = () => sendCmd(dcRef.current, SPORT_CMD.Move, {x, y, z});
    fire();
    moveIntervalRef.current = setInterval(fire, 500);
  }, []);

  const stopMove = useCallback(() => {
    if (moveIntervalRef.current) { clearInterval(moveIntervalRef.current); moveIntervalRef.current = null; }
    setMoving(null);
    sendCmd(dcRef.current, SPORT_CMD.StopMove);
  }, []);

  // ── UI ────────────────────────────────────────────────────────────
  const LIME = "#C8FF3E";
  const cardStyle = {
    background: "#111", color: "#fff", minHeight: "100vh",
    fontFamily: "system-ui, sans-serif", padding: "0",
  };
  const headerStyle = {
    display: "flex", alignItems: "center", gap: 12,
    padding: "16px 20px", borderBottom: "1px solid #222",
  };
  const btnStyle = (color = LIME, disabled = false) => ({
    background: disabled ? "#333" : color,
    color: disabled ? "#666" : "#000",
    border: "none", borderRadius: 8, padding: "10px 18px",
    fontWeight: 600, fontSize: 14, cursor: disabled ? "not-allowed" : "pointer",
  });
  const statusColor = {
    idle:       "#666", preparing: "#f0a500",
    connecting: "#f0a500", connected: LIME, error: "#ff4444",
  }[status] || "#666";

  const connected = status === "connected";
  const busy = status === "preparing" || status === "connecting";

  return (
    <div style={cardStyle}>
      {/* Header */}
      <div style={headerStyle}>
        <button onClick={onBack}
          style={{background:"none",border:"none",color:"#888",cursor:"pointer",fontSize:18,padding:0}}>
          ←
        </button>
        <span style={{fontSize:18, fontWeight:700}}>Go2 Air</span>
        <span style={{marginLeft:"auto", fontSize:13, color: statusColor, fontWeight:600}}>
          {{ idle:"未连接", preparing:"获取配置…", connecting:"建立连接…",
             connected:"已连接 ✓", error:"连接失败" }[status]}
        </span>
      </div>

      <div style={{padding:"16px 20px", display:"flex", flexDirection:"column", gap:16}}>
        {/* 连接按钮 */}
        {!connected && (
          <button onClick={connect} disabled={busy}
            style={btnStyle(LIME, busy)}>
            {busy ? "连接中…" : "连接 Go2"}
          </button>
        )}
        {connected && (
          <button onClick={disconnect} style={btnStyle("#ff4444")}>
            断开连接
          </button>
        )}

        {error && (
          <div style={{background:"#1a0000",border:"1px solid #ff4444",
            borderRadius:8,padding:"10px 14px",fontSize:13,color:"#ff6666"}}>
            {error}
          </div>
        )}

        {/* 视频流 */}
        <div style={{borderRadius:12,overflow:"hidden",background:"#000",
          aspectRatio:"16/9",display:"flex",alignItems:"center",justifyContent:"center"}}>
          <video ref={videoRef} autoPlay playsInline muted
            style={{width:"100%",height:"100%",objectFit:"cover",
              display: connected ? "block" : "none"}} />
          {!connected && (
            <span style={{color:"#444",fontSize:14}}>视频流（连接后显示）</span>
          )}
        </div>

        {/* 状态面板 */}
        {robotState && (
          <div style={{background:"#1a1a1a",borderRadius:12,padding:"12px 16px",
            display:"grid",gridTemplateColumns:"1fr 1fr 1fr 1fr",gap:8}}>
            {[
              {label:"模式", value: robotState.mode},
              {label:"体高(m)", value: robotState.body_height},
              {label:"Vx", value: robotState.vx},
              {label:"Vy", value: robotState.vy},
            ].map(({label, value}) => (
              <div key={label} style={{textAlign:"center"}}>
                <div style={{fontSize:11,color:"#666",marginBottom:4}}>{label}</div>
                <div style={{fontSize:15,fontWeight:700,color:LIME}}>{value}</div>
              </div>
            ))}
          </div>
        )}

        {/* 动作按钮 */}
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
          {[
            {label:"站起", cmd: SPORT_CMD.StandUp},
            {label:"坐下", cmd: SPORT_CMD.StandDown},
            {label:"挥手", cmd: SPORT_CMD.Hello},
            {label:"伸展", cmd: SPORT_CMD.Stretch},
            {label:"舞蹈 1", cmd: SPORT_CMD.Dance1},
            {label:"舞蹈 2", cmd: SPORT_CMD.Dance2},
          ].map(({label, cmd}) => (
            <button key={label} disabled={!connected}
              onClick={() => sendCmd(dcRef.current, cmd)}
              style={btnStyle("#2a2a2a", !connected)}>
              {label}
            </button>
          ))}
        </div>

        {/* 移动控制 */}
        <div style={{background:"#1a1a1a",borderRadius:12,padding:16}}>
          <div style={{fontSize:13,color:"#666",marginBottom:12,textAlign:"center"}}>
            移动控制 {moving && <span style={{color:LIME}}>（{moving}）</span>}
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:8,maxWidth:200,margin:"0 auto"}}>
            <div/>
            <button disabled={!connected} style={btnStyle("#2a2a2a",!connected)}
              onPointerDown={() => startMove(0.3,0,0,"前进")} onPointerUp={stopMove}>▲</button>
            <div/>
            <button disabled={!connected} style={btnStyle("#2a2a2a",!connected)}
              onPointerDown={() => startMove(0,0.3,0,"左移")} onPointerUp={stopMove}>◀</button>
            <button disabled={!connected} style={btnStyle("#ff4444",!connected)}
              onClick={stopMove}>■</button>
            <button disabled={!connected} style={btnStyle("#2a2a2a",!connected)}
              onPointerDown={() => startMove(0,-0.3,0,"右移")} onPointerUp={stopMove}>▶</button>
            <div/>
            <button disabled={!connected} style={btnStyle("#2a2a2a",!connected)}
              onPointerDown={() => startMove(-0.3,0,0,"后退")} onPointerUp={stopMove}>▼</button>
            <div/>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginTop:8,maxWidth:200,margin:"8px auto 0"}}>
            <button disabled={!connected} style={btnStyle("#2a2a2a",!connected)}
              onPointerDown={() => startMove(0,0,0.5,"左转")} onPointerUp={stopMove}>↺ 左转</button>
            <button disabled={!connected} style={btnStyle("#2a2a2a",!connected)}
              onPointerDown={() => startMove(0,0,-0.5,"右转")} onPointerUp={stopMove}>↻ 右转</button>
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 验证文件创建成功**

```bash
ls -la /home/eliott/ssm/app/src/pages/Go2Page.jsx
```

期望：文件存在，大小 > 5KB。

---

## Task 6: 更新 app/index.html

**Files:**
- Modify: `app/index.html`

- [ ] **Step 1: 读取当前 index.html 内容**

```bash
cat /home/eliott/ssm/app/index.html
```

- [ ] **Step 2: 找到加载 src/app.jsx 的 script 标签，在其前面插入两个新标签**

当前文件中应有类似：
```html
<script type="text/babel" src="src/app.jsx"></script>
```

在该行之前插入（保持 Go2Page.jsx 在 app.jsx 之前加载）：
```html
<script src="https://cdn.jsdelivr.net/npm/spark-md5@3.0.2/spark-md5.min.js"></script>
<script type="text/babel" src="src/pages/Go2Page.jsx"></script>
```

---

## Task 7: 更新 app.jsx — 注入 Go2 设备 + 路由

**Files:**
- Modify: `app/src/app.jsx`

- [ ] **Step 1: 读取当前路由分发代码（约 line 1417）**

```bash
grep -n "hashMatch\|go2\|DeviceDetailPage\|slug" /home/eliott/ssm/app/src/app.jsx | head -20
```

- [ ] **Step 2: 在 app.jsx 顶部常量区新增 Go2 静态设备**

在 `const ICONS = {...}` 或 `const LIME = ...` 附近，找到第一个 `const` 声明，在其前面加：

```javascript
// Go2 Air 静态设备条目（不依赖 MQTT 发现，直接硬编码）
const GO2_STATIC_DEVICE = {
  unit_id:    "go2_main",
  agent_id:   "go2_main",
  slug:       "go2-air",
  name:       "Go2 Air",
  agent_type: "robot",
  capabilities: ["MOVE", "STAND_UP", "SIT_DOWN", "HELLO", "STRETCH", "DANCE"],
};
```

- [ ] **Step 3: 找到路由分发代码，修改 go2-air 路由**

找到这段代码（约 line 1417）：
```javascript
const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
if (hashMatch) {
  const slug   = hashMatch[1];
  const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
```

在 `if (hashMatch)` 块内，`const device = ...` 之后，加入 Go2 专用路由：

```javascript
  // Go2 Air 专用页面
  if (slug === "go2-air") {
    return <Go2DevicePage onBack={() => navigate("")} />;
  }
```

完整修改后的块如下：
```javascript
const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
if (hashMatch) {
  const slug = hashMatch[1];
  // Go2 Air 专用页面（直连 WebRTC，不走通用 ISM 控制）
  if (slug === "go2-air") {
    return <Go2DevicePage onBack={() => navigate("")} />;
  }
  const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
  return (
    <DeviceDetailPage
      slug={slug}
      device={device}
      ...
```

- [ ] **Step 4: 在 agents 列表里注入 Go2 静态设备**

找到 agents 相关的 useState 或 useMemo，通常写法是:
```javascript
const [agents, setAgents] = useState([]);
```

找到 agents 被最终使用的地方（设备列表渲染），在渲染时合并静态设备：
```javascript
// 在组件的 agents 使用位置，将 GO2_STATIC_DEVICE 合并进去
const allAgents = [...agents, GO2_STATIC_DEVICE];
// 然后用 allAgents 替代 agents 在设备列表渲染处
```

具体位置需读文件后确定，关键原则：确保 `agents.find(a => a.slug === 'go2-air')` 能找到 GO2_STATIC_DEVICE。

- [ ] **Step 5: 验证路由**

```bash
make pwa-bg
sleep 2
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/
```

期望：`200`

---

## Task 8: 端对端测试

- [ ] **Step 1: 确保所有服务运行**

```bash
make stop
make broker &
sleep 3
make api-bg
sleep 3
make pwa-bg
sleep 2
make ps
```

期望：mosquitto、uvicorn、http.server 均出现。

- [ ] **Step 2: 在浏览器打开 Go2 页面**

```bash
make ngrok-url
```

打开输出的 URL，加上 `#/devices/go2-air`，例如：
`https://xxx.ngrok-free.app/#/devices/go2-air`

- [ ] **Step 3: 点击"连接 Go2"，验证连接流程**

观察浏览器 DevTools Console：
- 应看到 `[Go2] DataChannel open`
- 状态变为"已连接 ✓"
- 机器狗视频流在 `<video>` 中播放

- [ ] **Step 4: 发送指令验证**

点击"站起"按钮，机器狗应站起来。

- [ ] **Step 5: Commit 所有变更**

```bash
git add app/index.html app/src/app.jsx app/src/pages/Go2Page.jsx cloud/.env
git commit -m "feat: Go2 Air WebRTC 浏览器直连 — 信令代理 + #/devices/go2-air 控制页

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## 常见问题排查

**Q: `/api/go2/connect` 返回 502 "Device not online"**
→ Go2 Air 没有连接到互联网 / 没有开启远程模式。在机器狗 APP 里确认"远程连接"已启用。

**Q: ICE 连接卡在 "checking"**
→ TURN 服务器配置可能有问题。在 DevTools → Application → Network 里检查 `/api/go2/ice_servers` 返回的 `urls` 格式是否正确（应为 `turn:...` 或 `turns:...`）。

**Q: DataChannel 打开后没有 validation 消息**
→ 等待 5-10 秒，validation 由机器狗主动发起，有时有延迟。

**Q: 视频流不显示**
→ WebRTC 视频 track 可能没有建立。检查 `pc.ontrack` 是否触发（加 `console.log`）。注意：若 Go2 视频加密或使用非标准编解码，浏览器可能无法解码。
