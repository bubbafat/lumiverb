import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // The dev server is for this machine only. Its proxy passes on the Host the
    // browser asked for and who asked (X-Forwarded-For), as nginx does, so the API
    // sees each request as it was sent (src/server/api/client_address.py).
    host: "127.0.0.1",
    proxy: {
      "/v1": {
        target: "http://localhost:8000",
        changeOrigin: false,
        xfwd: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
