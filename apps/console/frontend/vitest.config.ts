import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';

export default defineConfig({
  esbuild: {
    jsx: 'automatic',
    jsxImportSource: 'react',
  },
  resolve: {
    alias: {
      '@testing-library/react': resolve(process.cwd(), 'node_modules/@testing-library/react/dist/@testing-library/react.esm.js'),
      '@testing-library/user-event': resolve(process.cwd(), 'node_modules/@testing-library/user-event/dist/esm/index.js'),
      '@testing-library/jest-dom/vitest': resolve(process.cwd(), 'node_modules/@testing-library/jest-dom/dist/vitest.mjs'),
      'react/jsx-runtime': resolve(process.cwd(), 'node_modules/react/jsx-runtime.js'),
      'react/jsx-dev-runtime': resolve(process.cwd(), 'node_modules/react/jsx-dev-runtime.js'),
    },
  },
  server: {
    fs: {
      allow: [resolve(process.cwd(), '..')],
    },
  },
  test: {
    dir: resolve(process.cwd(), '../tests/frontend'),
    environment: 'jsdom',
    globals: true,
    include: ['**/*.test.{ts,tsx}'],
    css: true,
  },
});
