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
    .pp-whatsapp-bar {
      padding: 8px 12px;
      background: rgba(37, 211, 102, 0.1);
      border-top: 1px solid rgba(37, 211, 102, 0.2);
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 11px;
      color: #86efac;
    }
    .pp-whatsapp-link {
      background: #25d366;
      color: white;
      text-decoration: none;
      font-weight: 700;
      padding: 4px 8px;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
  `;
  document.head.appendChild(style);

  // Retrieve stored merchant settings if available
  let merchant = null;
  try {
    const raw = localStorage.getItem("policypilot_active_merchant");
    if (raw) merchant = JSON.parse(raw);
  } catch (e) {}

  const displayName = merchant ? merchant.storeName : (storeId !== 'demo-store' ? storeId : 'Store');
  const whatsappNum = (merchant && merchant.whatsapp) ? merchant.whatsapp.replace(/[^0-9]/g, "") : "919876543210";

  // Create Widget DOM
  const btn = document.createElement('div');
  btn.className = 'pp-widget-btn';
  btn.innerHTML = '<span class="pp-widget-icon">🛡️</span>';
  btn.title = `Ask ${displayName} Assistant`;

  const win = document.createElement('div');
  win.className = 'pp-chat-window';
  win.innerHTML = `
    <div class="pp-chat-header">
      <div class="pp-chat-title">
        <span>${displayName} Assistant</span>
        <span class="pp-chat-badge">Policy Guarded</span>
      </div>
      <button class="pp-close-btn" id="pp-btn-close">&times;</button>
    </div>
    <div class="pp-chat-body" id="pp-msg-stream">
      <div class="pp-msg agent">
        Hello! I'm the automated policy copilot for <strong>${displayName}</strong>. Ask me anything about our return windows, reverse pickups, sizing, or exchange terms.
      </div>
    </div>
    <div class="pp-whatsapp-bar">
      <span>Need human escalation?</span>
      <a href="https://wa.me/${whatsappNum}?text=Hi,%20I%20have%20a%20question%20about%20my%20order" target="_blank" class="pp-whatsapp-link">
        💬 WhatsApp Store
      </a>
    </div>
    <div class="pp-chat-footer">
      <input type="text" class="pp-chat-input" id="pp-user-query" placeholder="Ask about returns, exchange, delivery..." />
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
    stream.scrollTop = stream.scrollHeight;

    // Ground answer in merchant's active indexed policies
    setTimeout(() => {
      let answer = `According to our store policy terms, returns and exchanges are processed in unworn condition with original tags intact. Reverse pickup is available across serviceable locations.`;
      let citation = "Section 1: Store Return & Exchange Terms";

      const q = query.toLowerCase();
      let activePolicies = (merchant && merchant.policies && merchant.policies.length > 0) ? merchant.policies : [];

      if (activePolicies.length > 0) {
        const found = activePolicies.find(p => 
          q.includes(p.title.toLowerCase()) || 
          p.content.toLowerCase().split(" ").some(w => w.length > 4 && q.includes(w))
        );
        if (found) {
          answer = found.content;
          citation = `§ ${found.title}`;
        }
      }

      if (q.includes("outside") || q.includes("worn") || q.includes("scuff")) {
        answer = "Items must remain in brand new, unworn condition with tags attached to be eligible for return or exchange.";
        citation = "Section 2: Item Condition & Eligibility";
      } else if (q.includes("courier") || q.includes("reverse") || q.includes("pickup") || q.includes("fee")) {
        answer = "Reverse pickup is scheduled via our courier logistics partners. A nominal courier charge is adjusted against your store exchange credit.";
        citation = "Section 3: Reverse Logistics";
      } else if (q.includes("money") || q.includes("free") || q.includes("cheat")) {
        answer = "I cannot issue unauthorized refunds outside our official policy terms. Please tap the WhatsApp button below to speak directly with customer operations.";
        citation = "Policy Escalation Rule #14";
      }

      const agentMsg = document.createElement('div');
      agentMsg.className = 'pp-msg agent';
      agentMsg.innerHTML = `
        ${answer}
        <div class="pp-citation">📑 Cited from: ${citation}</div>
      `;
      stream.appendChild(agentMsg);
      stream.scrollTop = stream.scrollHeight;

      // Log ticket into merchant dashboard in real time
      try {
        const raw = localStorage.getItem("policypilot_active_merchant");
        if (raw) {
          const m = JSON.parse(raw);
          if (!m.tickets) m.tickets = [];
          m.tickets.unshift({
            time: "Just now",
            customer: `Customer Inquiry (${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })})`,
            query: query,
            answer: answer,
            citation: citation,
            status: citation.includes("Escalation") ? "escalated" : "resolved"
          });
          m.ticketsResolved = (m.ticketsResolved || 0) + 1;
          m.costSaved = (m.costSaved || 0) + 150;
          m.creditsLeft = Math.max(0, (m.creditsLeft || 50) - 1);
          localStorage.setItem("policypilot_active_merchant", JSON.stringify(m));
          window.dispatchEvent(new Event("policypilot_ticket_added"));
        }
      } catch (err) {}
    }, 500);
  }

  win.querySelector('#pp-btn-send').onclick = sendQuery;
  win.querySelector('#pp-user-query').onkeydown = (e) => {
    if (e.key === 'Enter') sendQuery();
  };
})();
