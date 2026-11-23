import { createDemoService } from './demo-service.mjs';

// Composition root. A future REST/SSE adapter implements the same async queries,
// commands and subscribe(sessionId, listener) => unsubscribe interface.
export const service = createDemoService();