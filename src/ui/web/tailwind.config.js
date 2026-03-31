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
        "fade-in": {
          from: { opacity: "0", transform: "translateX(-50%) translateY(8px)" },
          to: { opacity: "1", transform: "translateX(-50%) translateY(0)" },
        },
      },
      animation: {
        "lv-progress": "lv-progress 3s linear forwards",
        "fade-in": "fade-in 0.2s ease-out",
      },
    },
  },
  plugins: [safeAreaPlugin],
};
