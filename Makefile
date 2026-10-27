# SSM — 服务管理
# make <服务>        前台运行（日志直出，在 tmux 窗口里跑）
# make <服务>-bg    后台运行（幂等，重复执行会重启）
# make stop          停止所有后台服务
# make ps / logs     查看进程 / 日志

.PHONY: broker broker-bg api api-bg orchestrator orchestrator-bg pwa pwa-bg tunnel tunnel-bg \
        stop ps logs rotate-logs tunnel-url trace trace-task task-trace errors tmux-windows

SSM_DIR     := $(CURDIR)
export ID
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
	@echo "Broker starting in foreground: internal/device TCP :1883"
	mosquitto -c $(BROKER_CONF)

broker-bg:
	@mkdir -p $(LOG_DIR)/broker
	@pkill -f "[m]osquitto" || true
	@sleep 1
	@SSM_DIR=$(SSM_DIR) envsubst < infra/broker/mosquitto.conf.tmpl > $(BROKER_CONF)
	@mosquitto -c $(BROKER_CONF) -d
	@echo "Broker 已后台启动 → logs/mosquitto.log"

# ── API ──────────────────────────────────────────────────────
api:
	@mkdir -p $(LOG_DIR)
	@pkill -TERM -f "[u]vicorn cloud.api.main" || true
	@for i in $$(seq 1 15); do \
		pgrep -f "[u]vicorn cloud.api.main" >/dev/null || break; \
		sleep 1; \
	done
	@if pgrep -f "[u]vicorn cloud.api.main" >/dev/null; then \
		echo "API 进程未在 15 秒内退出，请检查旧进程"; exit 1; \
	fi
	uv run uvicorn cloud.api.main:app --host 127.0.0.1 --port 8082 --reload --timeout-graceful-shutdown 10 2>&1 | tee $(LOG_DIR)/api.log

api-bg:
	$(call bg,[u]vicorn cloud.api.main,uv run uvicorn cloud.api.main:app --host 127.0.0.1 --port 8082 --timeout-graceful-shutdown 10,api)

# ── Orchestrator ─────────────────────────────────────────────
orchestrator:
	@while true; do \
		PYTHONPATH=$(SSM_DIR) uv run python -u cloud/orchestrator/main.py; \
		echo "[`date '+%Y-%m-%d %H:%M:%S'`] Orchestrator 退出，3 秒后自动重启... (Ctrl+C 停止)"; \
		sleep 3; \
	done

orchestrator-bg:
	$(call bg,[c]loud/orchestrator/main.py,env PYTHONPATH=$(SSM_DIR) uv run python -u cloud/orchestrator/main.py,orchestrator)

# ── PWA ──────────────────────────────────────────────────────
pwa:
	uv run python app/serve.py

pwa-bg:
	$(call bg,[a]pp/serve.py,uv run python app/serve.py,pwa)

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
	@pkill -9 -f "[c]loud/orchestrator/main.py" || true
	@pkill -9 -f "[a]pp/serve.py"             || true
	@pkill -9 -f "[c]loudflared"              || true
	@echo "All SSM services stopped."

# ── 工具 ─────────────────────────────────────────────────────
ps:
	@pgrep -a -f "mosquitto|uvicorn|app/serve\.py|cloudflared|orchestrator/main\.py" || echo "(none)"

logs:
	@echo "=== Latest session ==="
	@uv run python scripts/trace_task.py --latest || true
	@echo "=== Recent agent errors ==="
	@uv run python scripts/trace_task.py --errors --limit 8 || true

rotate-logs:
	@mkdir -p $(LOG_DIR)
	@SSM_DIR='$(SSM_DIR)' SSM_LOG_USER="$$(id -un)" SSM_LOG_GROUP="$$(id -gn)" \
		envsubst < infra/logrotate/ssm.conf.tmpl > $(LOG_DIR)/logrotate.conf
	@logrotate -s $(LOG_DIR)/.logrotate.state $(LOG_DIR)/logrotate.conf

trace-task:
	@if [ -z "$$ID" ]; then echo "Usage: make trace-task ID=<session_id> [JSON=1]" >&2; exit 2; fi
	@uv run python scripts/trace_task.py "$$ID" $(if $(filter 1 true yes,$(JSON)),--json,) $(if $(filter 1 true yes,$(FOLLOW)),--follow,)

task-trace:
	@if [ -n "$$ID" ]; then uv run python scripts/trace_task.py "$$ID" --follow; else uv run python scripts/trace_task.py --latest --follow; fi

errors:
	@uv run python scripts/trace_task.py --errors --follow

tmux-windows:
	@bash scripts/tmux_views.sh "$${ID:-latest}"

tunnel-url:
	@echo "$(PUBLIC_URL)"

trace:
	@set -a; . cloud/.env; set +a; while true; do \
		mosquitto_sub -h 127.0.0.1 -p 1883 -u "$$MQTT_USER" -P "$$MQTT_PASSWORD" -t "ssm/#" -v \
			| awk '{ print strftime("%Y-%m-%d %H:%M:%S"), $$0; fflush() }'; \
		echo "[`date '+%Y-%m-%d %H:%M:%S'`] MQTT 连接断开，2 秒后重连..."; \
		sleep 2; \
	done
