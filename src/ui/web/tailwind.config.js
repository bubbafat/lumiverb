/** @type {import('tailwindcss').Config} */
import safeAreaPlugin from "tailwindcss-safe-area";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {},
  },
  plugins: [safeAreaPlugin],
};
