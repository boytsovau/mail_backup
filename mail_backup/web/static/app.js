// Небольшие улучшения интерфейса. Встроенные скрипты запрещены CSP — всё здесь.

// Подтверждение опасных действий: <form data-confirm="Текст вопроса">
document.addEventListener("submit", (event) => {
  const question = event.target.dataset && event.target.dataset.confirm;
  if (question && !window.confirm(question)) {
    event.preventDefault();
  }
});

// Форма ящика: выбор хостера подставляет сервер, порт и шифрование из пресета
function setupProviderForm() {
  const select = document.getElementById("provider");
  const presetsTag = document.getElementById("presets");
  if (!select || !presetsTag) return;
  const presets = Object.fromEntries(JSON.parse(presetsTag.textContent).map((p) => [p.key, p]));
  const note = document.getElementById("provider-note");

  select.addEventListener("change", () => {
    const preset = presets[select.value];
    if (!preset) return;
    if (preset.host) document.getElementById("host").value = preset.host;
    document.getElementById("port").value = preset.port;
    document.getElementById("security").value = preset.security;
    if (note) note.textContent = preset.note;
  });
}

// Прокрутка лога запуска в конец
function scrollLogs(root) {
  root.querySelectorAll("pre.log").forEach((pre) => {
    pre.scrollTop = pre.scrollHeight;
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupProviderForm();
  scrollLogs(document);
});

// Был ли лог прокручен до конца перед обновлением фрагмента
let logAtBottom = true;

document.addEventListener("htmx:beforeSwap", () => {
  const pre = document.querySelector("pre.log");
  logAtBottom = !pre || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
});

document.addEventListener("htmx:afterSettle", () => {
  if (logAtBottom) scrollLogs(document);
});
