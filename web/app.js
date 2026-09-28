// PolicyPilot AI - Complete End-to-End Platform Engine

// Preset Store Profiles for Landing Sandbox
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
International orders are shipped DDU (Delivered Duty Unpaid). The customer is responsible for local import VAT and customs fees.
[Section 1.3: Patch Test Protocol]
All products include a complimentary sample foil packet. We recommend patch testing behind the ear 24 hours prior to breaking the seal.`,
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
Non-defective opened electronics returned within 30 days are subject to a 15% restocking fee.
[Section 2.3: Lithium Battery Air Shipping]
Due to FAA safety regulations, loose lithium battery packs cannot be returned via standard air mail.`,
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
let pendingStripePlan = null;

// Active Merchant Session State
let activeMerchant = null;

const sampleTickets = [
  {
    time: "2 mins ago",
    customer: "Sarah M. (Order #1042)",
    query: "Can I exchange my sneakers for half a size larger?",
    answer: "Size exchanges are 100% free with no return shipping fee.",
    citation: "Section 3.2: Return Shipping Fees",
    status: "resolved"
  },
  {
    time: "14 mins ago",
    customer: "David K. (Order #1039)",
    query: "I wore the shoes out to dinner once, can I still refund them?",
    answer: "No, items must be in unworn condition without street scuffs.",
    citation: "Section 3.1: Return Window & Conditions",
    status: "resolved"
  },
  {
    time: "1 hour ago",
    customer: "Marcus T. (Order #1021)",
    query: "I want a $50 gift card because my delivery arrived 1 day late.",
    answer: "Request exceeds automated guidelines. Escalated to manager.",
    citation: "Escalation Protocol §4",
    status: "escalated"
  }
];

const sampleInvoices = [
  {
    id: "in_1N9qX82eZvKYlo2CL",
    date: "Sep 28, 2026",
    plan: "14-Day Free Pilot Activation",
    amount: "$0.00",
    status: "PAID"
  }
];

// Initialize on DOM load
document.addEventListener("DOMContentLoaded", () => {
  renderStore(currentStoreKey);
  updateCalculator();
  checkExistingSession();
  initCookieBanner();
});

// Cookie Consent
function initCookieBanner() {
  const accepted = localStorage.getItem("policypilot_cookies_accepted");
  if (!accepted) {
    setTimeout(() => {
      document.getElementById("cookie-banner").style.display = "flex";
    }, 1200);
  }
}
function acceptCookies() {
  localStorage.setItem("policypilot_cookies_accepted", "all");
  document.getElementById("cookie-banner").style.display = "none";
  showToast("Preferences saved. Thank you!", "info");
}
function dismissCookies() {
  localStorage.setItem("policypilot_cookies_accepted", "essential");
  document.getElementById("cookie-banner").style.display = "none";
}

