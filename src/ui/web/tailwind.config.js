/** @type {import('tailwindcss').Config} */
import safeAreaPlugin from "tailwindcss-safe-area";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      keyframes: {
        "lv-progress": {
          from: { width: "0%" },
          to: { width: "100%" },
        },
      },
      animation: {
        "lv-progress": "lv-progress 3s linear forwards",
      },
    },
  },
  plugins: [safeAreaPlugin],
};
