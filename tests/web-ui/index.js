// Allow the task's explicit `node --test tests/web-ui` entry point as well as discovery.
Promise.all([import('./demo-service.test.mjs'), import('./web-service.test.mjs'),
  import('./pages.test.mjs'), import('./browser.test.mjs')]);