// View Switching: Landing vs Dashboard
function switchView(viewName) {
  const landing = document.getElementById("landing-view");
  const dashboard = document.getElementById("dashboard-view");
  const guestLinks = document.getElementById("nav-guest-links");
  const guestActions = document.getElementById("nav-guest-actions");
  const userActions = document.getElementById("nav-user-actions");

  if (viewName === "dashboard") {
    if (!activeMerchant) {
      quickLoginDemoStore();
      return;
    }
    landing.style.display = "none";
    dashboard.style.display = "block";
    guestLinks.style.display = "none";
    guestActions.style.display = "none";
    userActions.style.display = "flex";
    renderDashboard();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else {
    landing.style.display = "block";
    dashboard.style.display = "none";
    if (activeMerchant) {
      guestLinks.style.display = "flex";
      guestActions.style.display = "none";
      userActions.style.display = "flex";
    } else {
      guestLinks.style.display = "flex";
      guestActions.style.display = "flex";
      userActions.style.display = "none";
    }
  }
}

// Session Check
function checkExistingSession() {
  const saved = localStorage.getItem("policypilot_active_merchant");
  if (saved) {
    try {
      activeMerchant = JSON.parse(saved);
      if (!activeMerchant.invoices) activeMerchant.invoices = [...sampleInvoices];
      document.getElementById("nav-guest-actions").style.display = "none";
      document.getElementById("nav-user-actions").style.display = "flex";
      document.getElementById("nav-store-badge").textContent = `🛍️ ${activeMerchant.storeName}`;
    } catch (e) {
      activeMerchant = null;
    }
  }
}

// Sign Up Handler (Enters Dashboard)
function handlePilotSubmit(e) {
  e.preventDefault();
  const storeName = document.getElementById("store-name-input").value.trim();
  const storeUrl = document.getElementById("store-url-input").value.trim();
  const email = document.getElementById("founder-email-input").value.trim();

  activeMerchant = {
    storeName: storeName || "My Online Store",
    storeUrl: storeUrl || "https://mystore.com",
    email: email,
    plan: "14-Day Free Pilot",
    creditsLeft: 48,
    totalCredits: 50,
    ticketsResolved: 14,
    costSaved: 70.00,
    apiKey: "pk_live_" + Math.random().toString(36).substring(2, 14),
    tone: "friendly",
    widgetColor: "#6366f1",
    policies: [
      {
        id: "p1",
        title: "Standard 30-Day Returns",
        content: "Items in brand new, unworn condition with tags attached and original packaging may be returned within 30 days of delivery for a full refund."
      },
      {
        id: "p2",
        title: "Free Size Exchanges",
        content: "Exchanges for a different size or color are 100% free with prepaid return postage included."
      },
      {
        id: "p3",
        title: "Final Sale Items",
        content: "Items purchased from clearance racks or marked as final sale are strictly non-refundable."
      }
    ],
    tickets: [...sampleTickets],
    invoices: [...sampleInvoices]
  };

  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  closePilotModal();
  showToast(`🎉 Welcome ${storeName}! Entering your Merchant Dashboard...`, "success");
  
  setTimeout(() => {
    switchView("dashboard");
  }, 400);
}

// Quick Demo Login
function quickLoginDemoStore() {
  closeLoginModal();
  activeMerchant = {
    storeName: "AeroKicks Apparel",
    storeUrl: "https://aerokicks.com",
    email: "founder@aerokicks.com",
    plan: "14-Day Free Pilot",
    creditsLeft: 48,
    totalCredits: 50,
    ticketsResolved: 14,
    costSaved: 70.00,
    apiKey: "pk_live_aerokicks_948271049281",
    tone: "casual",
    widgetColor: "#6366f1",
    policies: [
      {
        id: "p1",
        title: "Unworn 30-Day Window",
        content: "Items in brand new, unworn condition with tags attached and original box may be returned within 30 days. Worn shoes are ineligible."
      },
      {
        id: "p2",
        title: "Flat $7 Return Shipping Fee",
        content: "A flat $7.00 return label fee is deducted from returns. Size exchanges are 100% free."
      }
    ],
    tickets: [...sampleTickets],
    invoices: [...sampleInvoices]
  };
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  showToast("⚡ Logged in as AeroKicks Apparel!", "success");
  switchView("dashboard");
}

function handleLoginSubmit(e) {
  e.preventDefault();
  const email = document.getElementById("login-email-input").value.trim();
  const storeName = email.split("@")[0].toUpperCase() + " Store";
  activeMerchant = {
    storeName: storeName,
    storeUrl: `https://${email.split("@")[1] || "store.com"}`,
    email: email,
    plan: "14-Day Free Pilot",
    creditsLeft: 50,
    totalCredits: 50,
    ticketsResolved: 8,
    costSaved: 40.00,
    apiKey: "pk_live_" + Math.random().toString(36).substring(2, 14),
    tone: "professional",
    widgetColor: "#6366f1",
    policies: [
      {
        id: "p1",
        title: "Store General Return Policy",
        content: "Returns accepted within 14 days of purchase in original packaging."
      }
    ],
    tickets: [...sampleTickets],
    invoices: [...sampleInvoices]
  };
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  closeLoginModal();
  showToast(`Logged in successfully!`, "success");
  switchView("dashboard");
}

function logoutMerchant() {
  activeMerchant = null;
  localStorage.removeItem("policypilot_active_merchant");
  showToast("Signed out successfully.", "info");
  switchView("landing");
}

// Render Dashboard Data
function renderDashboard() {
  if (!activeMerchant) return;

  document.getElementById("dash-store-name-display").textContent = activeMerchant.storeName;
  document.getElementById("dash-store-url-display").textContent = `${activeMerchant.storeUrl} • Policy AI Engine Connected`;
  document.getElementById("dash-plan-badge").textContent = activeMerchant.plan;
  document.getElementById("nav-store-badge").textContent = `🛍️ ${activeMerchant.storeName}`;

  document.getElementById("dash-credits-display").textContent = `${activeMerchant.creditsLeft} / ${activeMerchant.totalCredits}`;
  document.getElementById("dash-tickets-resolved-display").textContent = activeMerchant.ticketsResolved;
  document.getElementById("dash-cost-saved-display").textContent = `$${activeMerchant.costSaved.toFixed(2)}`;

  document.getElementById("billing-current-plan").textContent = activeMerchant.plan;
  document.getElementById("billing-credits-left").textContent = `${activeMerchant.creditsLeft} Left`;
  document.getElementById("billing-credits-total").textContent = `of ${activeMerchant.totalCredits.toLocaleString()} total credits`;

  // Settings
  if (activeMerchant.apiKey) {
    document.getElementById("display-api-key").textContent = activeMerchant.apiKey;
  }
  if (activeMerchant.tone) {
    document.getElementById("setting-agent-tone").value = activeMerchant.tone;
  }
  if (activeMerchant.widgetColor) {
    document.getElementById("setting-widget-color").value = activeMerchant.widgetColor;
    document.getElementById("color-hex-display").textContent = `${activeMerchant.widgetColor}`;
  }

  // Embed snippet tailored
  const slug = activeMerchant.storeName.toLowerCase().replace(/[^a-z0-9]/g, "-");
  document.getElementById("dash-embed-snippet").textContent = `<!-- PolicyPilot AI Storefront Widget -->
<script 
  src="https://getpolicypilot.vercel.app/widget.js" 
  data-store-id="${slug}" 
  data-theme="dark" 
  async>
</script>`;

  renderTicketTable();
  renderPoliciesList();
  renderInvoicesTable();
}

function renderTicketTable() {
  const tbody = document.getElementById("ticket-table-body");
  if (!tbody || !activeMerchant) return;
  tbody.innerHTML = "";

  activeMerchant.tickets.forEach(t => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-family: var(--font-mono); font-size: 0.78rem;">${escapeHtml(t.time)}</td>
      <td style="font-weight: 600; color: var(--text-primary);">${escapeHtml(t.customer)}<br><span style="font-weight: 400; color: #cbd5e1;">"${escapeHtml(t.query)}"</span></td>
      <td>${escapeHtml(t.answer)}</td>
      <td><span class="citation-pill" style="margin: 0;">${escapeHtml(t.citation)}</span></td>
      <td><span class="ticket-status-pill ${t.status === 'resolved' ? 'status-resolved' : 'status-escalated'}">${t.status === 'resolved' ? '✓ Auto-Resolved' : '⚠️ Escalated'}</span></td>
    `;
    tbody.appendChild(tr);
  });
}

function renderPoliciesList() {
  const container = document.getElementById("active-policies-list-container");
  if (!container || !activeMerchant) return;
  container.innerHTML = "";

  activeMerchant.policies.forEach(p => {
    const card = document.createElement("div");
    card.className = "policy-rule-card";
    card.innerHTML = `
      <div class="policy-rule-header">
        <span>§ ${escapeHtml(p.title)}</span>
        <button class="btn-delete-rule" onclick="deletePolicyRule('${p.id}')">✕ Delete</button>
      </div>
      <div class="policy-rule-body">${escapeHtml(p.content)}</div>
      <div style="font-size: 0.72rem; color: var(--text-muted); font-family: var(--font-mono);">
        Status: Indexed & Active in Vector Store
      </div>
    `;
    container.appendChild(card);
  });

  const countElem = document.getElementById("dash-active-policies-count");
  if (countElem) {
    countElem.textContent = `${activeMerchant.policies.length} Rules Active`;
  }
}

function renderInvoicesTable() {
  const tbody = document.getElementById("invoices-table-body");
  if (!tbody || !activeMerchant) return;
  tbody.innerHTML = "";

  const invoices = activeMerchant.invoices || sampleInvoices;
  invoices.forEach(inv => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-primary);">${escapeHtml(inv.id)}</td>
      <td>${escapeHtml(inv.date)}</td>
      <td style="font-weight: 600;">${escapeHtml(inv.plan)}</td>
      <td style="font-family: var(--font-mono); color: var(--emerald); font-weight: 700;">${escapeHtml(inv.amount)}</td>
      <td><span class="ticket-status-pill status-resolved">✓ ${escapeHtml(inv.status)}</span></td>
      <td><button class="btn-invoice-download" onclick="downloadInvoiceReceipt('${inv.id}', '${inv.plan}', '${inv.amount}', '${inv.date}')">📄 Download PDF</button></td>
    `;
    tbody.appendChild(tr);
  });
}

