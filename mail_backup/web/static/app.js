// Небольшие улучшения интерфейса. Встроенные скрипты запрещены CSP — всё здесь.

// Подтверждение опасных действий: <form data-confirm="Текст вопроса">
// или <button data-confirm="..."> — когда в форме несколько кнопок и спросить надо только у одной
document.addEventListener("submit", (event) => {
  const question =
    (event.submitter && event.submitter.dataset.confirm) ||
    (event.target.dataset && event.target.dataset.confirm);
  if (question && !window.confirm(question)) {
    event.preventDefault();
  }
});

// Условные части формы: <div data-show-if="имя:значение"> видна, только когда
// выбран переключатель name="имя" со значением «значение»
function updateConditional(form) {
  form.querySelectorAll("[data-show-if]").forEach((el) => {
    const [name, value] = el.dataset.showIf.split(":");
    const checked = form.querySelector(`input[name="${name}"]:checked`);
    el.hidden = !checked || checked.value !== value;
    // Скрытый переключатель не должен оставаться выбранным — выбираем первый видимый
    el.querySelectorAll("input[type=radio]:checked").forEach((radio) => {
      if (!el.hidden) return;
      const visible = [...form.querySelectorAll(`input[name="${radio.name}"]`)].find(
        (r) => !r.closest("[hidden]") && !r.disabled,
      );
      if (visible) {
        visible.checked = true;
        updateConditional(form);
      }
    });
  });
}

function setupConditional() {
  document.querySelectorAll("form").forEach((form) => {
    if (!form.querySelector("[data-show-if]")) return;
    updateConditional(form);
    form.addEventListener("change", () => updateConditional(form));
  });
}

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
  setupConditional();
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
