// 멀티턴 AI 채팅 — 펑션콜(비스트리밍) + 최종 응답 SSE 스트리밍 (구현스펙-멀티턴생성-펑션콜.md).
// 서버 /chat/stream 이 SSE(data: {json}) 로 session/tool/token/done 이벤트를 보낸다.
(function () {
  "use strict";
  var chat = document.getElementById("chat");
  if (!chat) return;
  var streamUrl = chat.dataset.streamUrl;
  var applyUrl = chat.dataset.applyUrl;
  var approvalsUrl = chat.dataset.approvalsUrl;
  var project = chat.dataset.project || null;  // 현재 프로젝트(대화 스코프)

  var form = document.getElementById("chatForm");
  var messagesEl = document.getElementById("messages");
  var stagedEl = document.getElementById("staged");
  var sendBtn = document.getElementById("sendBtn");

  function bubble(role, text) {
    var card = document.createElement("div");
    card.className = "card";
    card.style.marginBottom = "var(--space-2)";
    var who = document.createElement("div");
    who.className = "muted";
    who.textContent = role === "user" ? "🧑 나" : "🤖 AI";
    var body = document.createElement("div");
    body.className = "prose";
    body.style.whiteSpace = "pre-wrap";
    body.textContent = text || "";
    card.appendChild(who);
    card.appendChild(body);
    messagesEl.appendChild(card);
    card.scrollIntoView({ block: "end" });
    return body;
  }

  function toolChip(ev) {
    var chip = document.createElement("span");
    chip.className = "badge";
    chip.style.marginRight = "6px";
    var label = ev.name;
    if (ev.args && (ev.args.id || ev.args.doc_id || ev.args.query)) {
      label += "(" + (ev.args.id || ev.args.doc_id || ev.args.query) + ")";
    }
    chip.textContent = "🔧 " + label;
    return chip;
  }

  function renderStaged(staged) {
    stagedEl.innerHTML = "";
    if (!staged || !staged.length) return;
    var box = document.createElement("div");
    box.className = "banner banner-warning";
    var h = document.createElement("strong");
    h.textContent = "제안된 변경 " + staged.length + "건 (적용 대기)";
    box.appendChild(h);
    var ul = document.createElement("ul");
    staged.forEach(function (s) {
      var li = document.createElement("li");
      var lint = s.prelint && s.prelint.ok ? "lint OK"
        : "lint 경고: " + ((s.prelint && s.prelint.reasons) || []).join(", ");
      li.textContent = "[" + s.op + "] " + (s.doc_id || s.target_type || "") + " — " + lint;
      ul.appendChild(li);
    });
    box.appendChild(ul);
    var applyBtn = document.createElement("button");
    applyBtn.className = "btn btn-primary";
    applyBtn.textContent = "✅ 적용 (승인 큐에 제출)";
    applyBtn.addEventListener("click", doApply);
    box.appendChild(applyBtn);
    stagedEl.appendChild(box);
  }

  function doApply() {
    var sid = document.getElementById("sessionId").value;
    var actor = document.getElementById("actor").value || "anonymous";
    fetch(applyUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid, actor: actor }),
    })
      .then(function (r) { return r.json(); })
      .then(function (subs) {
        stagedEl.innerHTML = "";
        var ok = document.createElement("div");
        ok.className = "banner banner-success";
        var list = (subs || []).map(function (s) {
          return s.submission_id + " · " + s.doc_id + " [" + s.op + "]";
        }).join("<br>");
        ok.innerHTML = "<strong>제출됨 — 승인 대기 " + (subs || []).length + "건</strong><br>" +
          list + '<br><a class="btn" href="' + approvalsUrl + '">승인함으로</a>';
        stagedEl.appendChild(ok);
      })
      .catch(function () { alert("적용 실패"); });
  }

  // SSE 스트림 파싱(fetch + ReadableStream — POST 바디를 보내므로 EventSource 대신 사용).
  function stream(body, onEvent) {
    return fetch(streamUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp.ok) throw new Error("stream " + resp.status);
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) return;
          buf += decoder.decode(res.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(function (chunk) {
            var line = chunk.replace(/^data: /, "").trim();
            if (!line) return;
            try { onEvent(JSON.parse(line)); } catch (e) { /* ignore */ }
          });
          return pump();
        });
      }
      return pump();
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var msgInput = document.getElementById("message");
    var text = msgInput.value.trim();
    if (!text) return;
    var actor = document.getElementById("actor").value || "anonymous";
    var targetType = document.getElementById("targetType").value || "adr";
    var sid = document.getElementById("sessionId").value || null;

    bubble("user", text);
    msgInput.value = "";
    sendBtn.disabled = true;
    stagedEl.innerHTML = "";

    var tools = document.createElement("div");
    tools.className = "muted";
    tools.style.margin = "var(--space-2) 0";
    messagesEl.appendChild(tools);
    var assistantBody = null;

    stream(
      { session_id: sid, message: text, actor: actor, target_type: targetType, project: project },
      function (ev) {
        if (ev.type === "session") {
          document.getElementById("sessionId").value = ev.session_id;
        } else if (ev.type === "tool") {
          tools.appendChild(toolChip(ev));
        } else if (ev.type === "token") {
          if (!assistantBody) assistantBody = bubble("assistant", "");
          assistantBody.textContent += ev.text;
          assistantBody.scrollIntoView({ block: "end" });
        } else if (ev.type === "done") {
          if (!assistantBody) bubble("assistant", ev.reply);
          document.getElementById("sessionId").value = ev.session_id;
          renderStaged(ev.staged);
        }
      }
    ).catch(function () {
      bubble("assistant", "(오류: 응답을 받지 못했습니다. LLM 구성/서버 상태를 확인하세요.)");
    }).finally(function () {
      sendBtn.disabled = false;
    });
  });
})();
