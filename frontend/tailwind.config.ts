import type { Config } from "tailwindcss";

/**
 * Tailwind CSS configuration — v3 format.
 *
 * Pinned stack: tailwindcss 3.4.x, postcss 8.4.x, autoprefixer 10.4.x.
 * v3 requires an explicit `content` list; with no config file Tailwind defaults
 * to `content: []` and emits no utilities at all, which is silent rather than
 * fatal. Every glob below must therefore cover every file that names a class.
 *
 * `src/` is the only source root in this project (there is no top-level
 * `app/`, `pages/`, or `components/` directory).
 */
const config: Config = {
  content: ["./src/**/*.{js,jsx,ts,tsx,mdx}"],
  theme: {
    extend: {},
  },
  plugins: [],
};

export default config;
