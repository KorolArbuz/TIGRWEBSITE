(() => {
  "use strict";

  const STORAGE_KEY = "hoco_catalog_cart_v1";
  const MAX_QUANTITY = 999;
  let toastTimer = null;

  function clampQuantity(value, allowZero = true) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return allowZero ? 0 : 1;
    const rounded = Math.round(parsed);
    const minimum = allowZero ? 0 : 1;
    return Math.max(minimum, Math.min(MAX_QUANTITY, rounded));
  }

  function readCart() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      const clean = {};
      Object.values(parsed).forEach((item) => {
        if (!item || typeof item !== "object") return;
        const id = Number(item.id);
        const quantity = Number(item.quantity);
        const priceCents = Number(item.priceCents);
        if (!Number.isInteger(id) || id <= 0 || !Number.isFinite(priceCents)) return;
        clean[id] = {
          id,
          title: String(item.title || "Товар"),
          barcode: String(item.barcode || ""),
          image: String(item.image || "/static/img/placeholder.svg"),
          priceCents: Math.max(0, Math.round(priceCents)),
          quantity: clampQuantity(quantity || 1, false),
        };
      });
      return clean;
    } catch (_error) {
      return {};
    }
  }

  function writeCart(cart) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cart));
  }

  function money(cents) {
    const amount = Math.max(0, Number(cents) || 0) / 100;
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency: "RUB",
      minimumFractionDigits: Number.isInteger(amount) ? 0 : 2,
      maximumFractionDigits: 2,
    }).format(amount);
  }

  function totalQuantity(cart) {
    return Object.values(cart).reduce((sum, item) => sum + item.quantity, 0);
  }

  function totalCents(cart) {
    return Object.values(cart).reduce((sum, item) => sum + item.priceCents * item.quantity, 0);
  }

  function showToast(message) {
    const toast = document.querySelector(".js-toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2100);
  }

  function openCart() {
    document.body.classList.add("drawer-open");
    const drawer = document.querySelector(".cart-drawer");
    if (drawer) drawer.setAttribute("aria-hidden", "false");
  }

  function closeCart() {
    document.body.classList.remove("drawer-open");
    const drawer = document.querySelector(".cart-drawer");
    if (drawer) drawer.setAttribute("aria-hidden", "true");
  }

  function productFromQuantityControl(element) {
    const control = element.closest(".js-product-quantity");
    if (!(control instanceof HTMLElement)) return null;
    const id = Number(control.dataset.productId);
    const priceCents = Number(control.dataset.priceCents || 0);
    if (!Number.isInteger(id) || id <= 0 || !Number.isFinite(priceCents)) return null;
    return {
      id,
      title: String(control.dataset.title || "Товар"),
      priceCents: Math.max(0, Math.round(priceCents)),
      image: String(control.dataset.image || "/static/img/placeholder.svg"),
      barcode: String(control.dataset.barcode || ""),
      quantity: 1,
    };
  }

  function setProductQuantity(product, quantity, { notify = false } = {}) {
    if (!product || !Number.isInteger(product.id) || product.id <= 0) return;
    const cart = readCart();
    const previous = cart[product.id]?.quantity || 0;
    const next = clampQuantity(quantity, true);

    if (next <= 0) {
      delete cart[product.id];
    } else {
      cart[product.id] = {
        id: product.id,
        title: product.title,
        barcode: product.barcode,
        image: product.image,
        priceCents: product.priceCents,
        quantity: next,
      };
    }

    writeCart(cart);
    renderAll();
    if (notify && previous === 0 && next > 0) showToast("Товар добавлен в корзину");
    if (notify && previous > 0 && next === 0) showToast("Товар удалён из корзины");
  }

  function setQuantity(id, quantity) {
    const cart = readCart();
    if (!cart[id]) return;
    const next = clampQuantity(quantity, true);
    if (next <= 0) delete cart[id];
    else cart[id].quantity = next;
    writeCart(cart);
    renderAll();
  }

  function createCartItem(item) {
    const root = document.createElement("div");
    root.className = "cart-item";

    const image = document.createElement("img");
    image.className = "cart-item-image";
    image.src = item.image;
    image.alt = "";
    image.loading = "lazy";

    const copy = document.createElement("div");
    copy.className = "cart-item-copy";
    const title = document.createElement("strong");
    title.className = "cart-item-title";
    title.textContent = item.title;
    const code = document.createElement("small");
    code.textContent = item.barcode ? `Код ${item.barcode}` : "";
    const qty = document.createElement("div");
    qty.className = "qty-control";

    const minus = document.createElement("button");
    minus.type = "button";
    minus.dataset.cartAction = "minus";
    minus.dataset.productId = String(item.id);
    minus.setAttribute("aria-label", "Уменьшить количество");
    minus.textContent = "−";

    const value = document.createElement("input");
    value.className = "qty-input js-cart-quantity-input";
    value.type = "number";
    value.inputMode = "numeric";
    value.min = "0";
    value.max = String(MAX_QUANTITY);
    value.step = "1";
    value.value = String(item.quantity);
    value.dataset.productId = String(item.id);
    value.setAttribute("aria-label", "Количество товара в корзине");

    const plus = document.createElement("button");
    plus.type = "button";
    plus.dataset.cartAction = "plus";
    plus.dataset.productId = String(item.id);
    plus.setAttribute("aria-label", "Увеличить количество");
    plus.textContent = "+";
    qty.append(minus, value, plus);
    copy.append(title, code, qty);

    const side = document.createElement("div");
    side.className = "cart-item-side";
    const subtotal = document.createElement("strong");
    subtotal.textContent = money(item.priceCents * item.quantity);
    const remove = document.createElement("button");
    remove.className = "remove-button";
    remove.type = "button";
    remove.dataset.cartAction = "remove";
    remove.dataset.productId = String(item.id);
    remove.textContent = "Удалить";
    side.append(subtotal, remove);

    root.append(image, copy, side);
    return root;
  }

  function renderDrawer(cart) {
    const itemsRoot = document.querySelector(".js-cart-items");
    const empty = document.querySelector(".js-cart-empty");
    const footer = document.querySelector(".js-cart-footer");
    const total = document.querySelector(".js-cart-total");
    if (!itemsRoot || !empty || !footer || !total) return;
    itemsRoot.replaceChildren();
    const items = Object.values(cart);
    items.forEach((item) => itemsRoot.append(createCartItem(item)));
    empty.hidden = items.length > 0;
    footer.hidden = items.length === 0;
    total.textContent = money(totalCents(cart));
  }

  function createCheckoutItem(item) {
    const root = document.createElement("div");
    root.className = "checkout-item";
    const image = document.createElement("img");
    image.src = item.image;
    image.alt = "";
    image.loading = "lazy";
    const copy = document.createElement("div");
    copy.className = "checkout-item-copy";
    const title = document.createElement("strong");
    title.textContent = item.title;
    const detail = document.createElement("small");
    detail.textContent = `${money(item.priceCents)} × ${item.quantity}`;
    copy.append(title, detail);
    const sum = document.createElement("strong");
    sum.textContent = money(item.priceCents * item.quantity);
    root.append(image, copy, sum);
    return root;
  }

  function renderCheckout(cart) {
    const root = document.querySelector(".js-checkout-items");
    const empty = document.querySelector(".js-checkout-empty");
    const totalWrap = document.querySelector(".js-checkout-total-wrap");
    const total = document.querySelector(".js-checkout-total");
    const hidden = document.querySelector(".js-cart-json");
    const submit = document.querySelector(".js-submit-order");
    if (!root || !empty || !totalWrap || !total || !hidden || !submit) return;
    const items = Object.values(cart);
    root.replaceChildren();
    items.forEach((item) => root.append(createCheckoutItem(item)));
    hidden.value = JSON.stringify(items.map((item) => ({ id: item.id, quantity: item.quantity })));
    empty.hidden = items.length > 0;
    totalWrap.hidden = items.length === 0;
    total.textContent = money(totalCents(cart));
    submit.disabled = items.length === 0;
  }

  function renderProductQuantityControls(cart) {
    document.querySelectorAll(".js-product-quantity").forEach((control) => {
      if (!(control instanceof HTMLElement)) return;
      const id = Number(control.dataset.productId);
      const quantity = cart[id]?.quantity || 0;
      const input = control.querySelector(".js-product-quantity-input");
      if (input instanceof HTMLInputElement && document.activeElement !== input) {
        input.value = String(quantity);
      }
      control.classList.toggle("has-items", quantity > 0);
    });
  }

  function renderAll() {
    const cart = readCart();
    document.querySelectorAll(".js-cart-count").forEach((counter) => {
      counter.textContent = String(totalQuantity(cart));
    });
    renderDrawer(cart);
    renderCheckout(cart);
    renderProductQuantityControls(cart);
  }

  async function syncCart() {
    const cart = readCart();
    const ids = Object.keys(cart);
    if (!ids.length) {
      renderAll();
      return;
    }
    try {
      const response = await fetch(`/api/products?ids=${encodeURIComponent(ids.join(","))}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const current = {};
      (payload.products || []).forEach((product) => {
        const old = cart[product.id];
        if (!old) return;
        current[product.id] = {
          id: product.id,
          title: product.title,
          barcode: product.barcode,
          image: product.image,
          priceCents: product.price_cents,
          quantity: old.quantity,
        };
      });
      writeCart(current);
    } catch (_error) {
      // Offline or temporary API issue: keep the local cart. The server validates it at checkout.
    }
    renderAll();
  }

  function updateDeliveryFields() {
    const choice = document.querySelector(".js-delivery-choice:checked");
    const addressField = document.querySelector(".js-address-field");
    if (!choice || !addressField) return;
    const textarea = addressField.querySelector("textarea");
    const needsAddress = choice.value === "courier";
    addressField.hidden = !needsAddress;
    if (textarea) textarea.required = needsAddress;
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;

    const productAction = target.closest("[data-product-qty-action]");
    if (productAction instanceof HTMLElement) {
      event.preventDefault();
      const product = productFromQuantityControl(productAction);
      if (!product) return;
      const cart = readCart();
      const current = cart[product.id]?.quantity || 0;
      const delta = productAction.dataset.productQtyAction === "plus" ? 1 : -1;
      setProductQuantity(product, current + delta, { notify: current === 0 || current + delta === 0 });
      return;
    }

    if (target.closest(".js-cart-open")) {
      event.preventDefault();
      openCart();
      return;
    }
    if (target.closest(".js-cart-close")) {
      event.preventDefault();
      closeCart();
      return;
    }
    if (target.closest(".js-cart-clear")) {
      event.preventDefault();
      writeCart({});
      renderAll();
      showToast("Корзина очищена");
      return;
    }

    const cartAction = target.closest("[data-cart-action]");
    if (cartAction instanceof HTMLElement) {
      event.preventDefault();
      const id = Number(cartAction.dataset.productId);
      const cart = readCart();
      const item = cart[id];
      if (!item) return;
      if (cartAction.dataset.cartAction === "plus") setQuantity(id, item.quantity + 1);
      if (cartAction.dataset.cartAction === "minus") setQuantity(id, item.quantity - 1);
      if (cartAction.dataset.cartAction === "remove") setQuantity(id, 0);
      return;
    }

    const thumb = target.closest(".js-gallery-thumb");
    if (thumb instanceof HTMLElement) {
      event.preventDefault();
      const main = document.querySelector(".js-gallery-main");
      if (main instanceof HTMLImageElement && thumb.dataset.image) main.src = thumb.dataset.image;
      document.querySelectorAll(".js-gallery-thumb").forEach((node) => node.classList.remove("is-active"));
      thumb.classList.add("is-active");
    }
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    if (target.matches(".js-auto-submit")) {
      const form = target.closest("form");
      if (form instanceof HTMLFormElement) form.submit();
    }
    if (target.matches(".js-delivery-choice")) updateDeliveryFields();

    if (target instanceof HTMLInputElement && target.matches(".js-product-quantity-input")) {
      const product = productFromQuantityControl(target);
      if (!product) return;
      const previous = readCart()[product.id]?.quantity || 0;
      const next = clampQuantity(target.value, true);
      setProductQuantity(product, next, { notify: previous === 0 || next === 0 });
    }

    if (target instanceof HTMLInputElement && target.matches(".js-cart-quantity-input")) {
      const id = Number(target.dataset.productId);
      if (!Number.isInteger(id) || id <= 0) return;
      setQuantity(id, clampQuantity(target.value, true));
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeCart();
    const target = event.target;
    if (
      event.key === "Enter" &&
      target instanceof HTMLInputElement &&
      (target.matches(".js-product-quantity-input") || target.matches(".js-cart-quantity-input"))
    ) {
      event.preventDefault();
      target.blur();
    }
  });

  const checkoutForm = document.querySelector(".js-checkout-form");
  if (checkoutForm instanceof HTMLFormElement) {
    checkoutForm.addEventListener("submit", (event) => {
      const cart = readCart();
      if (!Object.keys(cart).length) {
        event.preventDefault();
        showToast("Корзина пуста");
        return;
      }
      const button = checkoutForm.querySelector(".js-submit-order");
      if (button instanceof HTMLButtonElement) {
        button.disabled = true;
        button.textContent = "Отправляем заказ…";
      }
    });
  }

  if (document.body.dataset.clearCart === "1") writeCart({});
  updateDeliveryFields();
  renderAll();
  syncCart();
})();
