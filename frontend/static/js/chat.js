(function () {
  // Stable session ID for this browser session
  const SESSION_ID = sessionStorage.getItem('alarm_session_id') || (() => {
    const id = 'sess_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
    sessionStorage.setItem('alarm_session_id', id);
    return id;
  })();

  const inputEl    = document.getElementById("userInput");
  const sendBtn    = document.getElementById("sendBtn");
  const newChatBtn = document.getElementById("newChatBtn");
  const kbStatus   = document.getElementById("kbStatus");
  const imageInput = document.getElementById("imageInput");
  const micBtn     = document.getElementById("micBtn");
  const imagePreviewBar = document.getElementById("imagePreviewBar");

  const AUDIENCES = ["homeowner", "installer", "support"];

  // Per-audience conversation history
  const histories = { homeowner: [], installer: [], support: [] };

  let isStreaming   = false;
  let pendingImages = [];
  let recognition   = null;
  let isRecording   = false;

  // ── KB status ──────────────────────────────────────────────────────────────
  fetch("/api/health")
    .then(r => r.json())
    .then(d => { kbStatus.textContent = `KB: ${d.kb_entries} alarms loaded`; })
    .catch(() => { kbStatus.textContent = "KB: offline"; });

  // ── Example buttons ────────────────────────────────────────────────────────
  document.querySelectorAll(".example-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      inputEl.value = btn.dataset.msg;
      inputEl.dispatchEvent(new Event("input"));
      inputEl.focus();
    });
  });

  // ── New chat ───────────────────────────────────────────────────────────────
  newChatBtn.addEventListener("click", () => {
    AUDIENCES.forEach(aud => {
      histories[aud] = [];
      const msgsEl = document.getElementById(`msgs-${aud}`);
      msgsEl.innerHTML = '<div class="col-empty">New conversation started. Ask a question below.</div>';
    });
    pendingImages = [];
    renderImagePreviews();
    inputEl.value = "";
    inputEl.style.height = "auto";
    sendBtn.disabled = true;
  });

  // ── Input handling ─────────────────────────────────────────────────────────
  inputEl.addEventListener("input", () => {
    sendBtn.disabled = (!inputEl.value.trim() && pendingImages.length === 0) || isStreaming;
    autoResize();
  });

  inputEl.addEventListener("keydown", e => {
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
        appendColError(`Could not attach **${file.name}**: ${e.message}`);
      }
    }
    imageInput.value = "";
    renderImagePreviews();
    sendBtn.disabled = (pendingImages.length === 0 && !inputEl.value.trim()) || isStreaming;
  });

  async function loadImageFile(file) {
    const isHeic = file.type === "image/heic" || file.type === "image/heif" ||
                   file.name.toLowerCase().endsWith(".heic") || file.name.toLowerCase().endsWith(".heif");
    if (isHeic) {
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
      reader.onload = e => resolve(e.target.result.split(",")[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  function renderImagePreviews() {
    imagePreviewBar.innerHTML = "";
    if (!pendingImages.length) { imagePreviewBar.style.display = "none"; return; }
    imagePreviewBar.style.display = "flex";
    pendingImages.forEach((img, i) => {
      const wrap = document.createElement("div");
      wrap.className = "img-preview-wrap";
      wrap.innerHTML = `<img src="${img.dataUrl}" class="img-preview-thumb" /><button class="img-preview-remove" data-i="${i}">×</button>`;
      imagePreviewBar.appendChild(wrap);
    });
    imagePreviewBar.querySelectorAll(".img-preview-remove").forEach(btn => {
      btn.addEventListener("click", () => {
        pendingImages.splice(parseInt(btn.dataset.i), 1);
        renderImagePreviews();
        if (!pendingImages.length && !inputEl.value.trim()) sendBtn.disabled = true;
      });
    });
  }

  async function compressImage(file, maxBytes) {
    return new Promise(resolve => {
      const reader = new FileReader();
      reader.onload = e => {
        const img = new Image();
        img.onload = () => {
          let w = img.width, h = img.height;
          const maxDim = 1920;
          if (w > maxDim || h > maxDim) {
            if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
            else { w = Math.round(w * maxDim / h); h = maxDim; }
          }
          const canvas = document.createElement("canvas");
          canvas.width = w; canvas.height = h;
          canvas.getContext("2d").drawImage(img, 0, 0, w, h);
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

  // ── Voice ──────────────────────────────────────────────────────────────────
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    micBtn.style.display = "none";
  } else {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    let finalTranscript = "";

    recognition.onstart = () => { isRecording = true; micBtn.classList.add("recording"); micBtn.title = "Tap to stop"; };
    recognition.onresult = e => {
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
      if (finalTranscript.trim()) { inputEl.value = finalTranscript.trim(); inputEl.dispatchEvent(new Event("input")); }
    };
    recognition.onerror = () => { isRecording = false; micBtn.classList.remove("recording"); };
    micBtn.addEventListener("click", () => {
      if (isRecording) recognition.stop();
      else { finalTranscript = ""; recognition.start(); }
    });
  }

  // ── Send ───────────────────────────────────────────────────────────────────
  async function send() {
    const text = inputEl.value.trim();
    if ((!text && !pendingImages.length) || isStreaming) return;

    const imagesToSend = [...pendingImages];
    pendingImages = [];
    renderImagePreviews();
    inputEl.value = "";
    inputEl.style.height = "auto";
    sendBtn.disabled = true;
    isStreaming = true;

    // Build content block for Bedrock
    let userContent;
    if (imagesToSend.length > 0) {
      userContent = [];
      imagesToSend.forEach(img => userContent.push({
        type: "image",
        source: { type: "base64", media_type: img.mimeType, data: img.dataUrl.split(",")[1] },
      }));
      userContent.push({ type: "text", text: text || "Please analyze this image and help me troubleshoot." });
    } else {
      userContent = text;
    }

    // Add user turn to all histories
    AUDIENCES.forEach(aud => histories[aud].push({ role: "user", content: userContent }));

    // Show user question and typing indicator in each column
    const typingEls = {};
    AUDIENCES.forEach(aud => {
      const msgsEl = document.getElementById(`msgs-${aud}`);
      // Clear empty placeholder on first message
      const empty = msgsEl.querySelector(".col-empty");
      if (empty) empty.remove();

      // Question bubble (only show text in first column to save space; all columns get images)
      const turnEl = document.createElement("div");
      turnEl.className = "col-turn";
      let questionHtml = "";
      if (imagesToSend.length > 0) {
        const imgs = imagesToSend.map(img => `<img src="${img.dataUrl}" class="col-question-img" />`).join("");
        questionHtml += `<div class="col-question-images">${imgs}</div>`;
      }
      if (text) questionHtml += `<div class="col-question"><span style="opacity:0.5;flex-shrink:0">You:</span> ${escHtml(text)}</div>`;
      turnEl.innerHTML = questionHtml;

      // Typing indicator
      const typingEl = document.createElement("div");
      typingEl.className = "col-typing";
      typingEl.innerHTML = `<div class="dot"></div><div class="dot"></div><div class="dot"></div>`;
      turnEl.appendChild(typingEl);
      typingEls[aud] = typingEl;

      msgsEl.appendChild(turnEl);
      msgsEl.scrollTop = msgsEl.scrollHeight;
    });

    // Fire three parallel streams
    await Promise.all(AUDIENCES.map(aud => streamAudience(aud, typingEls[aud], text)));

    isStreaming = false;
    sendBtn.disabled = !inputEl.value.trim() && !pendingImages.length;
  }

  async function streamAudience(audience, typingEl, question) {
    const msgsEl = document.getElementById(`msgs-${audience}`);
    const history = histories[audience];

    const answerEl = document.createElement("div");
    answerEl.className = "col-answer";

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, audience, session_id: SESSION_ID }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      typingEl.replaceWith(answerEl);

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
              answerEl.innerHTML = renderMarkdown(fullText);
              msgsEl.scrollTop = msgsEl.scrollHeight;
            } catch (_) {}
          }
        }
      }

      histories[audience].push({ role: "assistant", content: fullText });

      // Feedback bar on installer column only (avoid triple clutter)
      if (audience === "installer") {
        addFeedback(answerEl, question, fullText, audience);
      }

    } catch (err) {
      typingEl.replaceWith(answerEl);
      answerEl.innerHTML = `<span style="color:#ef4444">Error: ${escHtml(err.message)}</span>`;
    }
  }

  function appendColError(msg) {
    AUDIENCES.forEach(aud => {
      const msgsEl = document.getElementById(`msgs-${aud}`);
      const el = document.createElement("div");
      el.className = "col-answer";
      el.style.color = "#ef4444";
      el.textContent = msg;
      msgsEl.appendChild(el);
    });
  }

  // ── Feedback (on installer column) ────────────────────────────────────────
  function addFeedback(msgEl, question, answer, audience) {
    const bar = document.createElement("div");
    bar.className = "feedback-bar";
    bar.innerHTML = `
      <span class="feedback-label">Was this helpful?</span>
      <button class="feedback-btn up" title="Yes">👍</button>
      <button class="feedback-btn down" title="No">👎</button>
      <span class="feedback-thanks" style="display:none">Thanks!</span>
    `;
    msgEl.appendChild(bar);
    bar.querySelector(".up").addEventListener("click", () => sendFeedback("up", question, answer, audience, bar));
    bar.querySelector(".down").addEventListener("click", () => sendFeedback("down", question, answer, audience, bar));
  }

  function sendFeedback(rating, question, answer, audience, bar) {
    fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, audience, question, answer }),
    });
    bar.querySelectorAll(".feedback-btn").forEach(b => b.style.display = "none");
    bar.querySelector(".feedback-label").style.display = "none";
    bar.querySelector(".feedback-thanks").style.display = "inline";
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function escHtml(s) {
    return (s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  function renderMarkdown(text) {
    if (!text) return "";
    let html = text.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
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
    html = html.replace(/(<li>.*<\/li>\n?)+/g, m => `<ol>${m}</ol>`);
    html = html.replace(/^[-*]\s(.+)$/gm, "<li>$1</li>");
    html = html.replace(/(?<!<\/ol>)(<li>(?:(?!<ol>|<\/ol>)[\s\S])*?<\/li>\n?)+/g, m => {
      if (m.includes("<ol>")) return m;
      return `<ul>${m}</ul>`;
    });
    html = html.replace(/^---$/gm, "<hr/>");
    const blocks = html.split(/\n{2,}/);
    html = blocks.map(block => {
      block = block.trim();
      if (!block) return "";
      if (/^<(h[1-6]|ul|ol|pre|hr)/.test(block)) return block;
      return `<p>${block.replace(/\n/g, "<br/>")}</p>`;
    }).join("\n");
    return html;
  }
})();
