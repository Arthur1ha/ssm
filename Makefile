# SSM — 服务管理
# 用法：make <target>
#   make broker            启动 MQTT Broker（后台守护进程，幂等）
#   make api               启动 Chat API（前台，日志直出）
#   make api-bg            启动 Chat API（后台，幂等）
#   make orchestrator      启动 LangGraph 编排器（前台，日志直出）
#   make orchestrator-bg   启动 LangGraph 编排器（后台，幂等）
#   make pwa               启动 PWA 静态文件服务（前台）
#   make pwa-bg            启动 PWA 静态文件服务（后台，幂等）
#   make ngrok             启动 ngrok 隧道（前台）
#   make ngrok-bg          启动 ngrok 隧道（后台，幂等）
#   make stop              停止全部后台服务
#   make restart-*         重启指定后台服务
#   make ps                查看 SSM 相关进程
#   make logs              查看后台服务日志

.PHONY: broker api api-bg orchestrator orchestrator-bg pwa pwa-bg ngrok ngrok-bg \
        ps logs ngrok-url trace \
        stop restart-api restart-orchestrator restart-pwa restart-ngrok

# 项目根目录（Makefile 所在目录的绝对路径，运行时自动计算）
SSM_DIR := $(CURDIR)

# 日志目录（项目内，相对路径）
LOG_DIR  := $(SSM_DIR)/logs
BROKER_CONF := $(LOG_DIR)/mosquitto.conf

# ── Broker ──────────────────────────────────────────────────
broker: $(LOG_DIR)
	@pkill -f "[m]osquitto" || true
	@SSM_DIR=$(SSM_DIR) envsubst < broker/mosquitto.conf.tmpl > $(BROKER_CONF)
	mosquitto -c $(BROKER_CONF) -d
	@echo "Broker started: TCP :1883  WS :9001"

# ── Cloud services ───────────────────────────────────────────
api:
	uv run uvicorn server.api.main:app --host 127.0.0.1 --port 8082 --reload

api-bg: $(LOG_DIR)
	@pkill -f "[u]vicorn server.api.main" || true
	nohup uv run uvicorn server.api.main:app --host 127.0.0.1 --port 8082 \
		> $(LOG_DIR)/api.log 2>&1 &
	@echo "API started in background → logs/api.log"

orchestrator:
	cd server/orchestrator && uv run python -u main.py

orchestrator-bg: $(LOG_DIR)
	@pkill -f "[o]rchestrator.*main.py" || true
	@pkill -f "uv run python -u main.py" || true
	@sleep 1
	cd server/orchestrator && nohup uv run python -u main.py \
		> $(LOG_DIR)/orchestrator.log 2>&1 &
	@echo "Orchestrator started in background → logs/orchestrator.log"

# ── Phone PWA ────────────────────────────────────────────────
pwa:
	uv run python -m http.server 8081 --directory agents/phone

pwa-bg: $(LOG_DIR)
	@pkill -f "[h]ttp.server 8081" || true
	nohup uv run python -m http.server 8081 --directory agents/phone \
		> $(LOG_DIR)/pwa.log 2>&1 &
	@echo "PWA started in background → logs/pwa.log"

# ── ngrok ────────────────────────────────────────────────────
ngrok:
	env HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
		ngrok http 8080 --log=stdout --request-header-add "ngrok-skip-browser-warning:1"

ngrok-bg: $(LOG_DIR)
	@pkill -f "[n]grok" || true
	nohup env HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
		ngrok http 8080 --log=stdout --request-header-add "ngrok-skip-browser-warning:1" > $(LOG_DIR)/ngrok.log 2>&1 &
	@echo "ngrok started in background → logs/ngrok.log"
	@sleep 2
	@curl -s http://localhost:4040/api/tunnels 2>/dev/null | \
		python3 -c "import sys,json;[print('  Public URL:',t['public_url']) for t in json.load(sys.stdin)['tunnels']]" || true

# ── 停止 / 重启 ───────────────────────────────────────────────
stop:
	@pkill -f "[m]osquitto"               || true
	@pkill -f "[u]vicorn server.api.main" || true
	@pkill -f "[o]rchestrator.*main.py"   || true
	@pkill -f "[h]ttp.server 8081"        || true
	@pkill -f "[n]grok"                   || true
	@echo "All SSM background services stopped."

restart-api: api-bg
restart-orchestrator: orchestrator-bg
restart-pwa: pwa-bg
restart-ngrok: ngrok-bg

# ── 工具 ─────────────────────────────────────────────────────
$(LOG_DIR):
	@mkdir -p $(LOG_DIR)

ps:
	@echo "=== SSM processes ==="
	@pgrep -a -f "mosquitto|uvicorn|http.server|ngrok|orchestrator" || echo "(none running)"

logs:
	@echo "=== API ===" && tail -20 logs/api.log 2>/dev/null || echo "(no log)"
	@echo "=== Orchestrator ===" && tail -20 logs/orchestrator.log 2>/dev/null || echo "(no log)"
	@echo "=== PWA ===" && tail -5  logs/pwa.log 2>/dev/null || echo "(no log)"
	@echo "=== ngrok ===" && tail -5 logs/ngrok.log 2>/dev/null || echo "(no log)"

ngrok-url:
	@curl -s http://localhost:4040/api/tunnels | \
		python3 -c "import sys,json;[print(t['public_url']) for t in json.load(sys.stdin)['tunnels']]"

trace:
	mosquitto_sub -h 127.0.0.1 -p 1883 -u ssm_user -P Wl4sErQrlrpEbm7r -t "ssm/#" -v
