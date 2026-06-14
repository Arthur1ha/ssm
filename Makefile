# SSM — 服务管理
# make <服务>        前台运行（日志直出，在 tmux 窗口里跑）
# make <服务>-bg    后台运行（幂等，重复执行会重启）
# make stop          停止所有后台服务
# make ps / logs     查看进程 / 日志

.PHONY: broker api orchestrator orchestrator-bg pwa pwa-bg tunnel tunnel-bg \
        stop ps logs tunnel-url trace go2-logs go2-state go2-logs-window

SSM_DIR     := $(CURDIR)
LOG_DIR     := $(SSM_DIR)/logs
BROKER_CONF := $(LOG_DIR)/mosquitto.conf
PUBLIC_URL  := https://ssm.eliottxu.top
TUNNEL_CMD  := env HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
                   cloudflared tunnel run ssm

# 后台启动宏：$(1)=pkill 模式  $(2)=启动命令  $(3)=日志名
define bg
	@mkdir -p $(LOG_DIR)
	@pkill -f "$(1)" || true
	@sleep 1
	nohup $(2) > $(LOG_DIR)/$(3).log 2>&1 &
	@echo "$(3) 已后台启动 → logs/$(3).log"
endef

# ── Broker ──────────────────────────────────────────────────
broker:
	@mkdir -p $(LOG_DIR)/broker
	@pkill -f "[m]osquitto" || true
	@sleep 1
	@SSM_DIR=$(SSM_DIR) envsubst < infra/broker/mosquitto.conf.tmpl > $(BROKER_CONF)
	mosquitto -c $(BROKER_CONF) -d
	@echo "Broker started: TCP :1883  WS :9001"
	@tail -f $(LOG_DIR)/mosquitto.log

# ── API ──────────────────────────────────────────────────────
api:
	@mkdir -p $(LOG_DIR)
	@pkill -9 -f "[u]vicorn cloud.api.main" || true
	@sleep 1
	uv run uvicorn cloud.api.main:app --host 127.0.0.1 --port 8082 --reload 2>&1 | tee $(LOG_DIR)/api.log

# ── Orchestrator ─────────────────────────────────────────────
orchestrator:
	@while true; do \
		PYTHONPATH=$(SSM_DIR) uv run python -u cloud/orchestrator/main.py; \
		echo "[`date '+%Y-%m-%d %H:%M:%S'`] Orchestrator 退出，3 秒后自动重启... (Ctrl+C 停止)"; \
		sleep 3; \
	done

orchestrator-bg:
	$(call bg,cloud/orchestrator/main.py,PYTHONPATH=$(SSM_DIR) uv run python -u cloud/orchestrator/main.py,orchestrator)

# ── PWA ──────────────────────────────────────────────────────
pwa:
	uv run python app/serve.py

pwa-bg:
	$(call bg,[h]ttp.server 8081,uv run python app/serve.py,pwa)

# ── Cloudflare Tunnel ────────────────────────────────────────
tunnel:
	$(TUNNEL_CMD)

tunnel-bg:
	$(call bg,[c]loudflared,$(TUNNEL_CMD),tunnel)
	@sleep 3
	@echo "  Public URL: $(PUBLIC_URL)"

# ── 停止 ─────────────────────────────────────────────────────
stop:
	@pkill -9 -f "[m]osquitto"                || true
	@pkill -9 -f "[u]vicorn cloud.api.main"   || true
	@pkill -9 -f "cloud/orchestrator/main.py" || true
	@pkill -9 -f "[a]pp/serve.py"             || true
	@pkill -9 -f "[c]loudflared"              || true
	@echo "All SSM services stopped."

# ── 工具 ─────────────────────────────────────────────────────
ps:
	@pgrep -a -f "mosquitto|uvicorn|app/serve\.py|cloudflared|orchestrator/main\.py" || echo "(none)"

logs:
	@echo "=== API ===" && tail -20 logs/api.log 2>/dev/null || echo "(no log)"
	@echo "=== Orchestrator ===" && tail -20 logs/orchestrator.log 2>/dev/null || echo "(no log)"
	@echo "=== PWA ===" && tail -5 logs/pwa.log 2>/dev/null || echo "(no log)"
	@echo "=== tunnel ===" && tail -5 logs/tunnel.log 2>/dev/null || echo "(no log)"

tunnel-url:
	@echo "$(PUBLIC_URL)"

trace:
	@while true; do \
		mosquitto_sub -h 127.0.0.1 -p 1883 -u ssm_user -P Wl4sErQrlrpEbm7r -t "ssm/#" -v \
			| awk '{ print strftime("%Y-%m-%d %H:%M:%S"), $$0; fflush() }'; \
		echo "[`date '+%Y-%m-%d %H:%M:%S'`] MQTT 连接断开，2 秒后重连..."; \
		sleep 2; \
	done

go2-logs:
	@tail -f $(LOG_DIR)/go2.log | grep --line-buffered --text -v "\[Go2/Status\]"

go2-state:
	@tail -f $(LOG_DIR)/go2.log | grep --line-buffered --text "\[Go2/Status\]"

go2-logs-window:
	@tmux new-window -n go2-events "tail -f $(LOG_DIR)/go2.log | grep --line-buffered --text -v '\\[Go2/Status\\]'"
	@tmux new-window -n go2-state  "tail -f $(LOG_DIR)/go2.log | grep --line-buffered --text '\\[Go2/Status\\]'"

