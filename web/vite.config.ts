/// <reference types="vitest/config" />

import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const webRoot = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(webRoot, '..');
const cargo = readFileSync(
  resolve(repositoryRoot, 'src-tauri/Cargo.toml'),
  'utf8',
);
const cargoVersion =
  /^\[package\]\s*[\s\S]*?^version\s*=\s*"([^"]+)"\s*$/mu.exec(cargo)?.[1];
const tauriVersion = JSON.parse(
  readFileSync(resolve(repositoryRoot, 'src-tauri/tauri.conf.json'), 'utf8'),
) as { readonly version?: unknown };
const stableVersionSource =
  '(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)';
const prereleaseIdentifier = '(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)';
const exactDesktopVersion = new RegExp(
  `^${stableVersionSource}(?:-${prereleaseIdentifier}(?:\\.${prereleaseIdentifier})*)?$`,
  'u',
);
if (
  cargoVersion === undefined ||
  typeof tauriVersion.version !== 'string' ||
  cargoVersion !== tauriVersion.version ||
  !exactDesktopVersion.test(cargoVersion)
) {
  throw new Error('Cargo and Tauri desktop versions must match exactly');
}

export default defineConfig({
  define: {
    __STOCK_DESK_DESKTOP_VERSION__: JSON.stringify(cargoVersion),
  },
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
      },
    },
  },
  build: {
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: './src/test/setup.ts',
    css: true,
    reporters: process.env.CI ? ['default', 'junit'] : ['default'],
    outputFile: process.env.CI
      ? { junit: '../test-results/vitest/junit.xml' }
      : undefined,
    coverage: {
      provider: 'v8',
      reportsDirectory: 'coverage',
      reporter: ['text', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.d.ts', 'src/test/setup.ts'],
      thresholds: {
        lines: 80,
        statements: 80,
        functions: 80,
        branches: 75,
      },
    },
  },
});