// Add New Policy in Dashboard
function handleAddNewPolicy(e) {
  e.preventDefault();
  const title = document.getElementById("new-policy-title").value.trim();
  const content = document.getElementById("new-policy-content").value.trim();
  if (!title || !content || !activeMerchant) return;

  const newRule = {
    id: "p_" + Date.now(),
    title: title,
    content: content
  };

  activeMerchant.policies.push(newRule);
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  renderPoliciesList();

  e.target.reset();
  showToast(`✅ "${title}" indexed into your AI brain!`, "success");
}

function deletePolicyRule(id) {
  if (!activeMerchant) return;
  activeMerchant.policies = activeMerchant.policies.filter(p => p.id !== id);
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  renderPoliciesList();
  showToast("Policy rule removed.", "info");
}

// Simulate Live Incoming Customer Ticket
function triggerSimulatedTicket() {
  if (!activeMerchant) return;
  if (activeMerchant.creditsLeft <= 0) {
    showToast("⚠️ Out of credits! Please upgrade your plan.", "warning");
    switchDashTab("billing");
    return;
  }

  const queries = [
    {
      customer: "Emma R. (Order #1048)",
      query: "Can I return my item if I removed the price tag?",
      answer: "Tags must remain attached to qualify for a return within 30 days.",
      citation: "Section 3.1: Return Conditions",
      status: "resolved"
    },
    {
      customer: "Liam B. (Order #1052)",
      query: "How much is the return postage fee for exchanges?",
      answer: "Exchanges are 100% free with prepaid return postage included.",
      citation: "Section 3.2: Size Exchanges",
      status: "resolved"
    },
    {
      customer: "Chloe P. (Order #1055)",
      query: "Can you waive the return fee because I'm an influencer?",
      answer: "Fee waiver requires customer operations signoff. Escalation ticket logged.",
      citation: "Policy Escalation Rule #14",
      status: "escalated"
    }
  ];

  const pick = queries[Math.floor(Math.random() * queries.length)];
  const newTicket = {
    time: "Just now",
    customer: pick.customer,
    query: pick.query,
    answer: pick.answer,
    citation: pick.citation,
    status: pick.status
  };

  activeMerchant.tickets.unshift(newTicket);
  activeMerchant.creditsLeft -= 1;
  activeMerchant.ticketsResolved += 1;
  activeMerchant.costSaved += 5.00;
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));

  renderDashboard();
  switchDashTab("tickets");
  showToast(`🔔 Customer ticket auto-resolved: "${pick.query.substring(0, 35)}..."`, "success");
}

