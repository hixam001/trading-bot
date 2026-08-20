/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Dark terminal palette
        bg: {
          DEFAULT: "hsl(240, 15%, 5%)",   // #0c0c14
          card: "hsl(240, 12%, 9%)",       // #141420
          elevated: "hsl(240, 10%, 12%)",  // #1a1a24
        },
        border: {
          DEFAULT: "hsl(240, 10%, 18%)",   // #252536
          subtle: "hsl(240, 8%, 13%)",     // #1c1c28
        },
        text: {
          primary: "hsl(215, 28%, 90%)",   // #dde3f0
          secondary: "hsl(215, 15%, 55%)", // #7a8599
          muted: "hsl(215, 10%, 38%)",     // #585f6b
        },
        pass: {
          DEFAULT: "hsl(142, 71%, 45%)",   // #22c55e
          dim: "hsl(142, 40%, 20%)",       // dark green bg
          text: "hsl(142, 71%, 65%)",
        },
        fail: {
          DEFAULT: "hsl(215, 15%, 50%)",   // slate
          dim: "hsl(215, 10%, 13%)",
          text: "hsl(215, 15%, 55%)",
        },
        profit: {
          DEFAULT: "hsl(142, 71%, 45%)",
          text: "hsl(142, 71%, 65%)",
        },
        loss: {
          DEFAULT: "hsl(0, 72%, 51%)",     // #e53e3e
          dim: "hsl(0, 40%, 16%)",
          text: "hsl(0, 72%, 65%)",
        },
        warning: {
          DEFAULT: "hsl(38, 95%, 55%)",    // amber
          dim: "hsl(38, 60%, 16%)",
          text: "hsl(38, 95%, 70%)",
        },
        accent: {
          DEFAULT: "hsl(217, 91%, 60%)",   // blue accent
          dim: "hsl(217, 50%, 16%)",
          text: "hsl(217, 91%, 75%)",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "Consolas", "monospace"],
      },
      animation: {
        "slide-in": "slideIn 0.3s ease-out",
        "fade-in": "fadeIn 0.2s ease-out",
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "glow": "glow 2s ease-in-out infinite alternate",
      },
      keyframes: {
        slideIn: {
          "0%": { opacity: "0", transform: "translateY(-8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        glow: {
          "0%": { boxShadow: "0 0 4px rgba(34,197,94,0.2)" },
          "100%": { boxShadow: "0 0 12px rgba(34,197,94,0.5)" },
        },
      },
    },
  },
  plugins: [],
};
