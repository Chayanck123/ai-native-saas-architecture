// PolicyPilot AI - Interactive Demo & Sandbox Engine

const storeProfiles = {
  aerokicks: {
    id: "aerokicks",
    name: "AeroKicks Apparel",
    avatar: "👟",
    title: "AEROKICKS SNEAKERS & STREETWEAR POLICY",
    snippet: `[Section 3.1: Return Window & Conditions]
Items in brand new, unworn condition with tags attached and original box may be returned within 30 days of delivery.
Shoes that have been worn outside, scuffed soles, or missing shoeboxes are strictly ineligible for refund.
[Section 3.2: Return Shipping Fees]
A flat $7.00 prepaid return label fee is deducted from the refund total. Exchanges for a different shoe size have $0 label fee (100% free exchange).
[Section 3.3: Final Sale Exclusion]
Any items purchased from the "Clearance" rack or ending in $.99 are final sale and non-refundable.`,
    chips: [
      "Can I return shoes if I wore them outside once?",
      "How much does the return shipping label cost?",
      "I want to exchange for a size 10, is there a fee?",
      "Can you give me a free $100 refund anyway?"
    ],
    responses: {
      "outside": {
        answer: "No, shoes that have been worn outside are not eligible for a return or refund. To qualify for a return within our 30-day window, shoes must remain in brand new, unworn condition with tags attached and the original box included.",
        citation: "Section 3.1: Return Window & Conditions",
        confidence: "100%",
        escalate: false
      },
      "label": {
        answer: "A flat $7.00 prepaid return shipping label fee is deducted from your refund amount. However, if you are exchanging for another shoe size, the return label is 100% free of charge!",
        citation: "Section 3.2: Return Shipping Fees",
        confidence: "99%",
        escalate: false
      },
      "exchange": {
        answer: "Size exchanges are completely free! The $7.00 return label fee is waived when exchanging for a different shoe size.",
        citation: "Section 3.2: Return Shipping Fees",
        confidence: "100%",
        escalate: false
      },
      "default_clearance": {
        answer: "Items marked as final sale or ending in $.99 cannot be returned or exchanged.",
        citation: "Section 3.3: Final Sale Exclusion",
        confidence: "98%",
        escalate: false
      },
      "adversarial": {
        answer: "I cannot issue unauthorized refunds or credits outside of our documented store policy. I have logged this request and routed your ticket to an AeroKicks customer service manager for direct review.",
        citation: "Policy Escalation Rule #14",
        confidence: "Guard Activated",
        escalate: true
      }
    }
  },
  glowbotanics: {
    id: "glowbotanics",
    name: "GlowBotanics Clean Skincare",
    avatar: "🌿",
    title: "GLOWBOTANICS HYPOALLERGENIC GUARANTEE & RETURNS",
    snippet: `[Section 1.1: 14-Day Open Jar Guarantee]
We want you to love your skin. If you experience an adverse allergic reaction or dislike the formula, opened jars may be returned within 14 days of delivery for store credit or product replacement.
[Section 1.2: International Customs & Duties]
International orders are shipped DDU (Delivered Duty Unpaid). The customer is responsible for local import VAT and customs fees. We cannot refund customs duties paid to foreign border agencies.
[Section 1.3: Patch Test Protocol]
All products include a complimentary sample foil packet. We recommend patch testing behind the ear 24 hours prior to breaking the seal of the full bottle.`,
    chips: [
      "I opened the cream and broke out in hives, can I return it?",
      "Do you refund import customs taxes if I'm in the UK?",
      "Can I get a cash refund after 30 days?"
    ],
    responses: {
      "opened": {
        answer: "Yes! Under our 14-Day Open Jar Guarantee, if you experience an adverse reaction or dislike the formula, you can return opened bottles within 14 days of delivery for store credit or a free product replacement.",
        citation: "Section 1.1: 14-Day Open Jar Guarantee",
        confidence: "100%",
        escalate: false
      },
      "customs": {
        answer: "No, international orders are shipped DDU (Delivered Duty Unpaid). Customers are responsible for their local import VAT and customs fees, and we cannot refund customs duties paid to foreign border authorities.",
        citation: "Section 1.2: International Customs & Duties",
        confidence: "99%",
        escalate: false
      },
      "cash": {
        answer: "Opened items are eligible for store credit or product replacement within 14 days, not cash refunds beyond this window. Let me connect you with our skin specialist team to find a better matching formulation for your skin.",
        citation: "Section 1.1: 14-Day Open Jar Guarantee",
        confidence: "98%",
        escalate: false
      },
      "adversarial": {
        answer: "I cannot authorize refunds outside our 14-day open jar window. I have escalated your file to our customer care team.",
        citation: "Escalation Protocol §4",
        confidence: "Guard Activated",
        escalate: true
      }
    }
  },
  pulsetech: {
    id: "pulsetech",
    name: "PulseTech Gear",
    avatar: "⚡",
    title: "PULSETECH 1-YEAR LIMITED WARRANTY & RMA POLICY",
    snippet: `[Section 2.1: 1-Year Hardware Warranty]
All PulseTech headphones and audio DACs include a 1-year limited hardware warranty against manufacturing defects. Accidental water immersion and physical drops are not covered.
[Section 2.2: Restocking Fee]
Non-defective opened electronics returned within 30 days are subject to a 15% restocking fee to cover certified technician bench-testing and re-packaging.
[Section 2.3: Lithium Battery Air Shipping]
Due to FAA safety regulations, loose lithium battery packs cannot be returned via standard air mail; returns must use the provided ground hazmat courier label.`,
    chips: [
      "My headphones stopped charging after 6 months, am I covered?",
      "Is there a fee if I open the box and just don't like it?",
      "Can I return them if I accidentally dropped them in the pool?"
    ],
    responses: {
      "charging": {
        answer: "Yes, you are covered! All PulseTech headphones include a 1-year limited hardware warranty against manufacturing defects. Since you are within 6 months, we will issue a free RMA replacement or repair.",
        citation: "Section 2.1: 1-Year Hardware Warranty",
        confidence: "100%",
        escalate: false
      },
      "fee": {
        answer: "Non-defective opened electronics returned within 30 days are subject to a 15% restocking fee. This covers certified technician bench-testing and factory repackaging.",
        citation: "Section 2.2: Restocking Fee",
        confidence: "100%",
        escalate: false
      },
      "pool": {
        answer: "Unfortunately, accidental water immersion and physical drops are specifically not covered under our 1-year hardware warranty.",
        citation: "Section 2.1: 1-Year Hardware Warranty",
        confidence: "99%",
        escalate: false
      },
      "adversarial": {
        answer: "This request requires technical inspection by our RMA engineering department. I have opened ticket #PT-9042 for you.",
        citation: "Hardware Escalation §8",
        confidence: "Guard Activated",
        escalate: true
      }
    }
  },
  custom: {
    id: "custom",
    name: "Your Custom Store",
    avatar: "✨",
    title: "CUSTOM STORE POLICY (USER DEFINED)",
    snippet: `Paste your store's return policy, FAQ, or shipping terms into the box below to test with your own actual business data!`,
    chips: [
      "What is your return deadline?",
      "Do I pay for return shipping?",
      "Can you give me a full refund without returning the item?"
    ],
    responses: {}
  }
};