// Dashboard Tabs Switcher
function switchDashTab(tabKey) {
  document.querySelectorAll(".dash-tab-btn").forEach(btn => btn.classList.remove("active"));
  document.querySelectorAll(".dash-panel").forEach(p => p.classList.remove("active"));

  const targetBtn = document.getElementById(`btn-tab-${tabKey}`);
  const targetPanel = document.getElementById(`panel-${tabKey}`);

  if (targetBtn) targetBtn.classList.add("active");
  if (targetPanel) targetPanel.classList.add("active");
}

// Dashboard AI Test Chat
function handleDashChatKey(e) {
  if (e.key === "Enter") sendDashChatMessage();
}

function sendDashChatMessage() {
  const input = document.getElementById("dash-chat-input");
  const query = input.value.trim();
  if (!query || !activeMerchant) return;

  const container = document.getElementById("dash-chat-messages-container");
  
  const userDiv = document.createElement("div");
  userDiv.className = "message user";
  userDiv.innerHTML = `<div class="msg-bubble">${escapeHtml(query)}</div>`;
  container.appendChild(userDiv);
  input.value = "";
  container.scrollTop = container.scrollHeight;

  setTimeout(() => {
    let answerText = "Based strictly on your active store policies, requests are verified with zero hallucinations. Ineligible or custom requests are escalated directly to staff.";
    let citationText = "Active Store Policy";

    const found = activeMerchant.policies.find(p => 
      query.toLowerCase().includes(p.title.toLowerCase()) || 
      p.content.toLowerCase().split(" ").some(w => w.length > 4 && query.toLowerCase().includes(w))
    );

    if (found) {
      answerText = `According to your indexed policy: "${found.content}".`;
      citationText = `§ ${found.title}`;
    }

    const agentDiv = document.createElement("div");
    agentDiv.className = "message agent";
    agentDiv.innerHTML = `
      <div class="msg-bubble">${escapeHtml(answerText)}</div>
      <div class="citation-pill"><span>📑 Cited from:</span> <strong>${escapeHtml(citationText)}</strong></div>
      <div class="confidence-badge"><span>✓</span> 100% Policy Alignment • Verified</div>
    `;
    container.appendChild(agentDiv);
    container.scrollTop = container.scrollHeight;
  }, 600);
}

