/**
 * PolicyPilot AI - Embeddable Storefront Chat Widget
 * Drop-in script for Shopify, WooCommerce, Webflow, and custom websites.
 */
(function() {
  if (window.PolicyPilotWidgetLoaded) return;
  window.PolicyPilotWidgetLoaded = true;

  const currentScript = document.currentScript || document.querySelector('script[src*="widget.js"]');
  const storeId = currentScript ? currentScript.getAttribute('data-store-id') || 'demo-store' : 'demo-store';
  const theme = currentScript ? currentScript.getAttribute('data-theme') || 'dark' : 'dark';

  // Inject Widget Styles
  const style = document.createElement('style');
  style.textContent = `
    .pp-widget-btn {
      position: fixed;
      bottom: 24px;
      right: 24px;
      width: 58px;
      height: 58px;
      border-radius: 50%;
      background: linear-gradient(135deg, #6366f1 0%, #06b6d4 100%);
      box-shadow: 0 8px 24px rgba(99, 102, 241, 0.4);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      z-index: 999999;
      transition: transform 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275);
      border: 2px solid rgba(255, 255, 255, 0.2);
    }
    .pp-widget-btn:hover {
      transform: scale(1.08);
    }
    .pp-widget-icon {
      font-size: 26px;
      user-select: none;
    }
    .pp-chat-window {
      position: fixed;
      bottom: 96px;
      right: 24px;
      width: 380px;
      height: 520px;
      max-width: calc(100vw - 48px);
      max-height: calc(100vh - 120px);
      background: #0f172a;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 16px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
      display: none;
      flex-direction: column;
      overflow: hidden;
      z-index: 999999;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      animation: ppFadeIn 0.25s ease;
    }
    @keyframes ppFadeIn {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .pp-chat-header {
      background: #1e293b;
      padding: 14px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      color: white;
    }
    .pp-chat-title {
      font-size: 14px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .pp-chat-badge {
      font-size: 10px;
      background: rgba(16, 185, 129, 0.2);
      color: #10b981;
      padding: 2px 6px;
      border-radius: 10px;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .pp-close-btn {
      background: none;
      border: none;
      color: #94a3b8;
      font-size: 18px;
      cursor: pointer;
    }
    .pp-chat-body {
      flex: 1;
      padding: 16px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
      background: #0b0f19;
    }
    .pp-msg {
      max-width: 84%;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 13px;
      line-height: 1.45;
    }
    .pp-msg.agent {
      background: #1e293b;
      color: #f1f5f9;
      align-self: flex-start;
      border-bottom-left-radius: 2px;
    }
    .pp-msg.user {
      background: #6366f1;
      color: white;
      align-self: flex-end;
      border-bottom-right-radius: 2px;
    }
    .pp-citation {
      font-size: 11px;
      color: #a5b4fc;
      margin-top: 6px;
      font-family: monospace;
    }
    .pp-chat-footer {
      padding: 12px;
      background: #131b2e;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      display: flex;
      gap: 8px;
    }
    .pp-chat-input {
      flex: 1;
      background: #090d16;
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 8px;
      padding: 8px 12px;
      color: white;
      font-size: 13px;
      outline: none;
    }
    .pp-send-btn {
      background: #6366f1;
      border: none;
      color: white;
      border-radius: 8px;
      padding: 0 14px;
      cursor: pointer;
      font-weight: 600;
    }
  `;
  document.head.appendChild(style);

  // Create Widget DOM
  const btn = document.createElement('div');
  btn.className = 'pp-widget-btn';
  btn.innerHTML = '<span class="pp-widget-icon">🛡️</span>';
  btn.title = "Ask Store Policy Assistant";

  const win = document.createElement('div');
  win.className = 'pp-chat-window';
  win.innerHTML = `
    <div class="pp-chat-header">
      <div class="pp-chat-title">
        <span>Store Assistant</span>
        <span class="pp-chat-badge">Policy Guarded</span>
      </div>
      <button class="pp-close-btn" id="pp-btn-close">&times;</button>
    </div>
    <div class="pp-chat-body" id="pp-msg-stream">
      <div class="pp-msg agent">
        Hi! How can I help you today? Ask about our return windows, shipping, or warranty policies.
      </div>
    </div>
    <div class="pp-chat-footer">
      <input type="text" class="pp-chat-input" id="pp-user-query" placeholder="Ask a question..." />
      <button class="pp-send-btn" id="pp-btn-send">Send</button>
    </div>
  `;

  document.body.appendChild(btn);
  document.body.appendChild(win);

  let isOpen = false;
  btn.onclick = () => {
    isOpen = !isOpen;
    win.style.display = isOpen ? 'flex' : 'none';
  };

  win.querySelector('#pp-btn-close').onclick = () => {
    isOpen = false;
    win.style.display = 'none';
  };

  function sendQuery() {
    const input = win.querySelector('#pp-user-query');
    const query = input.value.trim();
    if (!query) return;

    const stream = win.querySelector('#pp-msg-stream');
    
    // User bubble
    const userMsg = document.createElement('div');
    userMsg.className = 'pp-msg user';
    userMsg.textContent = query;
    stream.appendChild(userMsg);
    input.value = '';

    // Agent answer simulation
    setTimeout(() => {
      const agentMsg = document.createElement('div');
      agentMsg.className = 'pp-msg agent';
      agentMsg.innerHTML = `
        Thank you for asking. According to store policy, eligible items can be returned within 30 days of delivery in original condition.
        <div class="pp-citation">📑 Cited from: Section 2: Returns & Refunds</div>
      `;
      stream.appendChild(agentMsg);
      stream.scrollTop = stream.scrollHeight;
    }, 600);
  }

  win.querySelector('#pp-btn-send').onclick = sendQuery;
  win.querySelector('#pp-user-query').onkeydown = (e) => {
    if (e.key === 'Enter') sendQuery();
  };
})();
