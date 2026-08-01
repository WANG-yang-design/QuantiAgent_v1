/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        up: "#e03131",      // A股红涨
        down: "#2f9e44",    // 绿跌
        brand: {
          50: "#eef4fb", 100: "#d9e6f5", 600: "#1c3a5e", 700: "#162e4c", 900: "#0f1f35",
        },
      },
    },
  },
  plugins: [],
};