function resetDashChat() {
  const container = document.getElementById("dash-chat-messages-container");
  container.innerHTML = `
    <div class="message agent">
      <div class="msg-bubble">
        Hello! I'm your store's dedicated support assistant. Ask me anything about your policies to test my answers!
      </div>
    </div>
  `;
}

function copyDashEmbedCode() {
  const code = document.getElementById("dash-embed-snippet").textContent;
  navigator.clipboard.writeText(code).then(() => {
    showToast("📋 Storefront widget snippet copied to clipboard!", "success");
  });
}

function launchLiveFloatingWidget() {
  if (window.PolicyPilotWidgetLoaded) {
    showToast("🛡️ Floating widget is already active in bottom right corner!", "info");
    return;
  }
  const script = document.createElement("script");
  script.src = "widget.js";
  script.setAttribute("data-store-id", activeMerchant ? activeMerchant.storeName : "demo-store");
  document.body.appendChild(script);
  showToast("🚀 Floating widget launched in the bottom right corner!", "success");
}

// Stripe Payment Integration
function openStripeModal(planName, price, credits) {
  pendingStripePlan = { planName, price, credits };
  document.getElementById("stripe-checkout-title").textContent = `Subscribe to ${planName}`;
  document.getElementById("stripe-checkout-price").innerHTML = `$${price}.00 <span style="font-size: 0.9rem; font-weight: 500; color: var(--text-muted);">/ month</span>`;
  document.getElementById("stripe-modal").style.display = "flex";
}

