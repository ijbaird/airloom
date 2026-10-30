(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AirloomState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class NavigationGate {
    constructor() {
      this.current = { fly: null, zoom: null, direct: null };
      this.nextToken = 0;
    }

    begin(kind) {
      if (!Object.hasOwn(this.current, kind)) throw new RangeError(`Unknown navigation kind: ${kind}`);
      for (const other of Object.keys(this.current)) {
        if (other !== kind) this.cancel(other);
      }
      const token = ++this.nextToken;
      this.current[kind] = token;
      return token;
    }

    cancel(kind) {
      if (Object.hasOwn(this.current, kind)) this.current[kind] = null;
    }

    isCurrent(kind, token) {
      return token !== null && token !== undefined && this.current[kind] === token;
    }
  }

  function reconcileKeyed(container, items, key, create, update) {
    const document = container.ownerDocument || (typeof globalThis.document !== "undefined" ? globalThis.document : null);
    const active = document?.activeElement || null;
    const surface = container.dataset.focusSurface || container.id || null;
    const children = [...container.childNodes];
    const existing = new Map();
    let focused = null;

    for (const node of children) {
      const nodeKey = node.dataset?.key;
      if (nodeKey !== undefined) existing.set(String(nodeKey), node);
      if (active && (node === active || node.contains(active))) {
        focused = { key: nodeKey, surface, element: active };
      }
    }

    const next = [];
    const retained = new Set();
    for (const item of items) {
      const itemKey = String(key(item));
      const node = existing.get(itemKey) || create(item);
      node.dataset.key = itemKey;
      update(node, item);
      next.push({ key: itemKey, node });
      retained.add(node);
    }

    for (const node of children) {
      if (!retained.has(node)) node.remove();
    }
    next.forEach(({ node }, index) => {
      const reference = container.childNodes[index] || null;
      if (reference !== node) container.insertBefore(node, reference);
    });

    let removedFocus = null;
    if (focused) {
      const match = next.find(({ key: itemKey }) => itemKey === focused.key);
      if (match) {
        const target = focused.element.isConnected && typeof focused.element.focus === "function" ? focused.element : match.node;
        target.focus();
      } else {
        removedFocus = { key: focused.key, surface };
      }
    }
    return { removedFocus };
  }

  return { NavigationGate, reconcileKeyed };
});
