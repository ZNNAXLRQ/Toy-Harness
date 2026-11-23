import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../../static/', import.meta.url);
const pages = ['index', 'approvals', 'sessions', 'checkpoints', 'subagents', 'tools', 'audit', 'context', 'settings'];

test('each HTML document owns one stylesheet and works as a static navigation target', async () => {
  for (const [dir, ext] of [['html', 'html'], ['css', 'css']]) {
    assert.deepEqual((await readdir(new URL(`${dir}/`, root))).filter((name) => name.endsWith(`.${ext}`)).sort(),
      pages.map((page) => `${page}.${ext}`).sort());
  }
  for (const page of pages) {
    const html = await readFile(new URL(`html/${page}.html`, root), 'utf8');
    const css = await readFile(new URL(`css/${page}.css`, root), 'utf8');
    assert.match(html, /<main\b[^>]*>[\s\S]*<h1\b[^>]*>[^<]+<\/h1>[\s\S]*<section\b/);
    assert.doesNotMatch(html, /<style\b|\s(?:style|on[\w-]+)\s*=|javascript\s*:/i);
    assert.doesNotMatch(css, /@import\b|url\s*\(/i);
    const stylesheets = [...html.matchAll(/<link\b[^>]*\brel="stylesheet"[^>]*>/gi)].map(([tag]) => tag);
    assert.deepEqual(stylesheets, [`<link rel="stylesheet" href="../css/${page}.css">`]);
    assert.equal((html.match(/<script\b/gi) ?? []).length, 1);
    assert.match(html, /<script type="module" src="\.\.\/js\/app\.js"><\/script>/);
    assert.match(html, /<nav\b[^>]*id="session-history"><\/nav>/);
    assert.match(html, /class="button primary new-link" href="sessions.html#new-session"/);
    const nav = html.match(/<nav class="task-nav"[^>]*>([\s\S]+?)<\/nav>/)[1];
    const taskPages = pages.filter((name) => !['index', 'settings'].includes(name));
    assert.deepEqual([...nav.matchAll(/<a href="([^"#]+)"/g)].map(([, href]) => href), taskPages.map((name) => `${name}.html`));
    assert.equal((nav.match(/aria-current=/g) ?? []).length, Number(taskPages.includes(page)));
    if (taskPages.includes(page)) assert.ok(nav.includes(`href="${page}.html" aria-current="page"`));
    assert.equal((html.match(/href="settings\.html"/g) ?? []).length, 1);
    assert.match(html, /<a class="sidebar-foot" href="settings\.html"/);
  }
});