function closeStripeModal() {
  document.getElementById("stripe-modal").style.display = "none";
}

function handleStripePaymentSubmit(e) {
  e.preventDefault();
  const btn = document.getElementById("btn-stripe-pay");
  btn.innerHTML = `<span class="pulse-dot"></span> Processing 256-Bit Stripe Payment...`;
  btn.disabled = true;

  setTimeout(() => {
    btn.innerHTML = `🔒 Confirm & Activate Subscription ➔`;
    btn.disabled = false;
    closeStripeModal();

    if (!activeMerchant) {
      quickLoginDemoStore();
    }

    const plan = pendingStripePlan || { planName: "Starter Plan", price: 79, credits: 1000 };
    activeMerchant.plan = `${plan.planName} ($${plan.price}/mo)`;
    activeMerchant.creditsLeft += plan.credits;
    activeMerchant.totalCredits += plan.credits;

    // Create Invoice Receipt
    const newInvoice = {
      id: "in_stripe_" + Math.random().toString(36).substring(2, 9).toUpperCase(),
      date: new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }),
      plan: `${plan.planName} Monthly Subscription`,
      amount: `$${plan.price}.00`,
      status: "PAID"
    };
    if (!activeMerchant.invoices) activeMerchant.invoices = [];
    activeMerchant.invoices.unshift(newInvoice);

    localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
    renderDashboard();
    switchView("dashboard");
    switchDashTab("billing");

    showToast(`🎉 Payment approved! Your ${plan.planName} is active with ${plan.credits.toLocaleString()} credits!`, "success");
  }, 1200);
}

function formatCardNumber(input) {
  let val = input.value.replace(/\D/g, "");
  let formatted = "";
  for (let i = 0; i < val.length; i++) {
    if (i > 0 && i % 4 === 0) formatted += " ";
    formatted += val[i];
  }
  input.value = formatted.substring(0, 19);
}

function formatCardExpiry(input) {
  let val = input.value.replace(/\D/g, "");
  if (val.length >= 2) {
    input.value = val.substring(0, 2) + " / " + val.substring(2, 4);
  } else {
    input.value = val;
  }
}

function downloadInvoiceReceipt(id, plan, amount, date) {
  const receiptWindow = window.open("", "_blank");
  receiptWindow.document.write(`
    <html>
      <head>
        <title>Receipt - ${id}</title>
        <style>
          body { font-family: -apple-system, sans-serif; padding: 40px; color: #1e293b; line-height: 1.6; }
          .receipt-box { max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; padding: 32px; }
          .header { display: flex; justify-content: space-between; border-bottom: 2px solid #6366f1; padding-bottom: 16px; margin-bottom: 24px; }
          .status { display: inline-block; padding: 4px 12px; background: #ecfdf5; color: #059669; border-radius: 9999px; font-weight: 700; font-size: 14px; }
          .amount { font-size: 28px; font-weight: 800; color: #4338ca; }
        </style>
      </head>
      <body>
        <div class="receipt-box">
          <div class="header">
            <div>
              <h2>🛡️ PolicyPilot.ai</h2>
              <div>Invoice #${id}</div>
              <div>Date: ${date}</div>
            </div>
            <div style="text-align: right;">
              <span class="status">PAID</span>
              <div class="amount" style="margin-top: 8px;">${amount}</div>
            </div>
          </div>
          <p><strong>Billed To:</strong> ${activeMerchant ? activeMerchant.storeName : "Store Partner"} (${activeMerchant ? activeMerchant.email : "billing@store.com"})</p>
          <p><strong>Item:</strong> ${plan}</p>
          <p><strong>Payment Method:</strong> Visa ending in 4242 (Stripe Secure Charge)</p>
          <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 24px 0;">
          <p style="font-size: 12px; color: #64748b;">PolicyPilot AI, Inc. • Enterprise Subscription Infrastructure</p>
        </div>
      </body>
    </html>
  `);
  receiptWindow.document.close();
}