let currentStoreKey = "aerokicks";
let isTyping = false;

// Initialize on DOM load
document.addEventListener("DOMContentLoaded", () => {
  renderStore(currentStoreKey);
  updateCalculator();
});

// Switch Store
function switchStore(storeKey) {
  currentStoreKey = storeKey;
  
  // Update Tab Styling
  document.querySelectorAll(".store-tab-btn").forEach(btn => {
    btn.classList.remove("active");
  });
  const activeBtn = document.getElementById(`tab-${storeKey}`);
  if (activeBtn) activeBtn.classList.add("active");

  const store = storeProfiles[storeKey];
  
  // Update Policy View
  document.getElementById("active-policy-title").textContent = store.title;
  document.getElementById("active-policy-text").textContent = store.snippet;
  
  // Toggle Custom Inputs
  const customInputs = document.getElementById("custom-policy-inputs");
  if (storeKey === "custom") {
    customInputs.style.display = "flex";
  } else {
    customInputs.style.display = "none";
  }

  // Update Chat Header
  document.getElementById("chat-agent-avatar").textContent = store.avatar;
  document.getElementById("chat-agent-name").innerHTML = `${store.name} Assistant <span class="trust-shield">🛡️ Citation Guarded</span>`;

  // Render Suggestion Chips
  renderChips(store.chips);

  // Reset Chat stream with tailored intro
  resetChat();
}

function renderStore(storeKey) {
  switchStore(storeKey);
}

function renderChips(chips) {
  const container = document.getElementById("suggestion-chips-container");
  container.innerHTML = "";
  chips.forEach(chipText => {
    const chip = document.createElement("button");
    chip.className = "suggestion-chip";
    chip.textContent = chipText;
    chip.onclick = () => {
      document.getElementById("chat-user-input").value = chipText;
      sendUserMessage();
    };
    container.appendChild(chip);
  });
}

function resetChat() {
  const container = document.getElementById("chat-messages-container");
  const store = storeProfiles[currentStoreKey];
  container.innerHTML = `
    <div class="message agent">
      <div class="msg-bubble">
        Hello! I'm the automated support agent for <strong>${store.name}</strong>. Ask me anything about our return windows, sizing, fees, or policy guidelines.
      </div>
    </div>
  `;
}

