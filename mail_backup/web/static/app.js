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

// Фильтры, которые применяются сразу при выборе: <form data-autosubmit>
function setupAutosubmit() {
  document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
    form.addEventListener("change", () => form.requestSubmit());
  });
}

// --- Графики (Chart.js) --------------------------------------------------------------
// Данные — в <script type="application/json" id="…">, холст — <canvas data-chart="ключ"
// data-source="id">. Формат готовит mail_backup/stats.py: {labels, charts: {ключ: {type,
// unit, datasets: [{label, color, data}]}}}. Цвета берутся из CSS-переменных темы.

const SIZE_UNITS = ["Б", "КБ", "МБ", "ГБ", "ТБ"];

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Цвет набора: номер палитры «1»…«8», «other» или статус (ok/warn/error/muted)
function seriesColor(color) {
  if (/^\d$/.test(color)) return cssVar(`--series-${color}`);
  if (color === "other") return cssVar("--series-other");
  return cssVar(`--${color}`);
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function fmtNumber(value, digits = 0) {
  return value.toLocaleString("ru-RU", { maximumFractionDigits: digits });
}

// Как fmt_size в web/common.py
function fmtSize(bytes) {
  let size = bytes;
  for (const unit of SIZE_UNITS) {
    if (size < 1024 || unit === "ТБ") return unit === "Б" ? `${fmtNumber(size)} ${unit}` : `${fmtNumber(size, 1)} ${unit}`;
    size /= 1024;
  }
}

function fmtMinutes(minutes) {
  if (minutes < 1) return `${Math.round(minutes * 60)} с`;
  if (minutes < 60) return `${fmtNumber(minutes, 1)} мин`;
  return `${Math.floor(minutes / 60)} ч ${Math.round(minutes % 60)} мин`;
}

function fmtDay(iso, withYear) {
  const [y, m, d] = iso.split("-");
  return withYear ? `${d}.${m}.${y}` : `${d}.${m}`;
}

// Байты на оси — в одной единице (КБ/МБ/ГБ) по наибольшему значению, чтобы деления
// были круглыми числами этой единицы
function byteScale(spec, stacked) {
  const n = spec.datasets[0] ? spec.datasets[0].data.length : 0;
  let max = 0;
  for (let i = 0; i < n; i++) {
    const values = spec.datasets.map((ds) => ds.data[i] || 0);
    max = Math.max(max, stacked ? values.reduce((a, b) => a + b, 0) : Math.max(...values));
  }
  let power = 0;
  while (max >= 1024 && power < SIZE_UNITS.length - 1) {
    max /= 1024;
    power++;
  }
  return { divisor: 1024 ** power, unit: SIZE_UNITS[power] };
}

function formatterFor(unit) {
  if (unit === "bytes") return fmtSize;
  if (unit === "minutes") return fmtMinutes;
  return (v) => fmtNumber(v);
}

function buildChart(canvas) {
  const source = JSON.parse(document.getElementById(canvas.dataset.source).textContent);
  const spec = source.charts[canvas.dataset.chart];
  const area = spec.type === "area";
  const scale = spec.unit === "bytes" ? byteScale(spec, true) : { divisor: 1, unit: "" };
  const format = formatterFor(spec.unit);
  const surface = cssVar("--surface");
  const muted = cssVar("--muted");
  const many = source.labels.length > 31;

  const datasets = spec.datasets.map((ds, index) => {
    const color = seriesColor(ds.color);
    const data = ds.data.map((v) => (v === null ? null : v / scale.divisor));
    if (area) {
      return {
        label: ds.label, data, rawData: ds.data, type: "line",
        borderColor: color, backgroundColor: withAlpha(color, 0.25), borderWidth: 2,
        fill: index === 0 ? "origin" : "-1", pointRadius: 0, pointHoverRadius: 4,
        pointHoverBorderColor: surface, pointHoverBorderWidth: 2, tension: 0,
      };
    }
    return {
      label: ds.label, data, rawData: ds.data,
      backgroundColor: color, hoverBackgroundColor: color,
      // Полоса цвета поверхности сверху — зазор между слоями стопки
      borderColor: surface, borderWidth: { top: 2 }, borderSkipped: "start", borderRadius: 4,
      maxBarThickness: 28, categoryPercentage: many ? 0.85 : 0.8, barPercentage: many ? 1 : 0.9,
    };
  });

  return new Chart(canvas, {
    type: "bar",
    data: { labels: source.labels, datasets },
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          stacked: true,
          grid: { display: false },
          border: { color: cssVar("--border") },
          ticks: {
            color: muted, maxRotation: 0, autoSkipPadding: 12,
            callback(value) { return fmtDay(source.labels[value], false); },
          },
        },
        y: {
          stacked: true,
          beginAtZero: true,
          grid: { color: cssVar("--border") },
          border: { display: false },
          ticks: {
            color: muted, precision: spec.unit === "count" ? 0 : undefined, maxTicksLimit: 6,
            callback(value) {
              if (spec.unit === "bytes") return `${fmtNumber(value, 1)} ${scale.unit}`;
              if (spec.unit === "minutes") return `${fmtNumber(value)} мин`;
              return fmtNumber(value);
            },
          },
        },
      },
      plugins: {
        // Легенда — только когда наборов больше одного: одиночный называет заголовок
        legend: {
          display: datasets.length > 1,
          position: "bottom",
          labels: { color: cssVar("--text"), boxWidth: 12, boxHeight: 12, useBorderRadius: true, borderRadius: 3 },
        },
        tooltip: {
          // Пустые слои в подсказке не нужны
          filter: (item) => item.dataset.rawData[item.dataIndex],
          itemSort: (a, b) => b.datasetIndex - a.datasetIndex,
          callbacks: {
            title: (items) => fmtDay(source.labels[items[0].dataIndex], true),
            label: (item) => ` ${item.dataset.label}: ${format(item.dataset.rawData[item.dataIndex])}`,
            footer(items) {
              if (items.length < 2) return "";
              const total = items.reduce((sum, item) => sum + item.dataset.rawData[item.dataIndex], 0);
              return `Всего: ${format(total)}`;
            },
          },
        },
      },
    },
  });
}

function renderCharts(root) {
  if (!window.Chart) return;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  // Графики, чьи холсты убрал htmx, освобождаем
  Object.values(Chart.instances).forEach((chart) => {
    if (!document.contains(chart.canvas)) chart.destroy();
  });
  root.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
    const old = Chart.getChart(canvas);
    if (old) old.destroy();
    buildChart(canvas);
  });
}

// Смена светлой/тёмной темы — перерисовываем с новыми цветами
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderCharts(document));

document.addEventListener("DOMContentLoaded", () => {
  setupProviderForm();
  setupConditional();
  setupAutosubmit();
  scrollLogs(document);
  renderCharts(document);
});

// Был ли лог прокручен до конца перед обновлением фрагмента
let logAtBottom = true;

document.addEventListener("htmx:beforeSwap", () => {
  const pre = document.querySelector("pre.log");
  logAtBottom = !pre || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
});

document.addEventListener("htmx:afterSettle", (event) => {
  if (logAtBottom) scrollLogs(document);
  renderCharts(event.target);
});