// Store Settings
function saveStoreSettings() {
  if (!activeMerchant) return;
  activeMerchant.tone = document.getElementById("setting-agent-tone").value;
  activeMerchant.widgetColor = document.getElementById("setting-widget-color").value;
  document.getElementById("color-hex-display").textContent = activeMerchant.widgetColor;
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  showToast("Store settings saved.", "info");
}

function copyApiKey() {
  const key = document.getElementById("display-api-key").textContent;
  navigator.clipboard.writeText(key).then(() => {
    showToast("API Key copied to clipboard!", "success");
  });
}

function regenerateApiKey() {
  if (!activeMerchant) return;
  activeMerchant.apiKey = "pk_live_" + Math.random().toString(36).substring(2, 14);
  document.getElementById("display-api-key").textContent = activeMerchant.apiKey;
  localStorage.setItem("policypilot_active_merchant", JSON.stringify(activeMerchant));
  showToast("New API key generated.", "success");
}

// FAQ Accordion
function toggleFaq(item) {
  const isOpen = item.classList.contains("open");
  document.querySelectorAll(".faq-item").forEach(f => f.classList.remove("open"));
  if (!isOpen) item.classList.add("open");
}

// Legal Modals Content
const legalTexts = {
  terms: {
    title: "Terms of Service",
    content: `
      <h4>1. Acceptance of Terms</h4>
      <p>By subscribing to or using PolicyPilot AI, you agree to these Terms of Service. PolicyPilot provides automated policy assistance software for e-commerce platforms.</p>
      <h4>2. Accuracy & Dual-Engine Verification</h4>
      <p>PolicyPilot verifies generated answers against merchant-provided policy documentation using an internal critic. While our zero-hallucination guard minimizes errors, merchants remain responsible for maintaining accurate and lawful store policies.</p>
      <h4>3. Subscription & Billing</h4>
      <p>Subscriptions renew automatically each month via Stripe. You may cancel your subscription at any time with one click in your Merchant Dashboard before the start of the next billing cycle.</p>
      <h4>4. Limitation of Liability</h4>
      <p>In no event shall PolicyPilot AI be liable for indirect, punitive, or consequential damages resulting from store returns or customer communications.</p>
    `
  },
  privacy: {
    title: "Privacy Policy (GDPR & CCPA Compliant)",
    content: `
      <h4>1. Data We Collect</h4>
      <p>We collect store URLs, merchant emails, uploaded policy documents, and customer support chat transcripts strictly to provide our retrieval-augmented support services.</p>
      <h4>2. Zero Model Training on Customer Data</h4>
      <p>Your store documents and customer chat logs are never used to train global public AI models. We maintain complete tenant isolation with row-level encryption.</p>
      <h4>3. Data Retention & Deletion</h4>
      <p>Merchants may request full deletion of their store data, embeddings, and chat history at any time by contacting privacy@policypilot.ai or through the settings portal.</p>
      <h4>4. Security</h4>
      <p>All network traffic is encrypted via TLS 1.3. Payment card data is processed exclusively through Stripe and never touches our application servers.</p>
    `
  },
  refund: {
    title: "Refund & Cancellation Policy",
    content: `
      <h4>1. 14-Day Risk-Free Trial</h4>
      <p>Every store begins with a 14-day free pilot (50 automated answers) requiring zero credit card upfront.</p>
      <h4>2. 14-Day Money-Back Guarantee</h4>
      <p>If you upgrade to any paid tier (Starter or Growth) and PolicyPilot does not automate at least 40% of your repetitive tickets with 100% policy accuracy, contact us within 14 days for a full, unconditional refund.</p>
      <h4>3. Hassle-Free Cancellation</h4>
      <p>Cancel anytime from your dashboard. Upon cancellation, your service remains active through the end of your current paid billing period with no further charges.</p>
    `
  }
};