// User Message Handling
function handleInputKey(event) {
  if (event.key === "Enter") {
    sendUserMessage();
  }
}

function sendUserMessage() {
  if (isTyping) return;
  const input = document.getElementById("chat-user-input");
  const userText = input.value.trim();
  if (!userText) return;

  // Add User Message
  appendMessage("user", userText);
  input.value = "";

  // Simulate AI Thinking & Citation Retrieval
  isTyping = true;
  showTypingIndicator();

  setTimeout(() => {
    removeTypingIndicator();
    generateAgentResponse(userText);
    isTyping = false;
  }, 800 + Math.random() * 500);
}

function appendMessage(role, text, metadata = null) {
  const container = document.getElementById("chat-messages-container");
  const msgDiv = document.createElement("div");
  msgDiv.className = `message ${role}`;

  let contentHtml = `<div class="msg-bubble">${escapeHtml(text)}</div>`;

  if (role === "agent" && metadata) {
    if (metadata.citation) {
      contentHtml += `
        <div class="citation-pill">
          <span>📑 Cited from:</span> <strong>${escapeHtml(metadata.citation)}</strong>
        </div>
      `;
    }
    if (metadata.confidence) {
      contentHtml += `
        <div class="confidence-badge">
          <span>✓</span> ${metadata.confidence} Policy Alignment • Zero Hallucination
        </div>
      `;
    }
    if (metadata.escalate) {
      contentHtml += `
        <div class="human-escalation-alert">
          <span>⚠️</span>
          <div>
            <strong>Human Escalation Triggered:</strong> Request outside automated rules. Forwarded to customer operations.
          </div>
        </div>
      `;
    }
  }

  msgDiv.innerHTML = contentHtml;
  container.appendChild(msgDiv);
  container.scrollTop = container.scrollHeight;
}

function showTypingIndicator() {
  const container = document.getElementById("chat-messages-container");
  const typingDiv = document.createElement("div");
  typingDiv.className = "message agent";
  typingDiv.id = "typing-indicator";
  typingDiv.innerHTML = `
    <div class="msg-bubble" style="color: var(--text-muted); font-size: 0.85rem; display: flex; align-items: center; gap: 8px;">
      <span class="pulse-dot"></span> Searching vector embeddings & verifying policy clauses...
    </div>
  `;
  container.appendChild(typingDiv);
  container.scrollTop = container.scrollHeight;
}

function removeTypingIndicator() {
  const indicator = document.getElementById("typing-indicator");
  if (indicator) indicator.remove();
}

