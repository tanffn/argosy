import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Honor the `_`-prefix convention for deliberately-unused bindings
  // (caught errors we ignore, placeholder args/vars).
  {
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
  // Client-copy guard for the proposals decision surface: a client surface is
  // a manual, not an internals dump. Keep raw state-machine enums and raw API
  // paths out of what the user reads. Scoped here (the canonical client action
  // surface) to stay high-signal; widen the glob as other surfaces are audited.
  {
    files: ["src/app/proposals/**/*.tsx", "src/components/proposals/**/*.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          // Rendering a raw `.status` (a state-machine enum like
          // "awaiting_human" / "executed_paper") directly in JSX. Wrap it in
          // friendlyStatus() so the client reads "Needs your decision", etc.
          selector:
            "JSXElement > JSXExpressionContainer > MemberExpression[property.name='status']",
          message:
            "Don't render a raw proposal .status in client JSX — wrap it in friendlyStatus() (raw state-machine enums read as internal jargon).",
        },
        {
          // A raw `/api/...` path shown to the client (JSX text).
          selector: "JSXText[value=/\\/api\\//]",
          message:
            "Don't show a raw /api/ path on a client surface — describe what it does in plain language.",
        },
        {
          // A raw `/api/...` path shown to the client (string-literal child).
          selector:
            "JSXElement > JSXExpressionContainer Literal[value=/\\/api\\//]",
          message:
            "Don't show a raw /api/ path on a client surface — describe what it does in plain language.",
        },
        {
          // A raw `/api/...` path shown to the client (template-literal child).
          selector:
            "JSXElement > JSXExpressionContainer TemplateElement[value.cooked=/\\/api\\//]",
          message:
            "Don't show a raw /api/ path on a client surface — describe what it does in plain language.",
        },
        {
          // Hardcoded internal status enums in client copy.
          selector:
            "JSXText[value=/awaiting_human|executed_paper|executed_live|cooling_off/]",
          message:
            "Don't put raw status enums in client copy — use plain language (see friendlyStatus).",
        },
      ],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
