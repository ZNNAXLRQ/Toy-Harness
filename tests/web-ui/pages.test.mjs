import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../../static/', import.meta.url);
const pages = ['index', 'approvals', 'sessions', 'checkpoints', 'subagents', 'tools', 'audit', 'context', 'settings'];

test('each HTML document owns one stylesheet and works as a static navigation target', async () => {
  for (const [dir, ext] of [['html', 'html'], ['css', 'css']]) {
    assert.deepEqual((await readdir(new URL(`${dir}/`, root))).filter((name) => name.endsWith(`.${ext}`)).sort(),
      [...pages, 'sidebar'].map((page) => `${page}.${ext}`).sort());
  }
  for (const page of pages) {
    const html = await readFile(new URL(`html/${page}.html`, root), 'utf8');
    const css = await readFile(new URL(`css/${page}.css`, root), 'utf8');
    assert.match(html, /<main\b[^>]*>[\s\S]*<h1\b[^>]*>[^<]+<\/h1>[\s\S]*<section\b/);
    assert.doesNotMatch(html, /<style\b|\s(?:style|on[\w-]+)\s*=|javascript\s*:/i);
    assert.ok(css.startsWith('@import "sidebar.css";'));
    assert.doesNotMatch(css.slice('@import "sidebar.css";'.length), /@import\b|url\s*\(/i);
    assert.doesNotMatch(css, /\.(?:sidebar|brand|logo|nav-label|new-link|nav-icon|history-nav|task-nav)\b|\bnav\s*(?:\{|a\b)/);
    const stylesheets = [...html.matchAll(/<link\b[^>]*\brel="stylesheet"[^>]*>/gi)].map(([tag]) => tag);
    assert.deepEqual(stylesheets, [`<link rel="stylesheet" href="../css/${page}.css">`]);
    assert.equal((html.match(/<script\b/gi) ?? []).length, 1);
    assert.match(html, /<script type="module" src="\.\.\/js\/app\.js"><\/script>/);
    assert.match(html, /<div id="sidebar-slot"><\/div>\s*<main\b/);
    assert.doesNotMatch(html, /class="sidebar\b|id="session-history"/);
    assert.match(html, /示例模式/);
  }
});

test('one inert sidebar fragment owns the navigation markup and link targets', async () => {
  const html = await readFile(new URL('html/sidebar.html', root), 'utf8');
  const css = await readFile(new URL('css/sidebar.css', root), 'utf8');
  assert.match(html, /^<aside class="sidebar">[\s\S]*<\/aside>\s*$/);
  assert.doesNotMatch(html, /<!doctype\b|<(?:html|head|body|main|style|script|link)\b|\s(?:style|on[\w-]+|aria-current)\s*=|javascript\s*:/i);
  assert.match(html, /class="brand"[^>]*>.*<\/div>\s*<a class="button primary new-link" href="sessions.html#new-session">.*<\/a>\s*<h2[^>]*>工作区<\/h2>\s*<nav\b[^>]*id="session-history"><\/nav>\s*<nav class="task-nav"/);
  const nav = html.match(/<nav class="task-nav"[^>]*>([\s\S]+?)<\/nav>/)[1];
  assert.deepEqual([...nav.matchAll(/<a href="([^"]+)"/g)].map(([, href]) => href),
    pages.filter((name) => !['index', 'settings'].includes(name)).map((name) => `${name}.html`));
  assert.equal((html.match(/href="settings\.html"/g) ?? []).length, 1);
  assert.match(html, /<\/nav>\s*<a class="sidebar-foot" href="settings\.html"[^>]*>.*<\/a>\s*<\/aside>/);
  assert.doesNotMatch(css, /@import\b|url\s*\(/i);
});