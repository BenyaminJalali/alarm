(function () {
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("userInput");
  const sendBtn = document.getElementById("sendBtn");
  const newChatBtn = document.getElementById("newChatBtn");
  const kbStatus = document.getElementById("kbStatus");

  let history = [];
  let currentRole = "installer";
  let isStreaming = false;

  // ── KB status ──────────────────────────────────────────────────────────────
  fetch("/api/health")
    .then((r) => r.json())
    .then((d) => {
      kbStatus.textContent = `KB: ${d.kb_entries} alarms loaded`;
    })
    .catch(() => {
      kbStatus.textContent = "KB: offline";
    });

  // ── Role selection ─────────────────────────────────────────────────────────
  document.querySelectorAll(".role-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".role-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentRole = btn.dataset.role;
    });
  });

  // ── Example buttons ────────────────────────────────────────────────────────
  document.querySelectorAll(".example-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      inputEl.value = btn.dataset.msg;
      inputEl.dispatchEvent(new Event("input"));
      inputEl.focus();
    });
  });

  // ── New chat ───────────────────────────────────────────────────────────────
  newChatBtn.addEventListener("click", () => {
    history = [];
    messagesEl.innerHTML = "";
    appendMessage(
      "assistant",
      "New conversation started. Describe a fault, alarm code, or symptom to begin."
    );
  });

  // ── Input handling ─────────────────────────────────────────────────────────
  inputEl.addEventListener("input", () => {
    sendBtn.disabled = !inputEl.value.trim() || isStreaming;
    autoResize();
  });

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) send();
    }
  });

  sendBtn.addEventListener("click", send);

  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
  }

  // ── Send ───────────────────────────────────────────────────────────────────
  async function send() {
    const text = inputEl.value.trim();
    if (!text || isStreaming) return;

    inputEl.value = "";
    inputEl.style.height = "auto";
    sendBtn.disabled = true;
    isStreaming = true;

    appendMessage("user", text);
    history.push({ role: "user", content: text });

    const typing = appendTyping();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, audience: currentRole }),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      typing.remove();
      const assistantEl = appendMessage("assistant", "");
      const contentEl = assistantEl.querySelector(".message-content");

      let fullText = "";
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const payload = line.slice(6);
            if (payload === "[DONE]") break;
            try {
              const { text: chunk } = JSON.parse(payload);
              fullText += chunk;
              contentEl.innerHTML = renderMarkdown(fullText);
              scrollToBottom();
            } catch (_) {}
          }
        }
      }

      history.push({ role: "assistant", content: fullText });
    } catch (err) {
      typing.remove();
      appendMessage("assistant", `Error: ${err.message}. Please try again.`);
    } finally {
      isStreaming = false;
      sendBtn.disabled = !inputEl.value.trim();
    }
  }

  // ── DOM helpers ────────────────────────────────────────────────────────────
  function appendMessage(role, text) {
    const div = document.createElement("div");
    div.className = `message ${role}`;
    div.innerHTML = `
      <div class="message-avatar">${role === "assistant" ? "G" : "U"}</div>
      <div class="message-content">${renderMarkdown(text)}</div>
    `;
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function appendTyping() {
    const div = document.createElement("div");
    div.className = "message assistant typing";
    div.innerHTML = `
      <div class="message-avatar">G</div>
      <div class="message-content">
        <div class="dot"></div><div class="dot"></div><div class="dot"></div>
      </div>
    `;
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ── Markdown renderer (minimal, no deps) ───────────────────────────────────
  function renderMarkdown(text) {
    if (!text) return "";

    // Escape HTML first
    let html = text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    // Code blocks
    html = html.replace(/```[\w]*\n?([\s\S]*?)```/g, "<pre><code>$1</code></pre>");

    // Inline code
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

    // Headers
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");

    // Bold and italic
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");

    // Severity colorization
    html = html.replace(/\bCritical\b/g, '<span class="severity-critical">Critical</span>');
    html = html.replace(/\bSeverity 1\b/g, '<span class="severity-critical">Severity 1</span>');
    html = html.replace(/\bSeverity 2\b/g, '<span class="severity-high">Severity 2</span>');
    html = html.replace(/\bSeverity 3\b/g, '<span class="severity-medium">Severity 3</span>');
    html = html.replace(/\bSeverity 4\b/g, '<span class="severity-low">Severity 4</span>');

    // Ordered lists
    html = html.replace(/^(\d+)\.\s(.+)$/gm, "<li>$2</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ol>${m}</ol>`);

    // Unordered lists
    html = html.replace(/^[-*]\s(.+)$/gm, "<li>$1</li>");
    // Wrap orphan li blocks not already in ol
    html = html.replace(/(?<!<\/ol>)(<li>(?:(?!<ol>|<\/ol>)[\s\S])*?<\/li>\n?)+/g, (m) => {
      if (m.includes("<ol>")) return m;
      return `<ul>${m}</ul>`;
    });

    // Horizontal rules
    html = html.replace(/^---$/gm, "<hr/>");

    // Paragraphs — split on double newlines
    const blocks = html.split(/\n{2,}/);
    html = blocks
      .map((block) => {
        block = block.trim();
        if (!block) return "";
        if (/^<(h[1-6]|ul|ol|pre|hr)/.test(block)) return block;
        return `<p>${block.replace(/\n/g, "<br/>")}</p>`;
      })
      .join("\n");

    return html;
  }
})();
