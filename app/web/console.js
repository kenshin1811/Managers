/* The floor manager's console.

   Speech recognition happens here, in the browser, and nowhere else: the
   words go to the server, the audio never leaves the machine. What the server
   gets is the same string whether it was spoken or typed, so the microphone
   is a convenience on top of a text command rather than a separate feature
   with its own behaviour.

   The Web Speech API is Chrome and Edge, partly Safari, and absent in
   Firefox. Rather than pretend otherwise, the microphone button only appears
   where it will work; the text box is always there. */

(function () {
  const input = $("console-input");
  const form = $("console-form");
  const heard = $("console-heard");
  const reply = $("console-reply");
  const mic = $("btn-mic");
  if (!form) return;

  let busy = false;

  function show(element, text, cls) {
    element.hidden = false;
    element.textContent = text;
    element.className = cls;
  }

  async function send(transcript, source) {
    if (busy || !transcript.trim()) return;
    busy = true;
    show(reply, "Working…", "console-reply");
    try {
      const result = await api("/api/console/command", {
        method: "POST",
        body: JSON.stringify({ transcript, source }),
      });
      show(reply, result.speech, `console-reply ${result.ok ? "is-ok" : "is-bad"}`);
      if (result.ok) input.value = "";
      // Repaint immediately: a re-plan that only showed up on the next poll
      // would make the console feel like it had not worked.
      if (window.__dashboardRefresh) window.__dashboardRefresh();
    } catch (err) {
      show(reply, err.message || String(err), "console-reply is-bad");
    } finally {
      busy = false;
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    heard.hidden = true;
    send(input.value, "typed");
  });

  /* ---------- hints, read from the catalogue ---------- */

  api("/api/console/vocabulary")
    .then((vocab) => {
      $("console-hints").innerHTML = vocab.examples
        .map((example) => `<button class="hint" type="button">${esc(example)}</button>`)
        .join("");
      $("console-hints").addEventListener("click", (event) => {
        const hint = event.target.closest(".hint");
        if (!hint) return;
        input.value = hint.textContent;
        input.focus();
      });
    })
    .catch(() => {
      /* hints are a nicety; the box works without them */
    });

  /* ---------- the microphone ---------- */

  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return;

  mic.hidden = false;
  const recognition = new Recognition();
  recognition.lang = "en-AU";
  recognition.interimResults = true;
  recognition.continuous = false;
  let listening = false;

  recognition.addEventListener("result", (event) => {
    let text = "";
    let final = false;
    for (const result of event.results) {
      text += result[0].transcript;
      final = final || result.isFinal;
    }
    show(heard, `“${text.trim()}”`, "console-heard");
    input.value = text.trim();
    if (final) send(text, "voice");
  });

  recognition.addEventListener("error", (event) => {
    listening = false;
    mic.classList.remove("is-live");
    $("mic-label").textContent = "Speak";
    const message =
      event.error === "not-allowed"
        ? "The microphone is blocked. Allow it in the address bar, or type instead."
        : `Speech recognition stopped: ${event.error}. Typing still works.`;
    show(reply, message, "console-reply is-bad");
  });

  recognition.addEventListener("end", () => {
    listening = false;
    mic.classList.remove("is-live");
    $("mic-label").textContent = "Speak";
  });

  mic.addEventListener("click", () => {
    if (listening) {
      recognition.stop();
      return;
    }
    heard.hidden = true;
    reply.hidden = true;
    try {
      recognition.start();
      listening = true;
      mic.classList.add("is-live");
      $("mic-label").textContent = "Listening…";
    } catch {
      show(reply, "Could not start the microphone.", "console-reply is-bad");
    }
  });
})();
