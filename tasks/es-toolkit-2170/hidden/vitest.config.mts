import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['hidden_tests/**/*.spec.ts'],
  },
});
