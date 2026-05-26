(function () {
  // Generate a stable session ID for this browser session
  const SESSION_ID = sessionStorage.getItem('alarm_session_id') || (() => {
    const id = 'sess_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
    sessionStorage.setItem('alarm_session_id', id);
    return id;
  })();

  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("userInput");
  const sendBtn = document.getElementById("sendBtn");
  const newChatBtn = document.getElementById("newChatBtn");
  const kbStatus = document.getElementById("kbStatus");
  const imageBtn = document.getElementById("imageBtn");
  const imageInput = document.getElementById("imageInput");
  const micBtn = document.getElementById("micBtn");
  const imagePreviewBar = document.getElementById("imagePreviewBar");

  let history = [];
  let currentRole = "installer";
  let isStreaming = false;
  let pendingImages = []; // { dataUrl, mimeType, filename }
  let recognition = null;
  let isRecording = false;

  // ── KB status ──────────────────────────────────────────────────────────────
  fetch("/api/health")
    .then((r) => r.json())
    .then((d) => { kbStatus.textContent = `KB: ${d.kb_entries} alarms loaded`; })
    .catch(() => { kbStatus.textContent = "KB: offline"; });

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
    pendingImages = [];
    messagesEl.innerHTML = "";
    imagePreviewBar.innerHTML = "";
    imagePreviewBar.style.display = "none";
    appendMessage("assistant", "New conversation started. Describe a fault, alarm code, or symptom to begin.");
  });

  // ── Input handling ─────────────────────────────────────────────────────────
  inputEl.addEventListener("input", () => {
    sendBtn.disabled = (!inputEl.value.trim() && pendingImages.length === 0) || isStreaming;
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

  // ── Image handling ─────────────────────────────────────────────────────────
  imageInput.addEventListener("change", async () => {
    const files = Array.from(imageInput.files);
    for (const file of files) {
      console.log("[image] selected:", file.name, "type:", file.type, "size:", file.size);
      try {
        const result = await loadImageFile(file);
        console.log("[image] processed OK:", file.name, "->", result.mimeType, "dataUrl length:", result.dataUrl.length);
        pendingImages.push(result);
      } catch (e) {
        console.error("[image] failed:", file.name, e);
        appendMessage("assistant", `⚠️ Could not attach **${file.name}**: ${e.message}. Try a screenshot (PNG) instead.`);
      }
    }
    imageInput.value = "";
    renderImagePreviews();
    sendBtn.disabled = pendingImages.length === 0 && !inputEl.value.trim() || isStreaming;
  });

  async function loadImageFile(file) {
    const isHeic = file.type === "image/heic" || file.type === "image/heif" ||
                   file.name.toLowerCase().endsWith(".heic") || file.name.toLowerCase().endsWith(".heif");

    if (isHeic) {
      // Send to server for conversion
      const raw = await fileToBase64(file);
      const res = await fetch("/api/convert-image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: raw }),
      });
      if (!res.ok) throw new Error("HEIC conversion failed");
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      return {
        dataUrl: "data:image/jpeg;base64," + json.data,
        mimeType: "image/jpeg",
        filename: file.name.replace(/\.heic$/i, ".jpg"),
      };
    }

    return compressImage(file, 3.5 * 1024 * 1024);
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => resolve(e.target.result.split(",")[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  function renderImagePreviews() {
    imagePreviewBar.innerHTML = "";
    if (pendingImages.length === 0) {
      imagePreviewBar.style.display = "none";
      return;
    }
    imagePreviewBar.style.display = "flex";
    pendingImages.forEach((img, i) => {
      const wrap = document.createElement("div");
      wrap.className = "img-preview-wrap";
      wrap.innerHTML = `
        <img src="${img.dataUrl}" class="img-preview-thumb" />
        <button class="img-preview-remove" data-i="${i}">×</button>
      `;
      imagePreviewBar.appendChild(wrap);
    });
    imagePreviewBar.querySelectorAll(".img-preview-remove").forEach((btn) => {
      btn.addEventListener("click", () => {
        pendingImages.splice(parseInt(btn.dataset.i), 1);
        renderImagePreviews();
        if (pendingImages.length === 0 && !inputEl.value.trim()) {
          sendBtn.disabled = true;
        }
      });
    });
  }

  async function compressImage(file, maxBytes) {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = (e) => {
        const img = new Image();
        img.onload = () => {
          let w = img.width, h = img.height;
          // Scale down if needed
          const maxDim = 1920;
          if (w > maxDim || h > maxDim) {
            if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
            else { w = Math.round(w * maxDim / h); h = maxDim; }
          }
          const canvas = document.createElement("canvas");
          canvas.width = w; canvas.height = h;
          canvas.getContext("2d").drawImage(img, 0, 0, w, h);

          // Try quality 0.85 first, reduce if still too big
          let quality = 0.85;
          let dataUrl = canvas.toDataURL("image/jpeg", quality);
          while (dataUrl.length * 0.75 > maxBytes && quality > 0.3) {
            quality -= 0.1;
            dataUrl = canvas.toDataURL("image/jpeg", quality);
          }
          resolve({ dataUrl, mimeType: "image/jpeg", filename: file.name });
        };
        img.src = e.target.result;
      };
      reader.readAsDataURL(file);
    });
  }

  // ── Voice handling ─────────────────────────────────────────────────────────
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  if (!SpeechRecognition) {
    micBtn.style.display = "none";
  } else {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    let finalTranscript = "";

    recognition.onstart = () => {
      isRecording = true;
      micBtn.classList.add("recording");
      micBtn.title = "Tap to stop";
    };

    recognition.onresult = (e) => {
      let interim = "";
      finalTranscript = "";
      for (let i = 0; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalTranscript += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      inputEl.value = finalTranscript || interim;
      inputEl.dispatchEvent(new Event("input"));
    };

    recognition.onend = () => {
      isRecording = false;
      micBtn.classList.remove("recording");
      micBtn.title = "Tap to speak";
      if (finalTranscript.trim()) {
        inputEl.value = finalTranscript.trim();
        inputEl.dispatchEvent(new Event("input"));
      }
    };

    recognition.onerror = () => {
      isRecording = false;
      micBtn.classList.remove("recording");
    };

    micBtn.addEventListener("click", () => {
      if (isRecording) {
        recognition.stop();
      } else {
        finalTranscript = "";
        recognition.start();
      }
    });
  }

  // ── Send ───────────────────────────────────────────────────────────────────
  async function send() {
    const text = inputEl.value.trim();
    if ((!text && pendingImages.length === 0) || isStreaming) return;

    const imagesToSend = [...pendingImages];
    pendingImages = [];
    renderImagePreviews();

    inputEl.value = "";
    inputEl.style.height = "auto";
    sendBtn.disabled = true;
    isStreaming = true;

    // Build user message content for history
    let userContent;
    if (imagesToSend.length > 0) {
      userContent = [];
      imagesToSend.forEach((img) => {
        userContent.push({
          type: "image",
          source: {
            type: "base64",
            media_type: img.mimeType,
            data: img.dataUrl.split(",")[1],
          },
        });
      });
      if (text) userContent.push({ type: "text", text });
      else userContent.push({ type: "text", text: "Please analyze this image and help me troubleshoot." });
    } else {
      userContent = text;
    }

    // Show user message in UI
    appendUserMessage(text, imagesToSend);
    history.push({ role: "user", content: userContent });

    const typing = appendTyping();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, audience: currentRole, session_id: SESSION_ID }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

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
      addFeedback(assistantEl, text, fullText);
    } catch (err) {
      typing.remove();
      appendMessage("assistant", "Sorry, something went wrong on my end. Please try again in a moment — if it keeps happening, try refreshing the page.");
    } finally {
      isStreaming = false;
      sendBtn.disabled = !inputEl.value.trim() && pendingImages.length === 0;
    }
  }

  // ── DOM helpers ────────────────────────────────────────────────────────────
  function appendUserMessage(text, images) {
    const div = document.createElement("div");
    div.className = "message user";
    let imagesHtml = "";
    if (images.length > 0) {
      imagesHtml = `<div class="message-images">${images.map(img =>
        `<img src="${img.dataUrl}" class="message-image" />`).join("")}</div>`;
    }
    div.innerHTML = `
      <div class="message-avatar">U</div>
      <div class="message-content">${imagesHtml}${text ? renderMarkdown(text) : ""}</div>
    `;
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

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

  // ── Feedback ───────────────────────────────────────────────────────────────
  function addFeedback(msgEl, question, answer) {
    const bar = document.createElement("div");
    bar.className = "feedback-bar";
    bar.innerHTML = `
      <span class="feedback-label">Was this helpful?</span>
      <button class="feedback-btn up" title="Yes, helpful">👍</button>
      <button class="feedback-btn down" title="Not helpful">👎</button>
      <span class="feedback-thanks" style="display:none">Thanks! This helps improve future answers.</span>
    `;
    msgEl.appendChild(bar);
    bar.querySelector(".up").addEventListener("click", () => sendFeedback("up", question, answer, bar));
    bar.querySelector(".down").addEventListener("click", () => sendFeedback("down", question, answer, bar));
  }

  function sendFeedback(rating, question, answer, bar) {
    fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, audience: currentRole, question, answer }),
    });
    bar.querySelectorAll(".feedback-btn").forEach(b => b.style.display = "none");
    bar.querySelector(".feedback-label").style.display = "none";
    bar.querySelector(".feedback-thanks").style.display = "inline";
  }

  // ── Markdown renderer ──────────────────────────────────────────────────────
  function renderMarkdown(text) {
    if (!text) return "";
    let html = text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    html = html.replace(/```[\w]*\n?([\s\S]*?)```/g, "<pre><code>$1</code></pre>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    html = html.replace(/\bCritical\b/g, '<span class="severity-critical">Critical</span>');
    html = html.replace(/\bSeverity 1\b/g, '<span class="severity-critical">Severity 1</span>');
    html = html.replace(/\bSeverity 2\b/g, '<span class="severity-high">Severity 2</span>');
    html = html.replace(/\bSeverity 3\b/g, '<span class="severity-medium">Severity 3</span>');
    html = html.replace(/\bSeverity 4\b/g, '<span class="severity-low">Severity 4</span>');
    html = html.replace(/^(\d+)\.\s(.+)$/gm, "<li>$2</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ol>${m}</ol>`);
    html = html.replace(/^[-*]\s(.+)$/gm, "<li>$1</li>");
    html = html.replace(/(?<!<\/ol>)(<li>(?:(?!<ol>|<\/ol>)[\s\S])*?<\/li>\n?)+/g, (m) => {
      if (m.includes("<ol>")) return m;
      return `<ul>${m}</ul>`;
    });
    html = html.replace(/^---$/gm, "<hr/>");
    const blocks = html.split(/\n{2,}/);
    html = blocks.map((block) => {
      block = block.trim();
      if (!block) return "";
      if (/^<(h[1-6]|ul|ol|pre|hr)/.test(block)) return block;
      return `<p>${block.replace(/\n/g, "<br/>")}</p>`;
    }).join("\n");
    return html;
  }
})();
