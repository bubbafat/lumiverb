// Node 25 and later have a Web Storage of their own: its global localStorage
// (and sessionStorage) is undefined without --localstorage-file, and it shadows
// jsdom's, so a test's token or setting went nowhere. Use jsdom's.
const own = (globalThis as unknown as { jsdom?: { window: Window } }).jsdom?.window;
for (const name of ["localStorage", "sessionStorage"] as const) {
  if (own && typeof (globalThis as Record<string, unknown>)[name] !== "object") {
    Object.defineProperty(globalThis, name, { value: own[name], configurable: true, writable: true });
  }
}
