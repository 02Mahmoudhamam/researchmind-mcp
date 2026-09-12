/**
 * PostCSS configuration.
 *
 * Next.js ships a default PostCSS pipeline, but that default does NOT include
 * Tailwind. Without this file `src/app/globals.css` was emitted verbatim: the
 * production build succeeded while shipping the literal text
 * `@tailwind base;@tailwind components;@tailwind utilities;` to the browser and
 * zero utility classes, so every page rendered unstyled.
 *
 * Defining this file replaces Next's default pipeline entirely, so the plugins
 * below are the complete list.
 *
 * This is the Tailwind v3 plugin form (`tailwindcss` as its own PostCSS
 * plugin). Tailwind v4's `@tailwindcss/postcss` is deliberately NOT used — see
 * tailwind.config.ts for the installed version.
 */
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