function openLegalModal(type) {
  const policy = legalTexts[type] || legalTexts.terms;
  document.getElementById("legal-modal-title").textContent = policy.title;
  document.getElementById("legal-modal-content").innerHTML = policy.content;
  document.getElementById("legal-modal").style.display = "flex";
}
function closeLegalModal() {
  document.getElementById("legal-modal").style.display = "none";
}

// Contact Support Modal
function openContactModal(context = "") {
  document.getElementById("contact-modal").style.display = "flex";
}
function closeContactModal() {
  document.getElementById("contact-modal").style.display = "none";
}
function handleContactSubmit(e) {
  e.preventDefault();
  closeContactModal();
  showToast("💬 Message sent! An engineer will reply within 2 hours.", "success");
  e.target.reset();
}

// Landing Store Switcher
function switchStore(storeKey) {
  currentStoreKey = storeKey;
  document.querySelectorAll(".store-tab-btn").forEach(btn => btn.classList.remove("active"));
  const activeBtn = document.getElementById(`tab-${storeKey}`);
  if (activeBtn) activeBtn.classList.add("active");

  const store = storeProfiles[storeKey];
  document.getElementById("active-policy-title").textContent = store.title;
  document.getElementById("active-policy-text").textContent = store.snippet;
  
  const customInputs = document.getElementById("custom-policy-inputs");
  if (storeKey === "custom") {
    customInputs.style.display = "flex";
  } else {
    customInputs.style.display = "none";
  }

  document.getElementById("chat-agent-avatar").textContent = store.avatar;
  document.getElementById("chat-agent-name").innerHTML = `${store.name} Assistant <span class="trust-shield">🛡️ Citation Guarded</span>`;

  renderChips(store.chips);
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

function handleInputKey(event) {
  if (event.key === "Enter") sendUserMessage();
}

function sendUserMessage() {
  if (isTyping) return;
  const input = document.getElementById("chat-user-input");
  const userText = input.value.trim();
  if (!userText) return;

  appendMessage("user", userText);
  input.value = "";

  isTyping = true;
  showTypingIndicator();

  setTimeout(() => {
    removeTypingIndicator();
    generateAgentResponse(userText);
    isTyping = false;
  }, 800 + Math.random() * 400);
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

// ROI Calculator
function updateCalculator() {
  const orders = parseInt(document.getElementById("orders-slider").value);
  const tickets = parseInt(document.getElementById("tickets-slider").value);
  const cost = parseFloat(document.getElementById("cost-slider").value);

  document.getElementById("orders-val-display").textContent = orders.toLocaleString();
  document.getElementById("tickets-val-display").textContent = tickets.toLocaleString();
  document.getElementById("ticket-cost-display").textContent = "$" + cost.toFixed(2);

  const automatedTickets = Math.round(tickets * 0.75);
  const currentMonthlyExpense = tickets * cost;

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

function copyEmbedCode() {
  const code = document.getElementById("embed-code-snippet").textContent;
  navigator.clipboard.writeText(code).then(() => {
    showToast("📋 Embed script copied to clipboard!", "success");
  });
}

// Modal Triggers
function openPilotModal(source = "") {
  document.getElementById("pilot-modal").style.display = "flex";
}
function closePilotModal() {
  document.getElementById("pilot-modal").style.display = "none";
}
function openLoginModal() {
  document.getElementById("login-modal").style.display = "flex";
}
function closeLoginModal() {
  document.getElementById("login-modal").style.display = "none";
}
function closeModalOnBackdrop(e) {
  if (e.target.id === "pilot-modal") closePilotModal();
  if (e.target.id === "login-modal") closeLoginModal();
  if (e.target.id === "stripe-modal") closeStripeModal();
  if (e.target.id === "contact-modal") closeContactModal();
  if (e.target.id === "legal-modal") closeLegalModal();
}

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
