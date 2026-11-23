import { createDemoService } from './demo-service.js';

// Static demo only. The Web entry point does not load or initialize this store.
const key = (name) => `toy-harness.demo.v1.${name}`;
function storageFailed() {
  document.getElementById('storage-warning').hidden = false;
}

function read(name) {
  try { return JSON.parse(sessionStorage.getItem(key(name))); }
  catch { storageFailed(); return null; }
}

function save(name, value) {
  try { sessionStorage.setItem(key(name), JSON.stringify(value)); }
  catch { storageFailed(); }
}

export const selectedSession = { read: () => read('selected'), save: (id) => save('selected', id) };
const saveState = (snapshot) => save('state', snapshot);
function startDemo() {
  try { return createDemoService({ snapshot: read('state'), save: saveState }); }
  catch { storageFailed(); return createDemoService({ save: saveState }); }
}
export const service = startDemo();