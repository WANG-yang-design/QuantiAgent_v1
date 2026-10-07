/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        up: "#e03131",      // A股红涨
        down: "#2f9e44",    // 绿跌
        brand: {
          50: "#eef4fb", 100: "#d9e6f5", 200: "#b7cfeb", 300: "#8fb2dc",
          400: "#5f8ec7", 500: "#3f6fa8", 600: "#1c3a5e", 700: "#162e4c",
          800: "#122740", 900: "#0f1f35",
        },
      },
      keyframes: {
        "fade-in": { "0%": { opacity: 0, transform: "translateY(4px)" },
                     "100%": { opacity: 1, transform: "translateY(0)" } },
        "toast-in": { "0%": { opacity: 0, transform: "translateY(-8px) scale(.98)" },
                      "100%": { opacity: 1, transform: "translateY(0) scale(1)" } },
      },
      animation: {
        "fade-in": "fade-in .18s ease-out",
        "toast-in": "toast-in .2s ease-out",
      },
    },
  },
  plugins: [],
};
