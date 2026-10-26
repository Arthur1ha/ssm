/* Device controls share result handling; transport acceptance is never completion. */
const commandBus = {
  async command(unitId, action, params = {}) {
    const go2Chat = unitId === 'go2' && action === 'chat';
    const deviceChat = action === 'chat' && !go2Chat;
    const taskId = go2Chat ? (params.task_id || `go2_${crypto.randomUUID().replaceAll('-', '')}`) : null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), deviceChat ? 60000 : 30000);
    try {
      const data = await apiClient.request(`/api/commands/${encodeURIComponent(unitId)}`, {
        method: 'POST', body: { action, params: go2Chat ? { ...params, task_id: taskId } : params }, signal: controller.signal,
      });
      if (data?.result && typeof data.result === 'object') {
        if (data.result.result === 'executing' && taskId) return this.waitForGo2Task(taskId);
        if (unitId !== 'go2' && data.result.result === 'timeout' && data.result.task_id) {
          return this.waitForLampTask(unitId, data.result.task_id, 15000);
        }
        return data.result;
      }
      if (unitId !== 'go2' && data?.task_id && data?.status === 'dispatched') {
        return this.waitForLampTask(unitId, data.task_id);
      }
      if (data?.status === 'dispatched' || data?.accepted === true || data?.ok === true && !data?.result) {
        return { ...data, result: 'accepted' };
      }
      return data;
    } catch (error) {
      if (go2Chat && (error.name === 'AbortError' || error.status === 502 || error.status === 503)) {
        return this.waitForGo2Task(taskId, error.name === 'AbortError' ? 90000 : 15000);
      }
      return { result: error.name === 'AbortError' ? 'timeout' : 'error', error: error.message };
    } finally { clearTimeout(timer); }
  },

  async waitForGo2Task(taskId, timeoutMs = 90000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), Math.min(10000, deadline - Date.now()));
      try {
        const result = await apiClient.request(`/api/commands/go2/tasks/${encodeURIComponent(taskId)}`, {
          signal: controller.signal,
        });
        if (result?.result !== 'executing') return result;
      } catch (error) {
        if (error.status === 403 || error.status === 401) {
          return { result: 'error', task_id: taskId, error: error.message };
        }
      } finally {
        clearTimeout(timer);
      }
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    return { result: 'executing', task_id: taskId };
  },

  async waitForLampTask(unitId, taskId, timeoutMs = 25000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const result = await apiClient.request(
          `/api/commands/${encodeURIComponent(unitId)}/tasks/${encodeURIComponent(taskId)}`,
        );
        if (result?.result !== 'executing') return result;
      } catch (error) {
        if ([401, 403, 404].includes(error.status)) {
          return { result: 'error', task_id: taskId, error: error.message };
        }
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    return { result: 'executing', task_id: taskId };
  },

  async cancelGo2Task(taskId) {
    return apiClient.request(`/api/commands/go2/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST', body: {},
    });
  },

  mqtt(unitId, action, params) {
    return this.command(unitId, action, params);
  },

  describe(result) {
    if (result?.result === 'needs_clarification' && result.question) return result.question;
    if (result?.error === 'stop_unconfirmed') return '停止指令尚未确认，请查看机器狗当前状态';
    if (result?.result === 'accepted') return '指令已发送，尚未确认完成';
    if (result?.result === 'executing') return '正在执行';
    if (result?.result === 'timeout') return '等待完成反馈超时，结果尚未确认';
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
      blocked: '当前状态或参数不允许执行', rejected: '指令参数或权限不符合设备要求', cancelled: '动作已被后续指令打断',
      timeout: '等待完成反馈超时，结果尚未确认', error: '执行失败',
      needs_clarification: result?.question || '请补充执行所需的信息',
    };
    return messages[result?.result] || '未收到有效执行结果';
  },

  // Serialize joystick writes, coalescing movement samples. A release always follows the in-flight sample.
  _velocities: new Map(),
  velocity(unitId, action, params, report) {
    let entry = this._velocities.get(unitId);
    if (!entry) {
      entry = { next: null, running: false };
      this._velocities.set(unitId, entry);
    }
    entry.next = { action, params, report };
    if (entry.running) return;
    entry.running = true;
    const drain = async () => {
      try {
        while (entry.next) {
          const item = entry.next;
          entry.next = null;
          const result = await this.command(unitId, item.action, item.params);
          item.report(result);
        }
      } finally { entry.running = false; }
    };
    drain();
  },
};
