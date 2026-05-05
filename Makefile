# SSM — 服务管理
# 用法：make <target>
#   make broker        启动 MQTT Broker（后台守护进程）
#   make api           启动 Chat API（前台，日志直出）
#   make orchestrator  启动 LangGraph 编排器（前台，日志直出）
#   make pwa           启动 PWA 静态文件服务（前台）
#   make ngrok         启动 ngrok 隧道（前台）
#   make ps            查看 SSM 相关进程
#   make logs          查看后台服务日志

.PHONY: broker api orchestrator pwa ngrok ps logs stop-api stop-pwa

# ── Broker ──────────────────────────────────────────────────
broker:
	mosquitto -c broker/mosquitto.conf -d
	@echo "Broker started: TCP :1883  WS :9001"

# ── Cloud services ───────────────────────────────────────────
api:
	uv run uvicorn server.api.main:app --host 127.0.0.1 --port 8082 --reload

api-bg:
	nohup uv run uvicorn server.api.main:app --host 127.0.0.1 --port 8082 \
		> /tmp/ssm_api.log 2>&1 &
	@echo "API started in background → /tmp/ssm_api.log"

orchestrator:
	cd server/orchestrator && uv run python -u main.py

orchestrator-bg:
	cd server/orchestrator && nohup uv run python -u main.py \
		> /tmp/ssm_orchestrator.log 2>&1 &
	@echo "Orchestrator started in background → /tmp/ssm_orchestrator.log"

# ── Phone PWA ────────────────────────────────────────────────
pwa:
	uv run python -m http.server 8081 --directory agents/phone

pwa-bg:
	nohup uv run python -m http.server 8081 --directory agents/phone \
		> /tmp/ssm_pwa.log 2>&1 &
	@echo "PWA started in background → /tmp/ssm_pwa.log"

# ── ngrok ────────────────────────────────────────────────────
ngrok:
	ngrok http 8080 --log=stdout --request-header-add "ngrok-skip-browser-warning:1"

ngrok-bg:
	nohup ngrok http 8080 --log=stdout --request-header-add "ngrok-skip-browser-warning:1" > /tmp/ssm_ngrok.log 2>&1 &
	@echo "ngrok started in background → /tmp/ssm_ngrok.log"
	@sleep 2
	@curl -s http://localhost:4040/api/tunnels 2>/dev/null | \
		python3 -c "import sys,json;[print('  Public URL:',t['public_url']) for t in json.load(sys.stdin)['tunnels']]" || true

# ── 工具 ─────────────────────────────────────────────────────
ps:
	@echo "=== SSM processes ==="
	@pgrep -a -f "mosquitto|uvicorn|http.server|ngrok|orchestrator" || echo "(none running)"

logs:
	@echo "=== API ===" && tail -20 /tmp/ssm_api.log 2>/dev/null || echo "(no log)"
	@echo "=== Orchestrator ===" && tail -20 /tmp/ssm_orchestrator.log 2>/dev/null || echo "(no log)"
	@echo "=== PWA ===" && tail -5  /tmp/ssm_pwa.log 2>/dev/null || echo "(no log)"
	@echo "=== ngrok ===" && tail -5 /tmp/ssm_ngrok.log 2>/dev/null || echo "(no log)"

ngrok-url:
	@curl -s http://localhost:4040/api/tunnels | \
		python3 -c "import sys,json;[print(t['public_url']) for t in json.load(sys.stdin)['tunnels']]"
