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

  return { NavigationGate };
});
