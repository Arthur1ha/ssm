/* Device controls share result handling; transport acceptance is never completion. */
const commandBus = {
  async command(unitId, action, params = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), action === 'chat' ? 60000 : 30000);
    try {
      const data = await apiClient.request(`/api/commands/${encodeURIComponent(unitId)}`, {
        method: 'POST', body: { action, params }, signal: controller.signal,
      });
      if (data?.result && typeof data.result === 'object') return data.result;
      if (data?.status === 'dispatched' || data?.accepted === true || data?.ok === true && !data?.result) {
        return { ...data, result: 'accepted' };
      }
      return data;
    } catch (error) {
      return { result: error.name === 'AbortError' ? 'timeout' : 'error', error: error.message };
    } finally { clearTimeout(timer); }
  },

  mqtt(unitId, action, params) {
    return this.command(unitId, action, params);
  },

  describe(result) {
    if (result?.error) {
      try {
        const detail = JSON.parse(result.error);
        return detail?.message || detail?.detail?.message || result.error;
      } catch (_) {
        return result.error;
      }
    }
    const messages = {
      ok: '执行完成', accepted: '指令已发送，尚未确认完成', executing: '正在执行',
      blocked: '当前状态或参数不允许执行', cancelled: '动作已被后续指令打断',
      timeout: '等待完成反馈超时，结果尚未确认', error: '执行失败',
    };
    return messages[result?.result] || '未收到有效执行结果';
  },

  // Serialize joystick writes, coalescing movement samples. A release always follows the in-flight sample.
  _velocities: new Map(),
  velocity(unitId, params, report) {
    let entry = this._velocities.get(unitId);
    if (!entry) {
      entry = { next: null, running: false };
      this._velocities.set(unitId, entry);
    }
    entry.next = { params, report };
    if (entry.running) return;
    entry.running = true;
    const drain = async () => {
      try {
        while (entry.next) {
          const item = entry.next;
          entry.next = null;
          const result = await this.command(unitId, 'velocity', item.params);
          item.report(result);
        }
      } finally { entry.running = false; }
    };
    drain();
  },
};
