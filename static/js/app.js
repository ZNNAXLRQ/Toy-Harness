// Mode comes from the launch URL, never from whether an API probe succeeds.
if (new URLSearchParams(location.search).get('mode') === 'web') {
  await import('./web-app.js');
} else {
  await import('./demo-app.js');
}