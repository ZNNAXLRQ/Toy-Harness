const fragment = new URL('../html/sidebar.html', import.meta.url);

export async function mountSidebar(slot) {
  const response = await fetch(fragment, { mode: 'same-origin', redirect: 'error' });
  if (!response.ok) throw new Error(`无法加载侧栏（HTTP ${response.status}）。`);

  const template = document.createElement('template');
  template.innerHTML = await response.text();
  const sidebar = template.content.firstElementChild;
  if (!sidebar?.matches('aside.sidebar')) throw new Error('侧栏片段格式无效。');

  for (const link of sidebar.querySelectorAll('.task-nav a, .sidebar-foot')) {
    const target = new URL(link.getAttribute('href'), fragment);
    if (target.pathname === location.pathname) link.setAttribute('aria-current', 'page');
  }
  slot.replaceWith(sidebar);
  return sidebar;
}