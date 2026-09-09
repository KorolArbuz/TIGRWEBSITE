(() => {
  "use strict";

  const input = document.querySelector(".js-file-input");
  const dropzone = document.querySelector(".js-dropzone");
  const label = document.querySelector(".js-file-label");

  function updateFileLabel() {
    if (!input || !label) return;
    const count = input.files ? input.files.length : 0;
    if (!count) label.textContent = "или нажмите, чтобы выбрать файлы";
    else if (count === 1) label.textContent = input.files[0].name;
    else label.textContent = `Выбрано файлов: ${count}`;
  }

  if (input) input.addEventListener("change", updateFileLabel);
  if (dropzone) {
    ["dragenter", "dragover"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-dragging");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropzone.addEventListener(name, () => dropzone.classList.remove("is-dragging"));
    });
  }

  document.querySelectorAll(".clickable-row").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest("a, button, input, select")) return;
      if (row.dataset.href) window.location.href = row.dataset.href;
    });
  });

  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", () => {
      const submit = form.querySelector('button[type="submit"]');
      if (!submit || submit.dataset.noLock === "1") return;
      window.setTimeout(() => {
        submit.disabled = true;
        if (submit.textContent && !submit.textContent.includes("…")) submit.textContent = "Обрабатываем…";
      }, 0);
    });
  });
})();
