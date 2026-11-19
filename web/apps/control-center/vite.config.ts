import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173 },
  // pdf.js's worker is an ES module; built as one, it is emitted as a .js asset (live/extract/pdfjs.ts).
  worker: { format: 'es' },
  // Lets the attachment tests import their synthetic workbook as a data URL; the app imports no .xlsx.
  assetsInclude: ['**/*.xlsx'],
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // Leaves room for the longer async waits configured in src/test/setup.ts.
    testTimeout: 15_000,
    // Raw CSS sources reach src/rtl.test.tsx, which checks them for physical properties.
    css: { include: [/\.css\?raw$/] },
  },
});