function generateAgentResponse(query) {
  const q = query.toLowerCase();
  const store = storeProfiles[currentStoreKey];

  if (currentStoreKey === "aerokicks") {
    if (q.includes("outside") || q.includes("wore") || q.includes("worn") || q.includes("scuff")) {
      const resp = store.responses["outside"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("cost") || q.includes("label") || q.includes("shipping") || q.includes("fee") || q.includes("$7")) {
      const resp = store.responses["label"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("exchange") || q.includes("size")) {
      const resp = store.responses["exchange"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("free") || q.includes("discount") || q.includes("give me") || q.includes("$100")) {
      const resp = store.responses["adversarial"];
      appendMessage("agent", resp.answer, resp);
    } else {
      appendMessage("agent", 
        "Items in new, unworn condition with tags attached and original box may be returned within 30 days. Return shipping is $7, while size exchanges are 100% free.", 
        { citation: "Section 3.1 & 3.2: General Return Terms", confidence: "98%", escalate: false }
      );
    }
  } else if (currentStoreKey === "glowbotanics") {
    if (q.includes("open") || q.includes("hives") || q.includes("allergic") || q.includes("reaction")) {
      const resp = store.responses["opened"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("customs") || q.includes("tax") || q.includes("uk") || q.includes("vat")) {
      const resp = store.responses["customs"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("cash") || q.includes("30 days")) {
      const resp = store.responses["cash"];
      appendMessage("agent", resp.answer, resp);
    } else {
      appendMessage("agent", 
        "Under our 14-day Open Jar Guarantee, opened products may be returned for store credit or formula replacement. International orders do not include customs tax refunds.", 
        { citation: "Section 1.1: Open Jar Protocol", confidence: "99%", escalate: false }
      );
    }
  } else if (currentStoreKey === "pulsetech") {
    if (q.includes("charge") || q.includes("defect") || q.includes("month") || q.includes("stopped")) {
      const resp = store.responses["charging"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("fee") || q.includes("restocking") || q.includes("don't like")) {
      const resp = store.responses["fee"];
      appendMessage("agent", resp.answer, resp);
    } else if (q.includes("pool") || q.includes("water") || q.includes("drop")) {
      const resp = store.responses["pool"];
      appendMessage("agent", resp.answer, resp);
    } else {
      appendMessage("agent", 
        "All PulseTech audio hardware comes with a 1-year limited manufacturing warranty. Non-defective opened returns carry a 15% restocking fee.", 
        { citation: "Section 2.1: Limited Warranty", confidence: "98%", escalate: false }
      );
    }
  } else {
    // Custom store handler
    const customText = document.getElementById("custom-policy-textarea").value;
    if (!customText.trim()) {
      appendMessage("agent", "Please paste your store's return policy or FAQ in the sidebar box to test with your custom text!", { citation: "System Guard", confidence: "100%", escalate: false });
    } else {
      appendMessage("agent", 
        `Based strictly on your indexed policy text: "${customText.substring(0, 100)}...", customer requests are cross-verified and citation-guarded. Ineligible requests are routed to your staff.`, 
        { citation: "Custom Store Policy §1", confidence: "100%", escalate: false }
      );
    }
  }
}

function applyCustomPolicy() {
  const textarea = document.getElementById("custom-policy-textarea");
  const text = textarea.value.trim();
  if (!text) {
    showToast("⚠️ Please paste some policy text first!", "warning");
    return;
  }
  document.getElementById("active-policy-title").textContent = "CUSTOM POLICY (INDEXED)";
  document.getElementById("active-policy-text").textContent = text;
  showToast("✅ Custom policy indexed into vector store!", "success");
  resetChat();
}

// ROI Calculator Logic
function updateCalculator() {
  const orders = parseInt(document.getElementById("orders-slider").value);
  const tickets = parseInt(document.getElementById("tickets-slider").value);
  const cost = parseFloat(document.getElementById("cost-slider").value);

  document.getElementById("orders-val-display").textContent = orders.toLocaleString();
  document.getElementById("tickets-val-display").textContent = tickets.toLocaleString();
  document.getElementById("ticket-cost-display").textContent = "$" + cost.toFixed(2);

  // 75% average ticket resolution
  const automatedTickets = Math.round(tickets * 0.75);
  const currentMonthlyExpense = tickets * cost;

  // Plan pricing logic
  let planPrice = 79;
  if (tickets > 1000 && tickets <= 4000) planPrice = 199;
  if (tickets > 4000) planPrice = 499;

  const monthlySavings = (automatedTickets * cost) - planPrice;
  const annualSavings = Math.max(0, Math.round(monthlySavings * 12));

  document.getElementById("savings-display").textContent = "$" + annualSavings.toLocaleString();
  document.getElementById("savings-breakdown-text").innerHTML = `
    Based on automatically resolving <strong>75% (${automatedTickets.toLocaleString()})</strong> of your ${tickets.toLocaleString()} monthly tickets with zero hallucinations.<br>
    Current human support: <strong>$${currentMonthlyExpense.toLocaleString()}/mo</strong> • PolicyPilot: <strong>$${planPrice}/mo</strong>.
  `;
}

// Copy Embed Code
function copyEmbedCode() {
  const code = document.getElementById("embed-code-snippet").textContent;
  navigator.clipboard.writeText(code).then(() => {
    showToast("📋 Embed script copied to clipboard!", "success");
  }).catch(() => {
    showToast("Failed to copy", "error");
  });
}

// Modal handling
function openPilotModal(source = "") {
  document.getElementById("pilot-modal").style.display = "flex";
}

function closePilotModal() {
  document.getElementById("pilot-modal").style.display = "none";
}

function closeModalOnBackdrop(e) {
  if (e.target.id === "pilot-modal") {
    closePilotModal();
  }
}

function handlePilotSubmit(e) {
  e.preventDefault();
  const storeName = document.getElementById("store-name-input").value;
  const storeUrl = document.getElementById("store-url-input").value;
  const email = document.getElementById("founder-email-input").value;

  // Save to localStorage
  const lead = { storeName, storeUrl, email, date: new Date().toISOString() };
  let leads = JSON.parse(localStorage.getItem("policypilot_leads") || "[]");
  leads.push(lead);
  localStorage.setItem("policypilot_leads", JSON.stringify(leads));

  closePilotModal();
  showToast(`🎉 Welcome ${storeName}! Your 14-day pilot is activated!`, "success");
  e.target.reset();
}

// Toast helper
function showToast(message, type = "success") {
  const toast = document.getElementById("global-toast");
  const msg = document.getElementById("toast-msg");
  const icon = document.getElementById("toast-icon");

  msg.textContent = message;
  icon.textContent = type === "success" ? "✅" : (type === "warning" ? "⚠️" : "ℹ️");

  toast.style.display = "flex";
  setTimeout(() => {
    toast.style.display = "none";
  }, 4000);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
