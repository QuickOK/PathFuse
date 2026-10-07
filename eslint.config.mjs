import js from "@eslint/js";
import globals from "globals";
export default [
  { ignores: ["ui/vendor/**", "node_modules/**"] },
  js.configs.recommended,
  { files: ["ui/**/*.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "script",
                       globals: { ...globals.browser, L: "readonly" } } },
  { files: ["tests/js/**/*.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "commonjs", globals: { ...globals.node } } },
  { files: ["eslint.config.mjs"], languageOptions: { sourceType: "module" } },
];
