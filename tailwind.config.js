/* Tailwind build configuration. The compiled stylesheet is committed; installing or running the plugin never
   needs Node. Theme colours read the board's CSS custom properties, so the light/dark (and project design-profile)
   switch stays a variable swap. */
module.exports = {
  content: ["./mockreview/board/**/*.{html,js}", "./mockreview/pages.py"],
  theme: {
    extend: {
      colors: {
        background: "var(--b)",
        surface: "var(--s)",
        surface2: "var(--s2)",
        line: "var(--l)",
        text: "var(--t)",
        muted: "var(--m)",
        accent: "var(--a)",
        accentInk: "var(--ai)",
        accentSoft: "var(--as)",
        ok: "var(--ok)",
        warn: "var(--wn)",
        pin: "var(--pin)"
      },
      borderRadius: { control: "var(--r)", card: "var(--rc)" },
      boxShadow: { pop: "var(--sh)" },
      fontFamily: { sans: ["var(--font)"] }
    }
  }
};
