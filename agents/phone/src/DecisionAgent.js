/**
 * DecisionAgent.js — 已简化为「人类 Override 标志」管理器。
 *
 * JS 规则引擎已移除，决策由 PC LangGraph 负责。
 * 手机端的手动控制直接发 command，不需要经过此模块。
 *
 * 保留此文件仅为了向 ESP32 发布 ssm/decision/active 标志：
 *   - 手机连接时不主动抢控（false），让 PC Agent 或 ESP32 local_rules 运行
 *   - 未来若需要「手机接管」可在此扩展
 */

class DecisionAgent {
    constructor(bus) {
        this._bus = bus;

        // 手机上线时声明不抢控
        bus.onConnect(() => {
            bus.publish('ssm/decision/active', 'false', { retain: true });
        });
    }
}
