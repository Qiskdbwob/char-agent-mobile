/** Session Manager — single unified session: attach + thread loading. */

import { api } from './api-client.js';
import { wsManager } from './ws-manager.js';

const UNIFIED_KEY = 'websocket:default';

export class SessionManager {
  constructor() {
    this.currentKey = UNIFIED_KEY;
    this.currentScope = null;
    this.runStartedAt = null;
    this._initialized = false;
  }

  init() {
    if (this._initialized) return;
    this._initialized = true;
    this.ensureAttached();
  }

  /** Attach the shared chat (no-op if already attached; re-attach on reconnect is automatic). */
  ensureAttached() {
    wsManager.attachChat(this.currentKey);
  }

  async loadThread(key, limit = 160, before = null) {
    const data = await api.fetchWebuiThread(key, { limit, before });
    if (data && typeof data === 'object') {
      this.currentScope = data.workspace_scope || null;
      this.runStartedAt = data.run_started_at || null;
      this.context = data.context || null;
    }
    return data;
  }
}

export const sessionManager = new SessionManager();
