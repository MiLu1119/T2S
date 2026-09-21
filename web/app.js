const form = document.querySelector('#query-form');
const question = document.querySelector('#question');
const submit = document.querySelector('#submit');
const emptyState = document.querySelector('#empty-state');
const result = document.querySelector('#result');
const errorBox = document.querySelector('#error');
let currentRequestId = null;
let runningRequestId = null;
let currentThreadId = localStorage.getItem('verisql_thread_id');
let currentDataSourceId = localStorage.getItem('verisql_data_source_id');
let dataSources = [];
let currentUser = null;
let managedUsers = [];
let editingSourceId = null;
let resultPage = 1;
let resultTotalPages = 1;
let resultPageSize = 50;
let currentResultColumns = [];
let currentResultRows = [];
const executionFlow = document.querySelector('#execution-flow');
const flowSteps = document.querySelector('#flow-steps');
const flowMetrics = document.querySelector('#flow-metrics');
const traceRail = document.querySelector('#trace-rail');
const traceEmpty = document.querySelector('#trace-empty');
const sidebar = document.querySelector('#workspace-sidebar');
const mobileScrim = document.querySelector('#mobile-scrim');
const managementDrawers = [...document.querySelectorAll('.management-drawer')];
let adminTraceItems = [];

function updateScrim() {
  const panelOpen = sidebar.classList.contains('open') || traceRail.classList.contains('open');
  mobileScrim.classList.toggle('hidden', !panelOpen);
}

function setSidebarOpen(open) {
  sidebar.classList.toggle('open', open);
  if (open) traceRail.classList.remove('open');
  updateScrim();
}

function setTraceOpen(open) {
  traceRail.classList.toggle('open', open);
  if (open) sidebar.classList.remove('open');
  updateScrim();
}

function closeResponsivePanels() {
  sidebar.classList.remove('open');
  traceRail.classList.remove('open');
  updateScrim();
}

function closeManagementDrawers(except = null) {
  managementDrawers.forEach(drawer => drawer.classList.toggle('hidden', drawer !== except));
}

function autoResizeQuestion() {
  question.style.height = 'auto';
  question.style.height = `${Math.min(Math.max(question.scrollHeight, 96), 280)}px`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    const config = await response.json();
    document.querySelector('#status-dot').classList.add('ready');
    document.querySelector('#runtime-label').textContent = `${config.provider.toUpperCase()} · ${config.model} · ${config.schema_context_mode.toUpperCase()}${config.schema_embedding_enabled ? ' · EMB' : ''}${config.etc_enabled ? ' · ETC ON' : ''}`;
  } catch (_) {
    document.querySelector('#runtime-label').textContent = '服务不可用';
  }
}

async function loadDataSources() {
  const response = await fetch('/api/data-sources');
  if (!response.ok) throw new Error('无法读取数据源');
  dataSources = (await response.json()).items;
  const select = document.querySelector('#data-source-select');
  select.innerHTML = dataSources.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
  if (!dataSources.some(item => item.id === currentDataSourceId)) currentDataSourceId = dataSources[0]?.id || null;
  if (currentDataSourceId) select.value = currentDataSourceId;
  updateSourceSummary();
}

function updateSourceSummary() {
  const source = dataSources.find(item => item.id === currentDataSourceId);
  document.querySelector('#active-source-label').textContent = source?.name || '未选择数据源';
  document.querySelector('#source-kind').textContent = source ? `${source.kind.toUpperCase()} · Read only` : '未选择';
  document.querySelector('#source-summary').textContent = source
    ? (source.kind === 'sqlite'
      ? (source.config.original_filename || source.config.registered_file || 'SQLite 数据库')
      : source.kind === 'postgresql'
        ? source.config.dsn
        : `${source.config.user}@${source.config.host}:${source.config.port}/${source.config.database}`)
    : '请添加一个数据源后开始查询';
  const health = document.querySelector('#source-health');
  health.className = `source-health ${source?.health_status || 'unknown'}`;
  health.textContent = source
    ? `${source.enabled ? '已启用' : '已停用'} · ${source.health_status === 'healthy' ? '连接正常' : source.health_status === 'unhealthy' ? '连接异常' : '尚未检查'}${source.table_count != null ? ` · ${source.table_count} 张表` : ''}${source.last_latency_ms != null ? ` · ${source.last_latency_ms} ms` : ''}`
    : '';
  const canDelete = currentUser?.permissions?.includes('data_sources.delete') && source?.is_owner;
  document.querySelector('#delete-source').classList.toggle('hidden', !canDelete);
  const canShare = currentUser?.permissions?.includes('data_sources.share') && source?.is_owner;
  document.querySelector('#show-source-grants').classList.toggle('hidden', !canShare);
  if (!canShare) document.querySelector('#source-grants-form').classList.add('hidden');
  const canManage = currentUser?.permissions?.includes('data_sources.update') && source?.is_owner;
  document.querySelector('.source-management').classList.toggle('hidden', !canManage);
  document.querySelector('#show-catalog').classList.toggle('hidden', !canManage || source?.kind !== 'sqlite');
  document.querySelector('#toggle-source').textContent = source?.enabled ? '停用' : '启用';
}

async function bootstrap() {
  const response = await fetch('/api/auth/me');
  if (response.status === 401) {
    document.querySelector('#login-screen').classList.remove('hidden');
    return;
  }
  currentUser = await response.json();
  document.querySelector('#current-user').textContent = `${currentUser.username} · ${currentUser.role}`;
  document.querySelector('.user-avatar').textContent = currentUser.username.slice(0, 1).toUpperCase();
  document.querySelector('#show-user-form').classList.toggle('hidden', !currentUser.permissions.includes('users.read'));
  document.querySelector('#show-admin-center').classList.toggle('hidden', !currentUser.permissions.includes('audit.read'));
  document.querySelector('#mobile-user-form').classList.toggle('hidden', !currentUser.permissions.includes('users.read'));
  document.querySelector('#mobile-admin-center').classList.toggle('hidden', !currentUser.permissions.includes('audit.read'));
  document.querySelector('#show-source-form').classList.toggle('hidden', !currentUser.permissions.includes('data_sources.create'));
  document.querySelector('#login-screen').classList.add('hidden');
  await Promise.all([loadConfig(), loadDataSources()]);
  await loadSessions();
}

function renderTable(columns, rows) {
  if (!rows.length) return '<p>查询成功，没有匹配的数据。</p>';
  const head = columns.map(col => `<th>${escapeHtml(col)}</th>`).join('');
  const body = rows.map(row => `<tr>${row.map(cell => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderTrace(data) {
  const trace = data.generation_trace || {};
  const usage = trace.token_usage || {};
  const tokenText = usage.total_tokens > 0
    ? `${usage.total_tokens}（输入 ${usage.prompt_tokens || 0} / 输出 ${usage.completion_tokens || 0}）`
    : 'API 未返回';
  const items = [
    ['总耗时', `${data.latency_ms} ms`],
    ['模型调用', `${trace.model_calls ?? 0} 次`],
    ['Token 用量', tokenText],
    ['Trace ID', data.trace_id || 'Tracing 未启用'],
    ['Redis 会话', data.checkpoint_hit ? '命中已有会话' : '新会话'],
    ['重试次数', data.retry_count],
    ['动态检索', (data.retrieval_events || []).length ? `触发 ${data.retrieval_events.length} 次` : '未触发'],
    ['Token Logprobs', trace.logprobs_available ? '可用' : '不可用 / Mock'],
    ['ETC 状态', trace.etc_triggered ? `触发于 token ${trace.trigger_index}` : '未触发'],
    ['AST 校验', data.ast_validation?.is_valid ? `通过 · ${(data.ast_validation.used_tables || []).join(', ') || '无表查询'}` : (data.ast_validation?.error_type || '未运行')],
    ['置信度', data.confidence ? `${Math.round(data.confidence.score * 100)}% · ${data.confidence.level}` : '历史记录未保存'],
    ['成本检查', data.cost_check?.available === false ? 'EXPLAIN 不可用' : `${data.cost_check?.level || '未运行'}${data.cost_check?.full_scans ? ` · ${data.cost_check.full_scans} 次全表扫描` : ''}`],
    ['安全与执行错误', (data.errors || []).length ? `${data.errors.length} 条` : '无'],
    ['最终状态', data.status],
    ['结果截断', data.truncated ? '是，已达到返回上限' : '否'],
  ];
  return items.map(([label, value]) => `<div class="trace-item"><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></div>`).join('');
}

function resetExecutionFlow() {
  flowSteps.innerHTML = '';
  flowMetrics.innerHTML = '';
  document.querySelector('#flow-total').textContent = '运行中';
  traceEmpty.classList.add('hidden');
  executionFlow.classList.remove('hidden');
}

function appendTimelineStep(step) {
  const item = document.createElement('li');
  item.className = 'flow-step';
  item.dataset.node = step.node || '';
  item.innerHTML = `<span class="step-index">${escapeHtml(step.sequence)}</span><span class="step-copy"><b>${escapeHtml(step.message)}</b><small>${escapeHtml(step.node)}</small></span><span class="step-duration">${escapeHtml(step.duration_ms)} ms</span>`;
  flowSteps.appendChild(item);
  item.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function renderFlowSummary(data) {
  document.querySelector('#flow-total').textContent = `完成 · ${data.latency_ms} ms`;
  const trace = data.generation_trace || {};
  const usage = trace.token_usage || {};
  const metrics = [
    ['模型调用', `${trace.model_calls ?? 0} 次`],
    ['Token', usage.total_tokens > 0 ? usage.total_tokens : '未返回'],
    ['重试', `${data.retry_count} 次`],
    ['RAG 检索', `${(data.retrieval_events || []).length} 次`],
    ['AST', data.ast_validation?.is_valid ? '通过' : '未通过'],
    ['Redis', data.checkpoint_hit ? '命中' : '新会话'],
    ['Trace ID', data.trace_id || '未启用'],
  ];
  flowMetrics.innerHTML = metrics.map(([label, value]) => `<div><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></div>`).join('');
}

function renderResult(data) {
  emptyState.classList.add('hidden');
  document.querySelector('#answer').textContent = data.answer;
  document.querySelector('#sql').textContent = data.sql;
  document.querySelector('#result-status').textContent = data.status === 'success'
    ? '执行成功'
    : data.status === 'cancelled' ? '已取消' : '需要人工处理';
  const resultStatus = document.querySelector('#result-status');
  resultStatus.className = `status-pill ${data.status === 'success' ? '' : data.status === 'cancelled' ? 'cancelled' : 'failed'}`;
  const warning = document.querySelector('#confidence-warning');
  const confidence = data.confidence || {};
  const cost = data.cost_check || {};
  const notices = [];
  if (confidence.low_confidence) notices.push(`低置信度 ${Math.round(confidence.score * 100)}%：${(confidence.reasons || []).join('；')}`);
  if ((cost.warnings || []).length) notices.push(`成本提示：${cost.warnings.join('；')}`);
  warning.innerHTML = notices.map(item => `<p>${escapeHtml(item)}</p>`).join('');
  warning.classList.toggle('hidden', !notices.length);
  currentResultColumns = data.columns || [];
  currentResultRows = (data.rows || []).slice(0, resultPageSize);
  document.querySelector('#table-wrap').innerHTML = renderTable(currentResultColumns, currentResultRows);
  document.querySelector('#trace').innerHTML = renderTrace(data);
  renderFlowSummary(data);
  currentRequestId = data.request_id;
  currentThreadId = data.thread_id;
  localStorage.setItem('verisql_thread_id', currentThreadId);
  document.querySelector('#feedback-comment').value = '';
  document.querySelector('#feedback-status').textContent = '';
  document.querySelectorAll('.feedback-button').forEach(button => button.classList.remove('selected'));
  result.classList.remove('hidden');
  document.querySelector('#result-tools').classList.remove('hidden');
  document.querySelector('#chart-panel').classList.add('hidden');
  resultPage = 1;
  resultTotalPages = Math.max(1, Math.ceil((data.rows || []).length / resultPageSize));
  updatePaginationControls();
  configureChartColumns();
  loadResultPage(1).catch(() => {});
}

function updatePaginationControls(totalRows = null) {
  document.querySelector('#page-label').textContent = `第 ${resultPage} / ${resultTotalPages} 页${totalRows == null ? '' : ` · ${totalRows} 行`}`;
  document.querySelector('#page-prev').disabled = resultPage <= 1;
  document.querySelector('#page-next').disabled = resultPage >= resultTotalPages;
}

async function loadResultPage(page) {
  if (!currentRequestId) return;
  const response = await fetch(`/api/query-results/${encodeURIComponent(currentRequestId)}?page=${page}&page_size=${resultPageSize}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '结果已过期');
  resultPage = data.page;
  resultTotalPages = data.total_pages;
  currentResultColumns = data.columns;
  currentResultRows = data.rows;
  document.querySelector('#table-wrap').innerHTML = renderTable(data.columns, data.rows);
  updatePaginationControls(data.total_rows);
  configureChartColumns();
  if (!document.querySelector('#chart-panel').classList.contains('hidden')) renderChart();
}

function configureChartColumns() {
  const category = document.querySelector('#chart-category');
  const value = document.querySelector('#chart-value');
  category.innerHTML = currentResultColumns.map((column, index) => `<option value="${index}">${escapeHtml(column)}</option>`).join('');
  const numericIndexes = currentResultColumns.map((_, index) => index).filter(index => currentResultRows.some(row => Number.isFinite(Number(row[index])) && row[index] !== null && row[index] !== ''));
  value.innerHTML = (numericIndexes.length ? numericIndexes : currentResultColumns.map((_, index) => index)).map(index => `<option value="${index}">${escapeHtml(currentResultColumns[index])}</option>`).join('');
  if (numericIndexes.length) value.value = String(numericIndexes[0]);
}

function chartSeries() {
  const categoryIndex = Number(document.querySelector('#chart-category').value || 0);
  const valueIndex = Number(document.querySelector('#chart-value').value || 0);
  return currentResultRows.slice(0, 200).map(row => ({label: String(row[categoryIndex] ?? ''), value: Number(row[valueIndex])})).filter(item => Number.isFinite(item.value));
}

function renderChart() {
  const series = chartSeries();
  const canvas = document.querySelector('#chart-canvas');
  if (!series.length) { canvas.innerHTML = '<p>当前页没有可绘制的数值数据。</p>'; return; }
  const colors = ['#6ee7f2','#74e0a3','#ffbd66','#c4a7ff','#ff8a8a','#7ba7ff','#f59edc','#91a0b3'];
  const type = document.querySelector('#chart-type').value;
  if (type === 'pie') {
    const positive = series.map(item => ({...item, value: Math.max(0, item.value)}));
    const total = positive.reduce((sum, item) => sum + item.value, 0);
    if (!total) { canvas.innerHTML = '<p>饼图需要大于零的数值。</p>'; return; }
    let angle = 0;
    const stops = positive.map((item, index) => { const start = angle; angle += item.value / total * 360; return `${colors[index % colors.length]} ${start}deg ${angle}deg`; });
    const legend = positive.map((item, index) => `<span><i class="legend-swatch" style="background:${colors[index % colors.length]}"></i>${escapeHtml(item.label)} · ${escapeHtml(item.value)}</span>`).join('');
    canvas.innerHTML = `<div class="pie-layout"><div class="pie-circle" style="background:conic-gradient(${stops.join(',')})"></div><div class="chart-legend">${legend}</div></div>`;
    return;
  }
  const width = 760, height = 300, left = 52, bottom = 48, top = 18;
  const values = series.map(item => item.value);
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const span = max - min || 1;
  const xStep = (width - left - 20) / Math.max(1, series.length);
  const y = value => top + (max - value) / span * (height - top - bottom);
  const zeroY = y(0);
  const labels = series.map((item, index) => `<text x="${left + xStep * (index + .5)}" y="${height - 20}" text-anchor="middle" fill="#91a0b3" font-size="10">${escapeHtml(item.label.slice(0, 12))}</text>`).join('');
  let marks;
  if (type === 'line') {
    const points = series.map((item, index) => `${left + xStep * (index + .5)},${y(item.value)}`).join(' ');
    marks = `<polyline points="${points}" fill="none" stroke="#6ee7f2" stroke-width="3"/>` + series.map((item, index) => `<circle cx="${left + xStep * (index + .5)}" cy="${y(item.value)}" r="4" fill="#74e0a3"><title>${escapeHtml(item.label)}: ${item.value}</title></circle>`).join('');
  } else {
    const barWidth = Math.max(4, xStep * .65);
    marks = series.map((item, index) => { const itemY = y(item.value); return `<rect x="${left + xStep * (index + .5) - barWidth / 2}" y="${Math.min(itemY, zeroY)}" width="${barWidth}" height="${Math.max(1, Math.abs(zeroY - itemY))}" fill="${colors[index % colors.length]}"><title>${escapeHtml(item.label)}: ${item.value}</title></rect>`; }).join('');
  }
  canvas.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img"><line x1="${left}" y1="${zeroY}" x2="${width - 10}" y2="${zeroY}" stroke="#536274"/><text x="8" y="${top + 8}" fill="#91a0b3" font-size="11">${escapeHtml(max)}</text><text x="8" y="${height - bottom}" fill="#91a0b3" font-size="11">${escapeHtml(min)}</text>${marks}${labels}</svg>`;
}

async function loadSessions() {
  if (!currentDataSourceId) return;
  const response = await fetch(`/api/workbench/sessions?data_source_id=${encodeURIComponent(currentDataSourceId)}`);
  const data = await response.json();
  if (!response.ok) return;
  const list = document.querySelector('#session-list');
  list.innerHTML = data.items.length ? data.items.map(item => `<div class="session-item ${item.thread_id === currentThreadId ? 'active' : ''}" data-thread-id="${escapeHtml(item.thread_id)}"><div class="session-item-head"><b>${escapeHtml(item.title)}</b><span class="session-item-actions"><button type="button" data-session-action="rename">改名</button><button type="button" data-session-action="delete">删除</button></span></div><small>${item.query_count} 次查询 · ${new Date(item.updated_at).toLocaleString()}</small></div>`).join('') : '<small>暂无历史会话</small>';
}

async function openSession(threadId) {
  currentThreadId = threadId;
  localStorage.setItem('verisql_thread_id', threadId);
  await loadSessions();
  const response = await fetch(`/api/workbench/sessions/${encodeURIComponent(threadId)}/queries?data_source_id=${encodeURIComponent(currentDataSourceId)}`);
  const data = await response.json();
  const list = document.querySelector('#session-query-list');
  if (!response.ok) { list.classList.add('hidden'); return; }
  list.innerHTML = data.items.map(item => `<button type="button" class="query-history-item" data-request-id="${escapeHtml(item.request_id)}"><b>${escapeHtml(item.question)}</b><small>${escapeHtml(item.status)} · ${item.row_count} 行 · ${item.latency_ms} ms</small></button>`).join('');
  list.classList.remove('hidden');
  setSidebarOpen(false);
}

async function openHistoricalResult(requestId) {
  const response = await fetch(`/api/query-results/${encodeURIComponent(requestId)}?page=1&page_size=${resultPageSize}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '结果已过期');
  currentThreadId = data.thread_id;
  currentDataSourceId = data.data_source_id;
  const view = {...data, request_id: requestId, generation_trace: {}, retrieval_events: [], ast_validation: {}, errors: [], retry_count: 0, trace_id: '', checkpoint_hit: true, confidence: {}, cost_check: {}};
  renderResult(view);
  question.value = data.question;
  autoResizeQuestion();
}

async function streamQuery(payload) {
  const response = await fetch('/api/query/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.detail || '请求失败');
  }
  if (!response.body) throw new Error('浏览器不支持流式响应');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const status = document.querySelector('#stream-status');
  let buffer = '';
  let finalData = null;
  let streamError = null;

  function consumeBlock(block) {
    if (!block || block.startsWith(':')) return;
    let eventName = 'message';
    const dataLines = [];
    block.split('\n').forEach(line => {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return;
    const data = JSON.parse(dataLines.join('\n'));
    if (eventName === 'start') {
      runningRequestId = data.request_id;
      document.querySelector('#cancel-query').classList.remove('hidden');
      currentThreadId = data.thread_id;
      localStorage.setItem('verisql_thread_id', currentThreadId);
      status.textContent = data.message;
    } else if (eventName === 'progress') {
      status.textContent = data.message;
      submit.textContent = data.message;
      appendTimelineStep(data);
    } else if (eventName === 'final') {
      finalData = data;
    } else if (eventName === 'error') {
      streamError = data.detail || 'Agent 执行失败';
    }
  }

  while (true) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), {stream: !done}).replace(/\r\n/g, '\n');
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() || '';
    blocks.forEach(consumeBlock);
    if (done) break;
  }
  if (buffer.trim()) consumeBlock(buffer.trim());
  if (streamError) throw new Error(streamError);
  if (!finalData) throw new Error('流式响应提前结束');
  return finalData;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  errorBox.classList.add('hidden');
  result.classList.add('hidden');
  submit.disabled = true;
  submit.textContent = 'Agent 运行中…';
  const streamStatus = document.querySelector('#stream-status');
  streamStatus.textContent = '正在建立流式连接…';
  streamStatus.classList.remove('hidden');
  resetExecutionFlow();
  try {
    if (!currentDataSourceId) throw new Error('请先添加并选择数据源');
    const data = await streamQuery({question: question.value.trim(), thread_id: currentThreadId, data_source_id: currentDataSourceId});
    renderResult(data);
    await loadSessions();
    streamStatus.textContent = '运行完成';
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove('hidden');
  } finally {
    runningRequestId = null;
    document.querySelector('#cancel-query').classList.add('hidden');
    submit.disabled = false;
    submit.innerHTML = '运行 Agent <span>→</span>';
    window.setTimeout(() => streamStatus.classList.add('hidden'), 1200);
  }
});

document.querySelector('#cancel-query').addEventListener('click', async () => {
  if (!runningRequestId) return;
  const button = document.querySelector('#cancel-query');
  button.disabled = true;
  button.textContent = '正在取消…';
  const response = await fetch(`/api/requests/${encodeURIComponent(runningRequestId)}/cancel`, {method: 'POST'});
  const data = await response.json();
  document.querySelector('#stream-status').textContent = response.ok ? '已请求取消，正在安全停止…' : (data.detail || '取消失败');
  button.disabled = false;
  button.textContent = '取消请求';
});

document.querySelector('#new-session').addEventListener('click', async () => {
  currentThreadId = null;
  currentRequestId = null;
  localStorage.removeItem('verisql_thread_id');
  question.value = '';
  autoResizeQuestion();
  result.classList.add('hidden');
  executionFlow.classList.add('hidden');
  traceEmpty.classList.remove('hidden');
  flowSteps.innerHTML = '';
  flowMetrics.innerHTML = '';
  emptyState.classList.remove('hidden');
  document.querySelector('#session-query-list').classList.add('hidden');
  await loadSessions();
  question.focus();
});

document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const response = await fetch('/api/auth/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: document.querySelector('#login-username').value, password: document.querySelector('#login-password').value}),
  });
  const data = await response.json();
  if (!response.ok) { document.querySelector('#login-error').textContent = data.detail || '登录失败'; return; }
  document.querySelector('#login-password').value = '';
  await bootstrap();
});

document.querySelector('#logout').addEventListener('click', async () => {
  await fetch('/api/auth/logout', {method: 'POST'});
  location.reload();
});

async function loadUsers() {
  const response = await fetch('/api/users');
  if (!response.ok) throw new Error('无法读取用户列表');
  managedUsers = (await response.json()).items;
  document.querySelector('#user-list').innerHTML = managedUsers.map(user => `
    <div class="user-row" data-user-id="${escapeHtml(user.id)}">
      <span><b>${escapeHtml(user.username)}</b><small>${escapeHtml(user.role)} · ${user.active ? '启用' : '停用'}</small></span>
      ${user.id === currentUser.id ? '<em>当前用户</em>' : `<button type="button" data-action="toggle">${user.active ? '停用' : '启用'}</button><button type="button" data-action="reset">重置密码</button><button type="button" data-action="delete">删除</button>`}
    </div>`).join('');
  document.querySelector('#grant-user').innerHTML = managedUsers.filter(user => user.id !== currentUser.id && user.active).map(user => `<option value="${escapeHtml(user.id)}">${escapeHtml(user.username)} · ${escapeHtml(user.role)}</option>`).join('');
}

document.querySelector('#show-user-form').addEventListener('click', async () => {
  closeManagementDrawers(document.querySelector('#user-form'));
  try { await loadUsers(); } catch (error) { document.querySelector('#user-status').textContent = error.message; }
});
document.querySelector('#mobile-user-form').addEventListener('click', async () => {
  setSidebarOpen(false);
  closeManagementDrawers(document.querySelector('#user-form'));
  try { await loadUsers(); } catch (error) { document.querySelector('#user-status').textContent = error.message; }
});
document.querySelector('#cancel-user').addEventListener('click', () => document.querySelector('#user-form').classList.add('hidden'));
document.querySelector('#create-user-form').addEventListener('submit', async event => {
  event.preventDefault();
  const status = document.querySelector('#user-status');
  const response = await fetch('/api/users', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: document.querySelector('#new-username').value.trim(), password: document.querySelector('#new-password').value, role: document.querySelector('#new-role').value}),
  });
  const data = await response.json();
  status.textContent = response.ok ? `用户 ${data.username} 已创建` : (data.detail || '创建失败');
  if (response.ok) { document.querySelector('#new-password').value = ''; await loadUsers(); }
});

document.querySelector('#user-list').addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const userId = button.closest('[data-user-id]').dataset.userId;
  const user = managedUsers.find(item => item.id === userId);
  let response;
  if (button.dataset.action === 'toggle') {
    response = await fetch(`/api/users/${encodeURIComponent(userId)}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({active: !user.active})});
  } else if (button.dataset.action === 'reset') {
    const password = window.prompt(`为 ${user.username} 设置新密码（至少 8 位）`);
    if (!password) return;
    response = await fetch(`/api/users/${encodeURIComponent(userId)}/reset-password`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password})});
  } else {
    if (!window.confirm(`确认删除用户“${user.username}”？`)) return;
    response = await fetch(`/api/users/${encodeURIComponent(userId)}`, {method: 'DELETE'});
  }
  const data = await response.json();
  document.querySelector('#user-status').textContent = response.ok ? '操作成功' : (data.detail || '操作失败');
  if (response.ok) await loadUsers();
});

document.querySelector('#data-source-select').addEventListener('change', async event => {
  currentDataSourceId = event.target.value;
  localStorage.setItem('verisql_data_source_id', currentDataSourceId);
  currentThreadId = null;
  localStorage.removeItem('verisql_thread_id');
  updateSourceSummary();
  document.querySelector('#session-query-list').classList.add('hidden');
  await loadSessions();
  setSidebarOpen(false);
});

document.querySelector('#delete-source').addEventListener('click', async () => {
  const source = dataSources.find(item => item.id === currentDataSourceId);
  if (!source || !window.confirm(`确认删除数据源“${source.name}”？`)) return;
  const response = await fetch(`/api/data-sources/${encodeURIComponent(source.id)}`, {method: 'DELETE'});
  if (!response.ok) { const data = await response.json(); alert(data.detail || '删除失败'); return; }
  currentDataSourceId = null;
  localStorage.removeItem('verisql_data_source_id');
  await loadDataSources();
});

async function loadSourceGrants() {
  const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/grants`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '无法读取授权');
  document.querySelector('#source-grants-list').innerHTML = data.items.length ? data.items.map(grant => `<div class="user-row"><span><b>${escapeHtml(grant.username)}</b><small>${escapeHtml(grant.access_level)}</small></span><button type="button" data-revoke-user="${escapeHtml(grant.user_id)}">撤销</button></div>`).join('') : '<small>尚未共享给其他用户</small>';
}

document.querySelector('#show-source-grants').addEventListener('click', async () => {
  closeManagementDrawers(document.querySelector('#source-grants-form'));
  try { await Promise.all([loadUsers(), loadSourceGrants()]); } catch (error) { document.querySelector('#grant-status').textContent = error.message; }
});
document.querySelector('#cancel-grants').addEventListener('click', () => document.querySelector('#source-grants-form').classList.add('hidden'));
document.querySelector('#save-grant').addEventListener('click', async () => {
  const userId = document.querySelector('#grant-user').value;
  if (!userId) { document.querySelector('#grant-status').textContent = '没有可授权用户'; return; }
  const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/grants`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: userId, access_level: document.querySelector('#grant-level').value})});
  const data = await response.json();
  document.querySelector('#grant-status').textContent = response.ok ? '授权已保存' : (data.detail || '授权失败');
  if (response.ok) await loadSourceGrants();
});
document.querySelector('#source-grants-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-revoke-user]');
  if (!button) return;
  const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/grants/${encodeURIComponent(button.dataset.revokeUser)}`, {method: 'DELETE'});
  if (response.ok) await loadSourceGrants();
});

function setSourceFormKind(kind, editing = false) {
  document.querySelector('#sqlite-fields').classList.toggle('hidden', kind !== 'sqlite' || editing);
  document.querySelector('#mysql-fields').classList.toggle('hidden', kind !== 'mysql');
  document.querySelector('#postgresql-fields').classList.toggle('hidden', kind !== 'postgresql');
  document.querySelector('#sqlite-file').required = kind === 'sqlite' && !editing;
  document.querySelector('#source-type').disabled = editing;
  document.querySelector('#save-source').textContent = editing ? '验证并保存' : (kind === 'sqlite' ? '上传并保存' : '测试连接并保存');
}

document.querySelector('#show-source-form').addEventListener('click', () => {
  editingSourceId = null;
  document.querySelector('#source-form').reset();
  document.querySelector('#source-form-title').textContent = '添加数据源';
  document.querySelector('#source-type').value = 'sqlite';
  setSourceFormKind('sqlite');
  closeManagementDrawers(document.querySelector('#source-form'));
});
document.querySelector('#cancel-source').addEventListener('click', () => document.querySelector('#source-form').classList.add('hidden'));
document.querySelector('#source-type').addEventListener('change', event => {
  setSourceFormKind(event.target.value);
});

document.querySelector('#edit-source').addEventListener('click', () => {
  const source = dataSources.find(item => item.id === currentDataSourceId);
  if (!source) return;
  editingSourceId = source.id;
  document.querySelector('#source-form').reset();
  document.querySelector('#source-form-title').textContent = '编辑数据源';
  document.querySelector('#source-name').value = source.name;
  document.querySelector('#source-description').value = source.description || '';
  document.querySelector('#source-type').value = source.kind;
  if (source.kind === 'mysql') {
    document.querySelector('#mysql-host').value = source.config.host || '';
    document.querySelector('#mysql-port').value = source.config.port || 3306;
    document.querySelector('#mysql-database').value = source.config.database || '';
    document.querySelector('#mysql-user').value = source.config.user || '';
    document.querySelector('#mysql-ssl').checked = Boolean(source.config.ssl);
    document.querySelector('#mysql-password').placeholder = source.has_secret ? '留空表示保持原密码' : '请输入密码';
  } else if (source.kind === 'postgresql') {
    document.querySelector('#postgresql-dsn').value = source.config.dsn || '';
    document.querySelector('#postgresql-schemas').value = (source.config.schemas || ['public']).join(',');
    document.querySelector('#postgresql-password').placeholder = source.has_secret ? '留空表示保持原密码' : '请输入密码';
  }
  setSourceFormKind(source.kind, true);
  closeManagementDrawers(document.querySelector('#source-form'));
});

document.querySelector('#test-source').addEventListener('click', async () => {
  const status = document.querySelector('#source-health');
  status.textContent = '正在检查连接与 Schema…';
  const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/health`, {method: 'POST'});
  const data = await response.json();
  status.textContent = response.ok ? `连接正常，发现 ${data.table_count} 张表` : (data.detail || '连接失败');
  await loadDataSources();
});

function renderCatalog(catalog) {
  const tables = Object.entries(catalog.tables || {});
  document.querySelector('#catalog-list').innerHTML = tables.map(([tableName, table]) => `
    <details class="catalog-table" data-catalog-table="${escapeHtml(tableName)}">
      <summary><b>${escapeHtml(tableName)}</b><span>${escapeHtml(table.description_source || 'inferred')}</span></summary>
      <label>表业务描述<input data-table-description value="${escapeHtml(table.description || '')}" maxlength="500"></label>
      <div class="catalog-columns">${Object.entries(table.columns || {}).map(([columnName, column]) => `
        <label><span><code>${escapeHtml(columnName)}</code><small>${escapeHtml(column.data_type || '')}</small></span><input data-column-description="${escapeHtml(columnName)}" value="${escapeHtml(column.description || '')}" maxlength="500"></label>`).join('')}</div>
    </details>`).join('');
  document.querySelector('#catalog-status').textContent = `${tables.length} 张表 · ${catalog.inferred_relations?.length || 0} 条候选关系 · ${catalog.generation_source}`;
}

async function loadCatalog(syncIfMissing = true) {
  let response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/schema-catalog`);
  if (response.status === 404 && syncIfMissing) {
    response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/schema-catalog/sync`, {method: 'POST'});
    if (!response.ok) { const data = await response.json(); throw new Error(data.detail || 'Catalog 同步失败'); }
    return loadCatalog(false);
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Catalog 加载失败');
  renderCatalog(data);
}

document.querySelector('#show-catalog').addEventListener('click', async () => {
  closeManagementDrawers(document.querySelector('#catalog-drawer'));
  document.querySelector('#catalog-status').textContent = '正在读取语义目录…';
  try { await loadCatalog(); } catch (error) { document.querySelector('#catalog-status').textContent = error.message; }
});
document.querySelector('#cancel-catalog').addEventListener('click', () => document.querySelector('#catalog-drawer').classList.add('hidden'));
document.querySelector('#sync-catalog').addEventListener('click', async () => {
  const status = document.querySelector('#catalog-status');
  status.textContent = '正在同步数据库结构…';
  const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/schema-catalog/sync`, {method: 'POST'});
  const data = await response.json();
  if (!response.ok) { status.textContent = data.detail || '同步失败'; return; }
  await loadCatalog(false);
});
document.querySelector('#enrich-catalog').addEventListener('click', async () => {
  if (!window.confirm('确认将表名、字段名、类型和关系发送给当前模型生成业务描述？不会发送字段值。')) return;
  const status = document.querySelector('#catalog-status');
  const button = document.querySelector('#enrich-catalog');
  button.disabled = true; status.textContent = 'AI 正在分批生成语义描述…';
  try {
    const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/schema-catalog/enrich`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({table_names: []})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'AI 描述生成失败');
    await loadCatalog(false);
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
});
document.querySelector('#save-catalog').addEventListener('click', async () => {
  const tables = {};
  document.querySelectorAll('[data-catalog-table]').forEach(table => {
    const columns = {};
    table.querySelectorAll('[data-column-description]').forEach(input => { columns[input.dataset.columnDescription] = input.value.trim(); });
    tables[table.dataset.catalogTable] = {description: table.querySelector('[data-table-description]').value.trim(), columns};
  });
  const response = await fetch(`/api/data-sources/${encodeURIComponent(currentDataSourceId)}/schema-catalog/descriptions`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tables})});
  const data = await response.json();
  document.querySelector('#catalog-status').textContent = response.ok ? '语义描述已保存并应用到后续检索' : (data.detail || '保存失败');
});

document.querySelector('#toggle-source').addEventListener('click', async () => {
  const source = dataSources.find(item => item.id === currentDataSourceId);
  if (!source) return;
  const response = await fetch(`/api/data-sources/${encodeURIComponent(source.id)}/enabled`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: !source.enabled})});
  const data = await response.json();
  if (!response.ok) { document.querySelector('#source-status').textContent = data.detail || '操作失败'; return; }
  await loadDataSources();
});

document.querySelector('#source-form').addEventListener('submit', async event => {
  event.preventDefault();
  const kind = document.querySelector('#source-type').value;
  const status = document.querySelector('#source-status');
  status.textContent = kind === 'sqlite' ? '正在上传并检查数据库…' : '正在验证数据库连接…';
  let response;
  const description = document.querySelector('#source-description').value.trim();
  if (editingSourceId) {
    const payload = {name: document.querySelector('#source-name').value.trim(), description};
    if (kind === 'mysql') {
      payload.config = {host: document.querySelector('#mysql-host').value.trim(), port: Number(document.querySelector('#mysql-port').value), database: document.querySelector('#mysql-database').value.trim(), user: document.querySelector('#mysql-user').value.trim(), ssl: document.querySelector('#mysql-ssl').checked};
      const password = document.querySelector('#mysql-password').value;
      if (password) payload.password = password;
    } else if (kind === 'postgresql') {
      payload.config = {dsn: document.querySelector('#postgresql-dsn').value.trim(), schemas: document.querySelector('#postgresql-schemas').value.split(',').map(item => item.trim()).filter(Boolean)};
      const password = document.querySelector('#postgresql-password').value;
      if (password) payload.password = password;
    }
    response = await fetch(`/api/data-sources/${encodeURIComponent(editingSourceId)}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  } else if (kind === 'sqlite') {
    const selectedFile = document.querySelector('#sqlite-file').files[0];
    if (!selectedFile) { status.textContent = '请选择 SQLite 数据库文件'; return; }
    const body = new FormData();
    body.append('name', document.querySelector('#source-name').value.trim());
    body.append('description', description);
    body.append('file', selectedFile);
    response = await fetch('/api/data-sources/upload-sqlite', {method: 'POST', body});
  } else {
    const config = kind === 'mysql'
      ? {host: document.querySelector('#mysql-host').value.trim(), port: Number(document.querySelector('#mysql-port').value), database: document.querySelector('#mysql-database').value.trim(), user: document.querySelector('#mysql-user').value.trim(), ssl: document.querySelector('#mysql-ssl').checked}
      : {dsn: document.querySelector('#postgresql-dsn').value.trim(), schemas: document.querySelector('#postgresql-schemas').value.split(',').map(item => item.trim()).filter(Boolean)};
    const password = kind === 'mysql' ? document.querySelector('#mysql-password').value : document.querySelector('#postgresql-password').value;
    response = await fetch('/api/data-sources', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: document.querySelector('#source-name').value.trim(), description, kind, config, password}),
    });
  }
  const data = await response.json();
  if (!response.ok) { status.textContent = data.detail || '保存失败'; return; }
  currentDataSourceId = data.id || editingSourceId;
  localStorage.setItem('verisql_data_source_id', currentDataSourceId);
  status.textContent = editingSourceId ? '数据源已更新，旧连接池已失效' : `${kind === 'sqlite' ? '上传' : '连接'}成功，发现 ${data.table_count} 张表`;
  editingSourceId = null;
  document.querySelector('#source-form').classList.add('hidden');
  await loadDataSources();
});

document.querySelectorAll('.feedback-button').forEach(button => {
  button.addEventListener('click', async () => {
    if (!currentRequestId) return;
    const status = document.querySelector('#feedback-status');
    const buttons = document.querySelectorAll('.feedback-button');
    buttons.forEach(item => { item.disabled = true; });
    status.textContent = '正在保存…';
    try {
      const response = await fetch('/api/feedback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          request_id: currentRequestId,
          verdict: button.dataset.verdict,
          comment: document.querySelector('#feedback-comment').value.trim(),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '反馈保存失败');
      buttons.forEach(item => item.classList.toggle('selected', item === button));
      status.textContent = '反馈已保存，可以重新选择进行修改。';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      buttons.forEach(item => { item.disabled = false; });
    }
  });
});

document.querySelectorAll('.example').forEach(button => {
  button.addEventListener('click', () => {
    question.value = [...button.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
    autoResizeQuestion();
    setSidebarOpen(false);
    question.focus();
  });
});

document.querySelector('#refresh-sessions').addEventListener('click', loadSessions);
document.querySelector('#session-list').addEventListener('click', async event => {
  const item = event.target.closest('[data-thread-id]');
  if (!item) return;
  const threadId = item.dataset.threadId;
  const action = event.target.closest('[data-session-action]')?.dataset.sessionAction;
  if (action === 'rename') {
    const title = window.prompt('新的会话名称');
    if (!title) return;
    await fetch(`/api/workbench/sessions/${encodeURIComponent(threadId)}?data_source_id=${encodeURIComponent(currentDataSourceId)}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title})});
    await loadSessions();
    return;
  }
  if (action === 'delete') {
    if (!window.confirm('确认删除该会话及其短期结果缓存？')) return;
    await fetch(`/api/sessions/${encodeURIComponent(threadId)}?data_source_id=${encodeURIComponent(currentDataSourceId)}`, {method: 'DELETE'});
    if (currentThreadId === threadId) { currentThreadId = null; localStorage.removeItem('verisql_thread_id'); }
    document.querySelector('#session-query-list').classList.add('hidden');
    await loadSessions();
    return;
  }
  await openSession(threadId);
});
document.querySelector('#session-query-list').addEventListener('click', async event => {
  const item = event.target.closest('[data-request-id]');
  if (!item) return;
  try { await openHistoricalResult(item.dataset.requestId); } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove('hidden'); }
});

document.querySelector('#page-prev').addEventListener('click', () => loadResultPage(resultPage - 1));
document.querySelector('#page-next').addEventListener('click', () => loadResultPage(resultPage + 1));
document.querySelector('#page-size').addEventListener('change', event => { resultPageSize = Number(event.target.value); loadResultPage(1); });
document.querySelector('#export-csv').addEventListener('click', () => {
  if (currentRequestId) window.location.href = `/api/query-results/${encodeURIComponent(currentRequestId)}/export.csv`;
});
document.querySelector('#toggle-chart').addEventListener('click', () => {
  const panel = document.querySelector('#chart-panel');
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) renderChart();
});
document.querySelector('#render-chart').addEventListener('click', renderChart);
document.querySelector('#chart-type').addEventListener('change', renderChart);

document.querySelector('#open-sidebar').addEventListener('click', () => setSidebarOpen(true));
document.querySelector('#close-sidebar').addEventListener('click', () => setSidebarOpen(false));
document.querySelector('#show-trace-panel').addEventListener('click', () => setTraceOpen(true));
document.querySelector('#close-trace-panel').addEventListener('click', () => setTraceOpen(false));
mobileScrim.addEventListener('click', closeResponsivePanels);
question.addEventListener('input', autoResizeQuestion);
question.addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    if (!submit.disabled) form.requestSubmit();
  }
});
document.querySelector('#copy-sql').addEventListener('click', async event => {
  const button = event.currentTarget;
  const sql = document.querySelector('#sql').textContent;
  if (!sql) return;
  try {
    await navigator.clipboard.writeText(sql);
    button.textContent = '已复制';
  } catch (_) {
    button.textContent = '复制失败';
  }
  window.setTimeout(() => { button.textContent = '复制 SQL'; }, 1400);
});

function adminTable(columns, rows) {
  if (!rows.length) return '<p class="field-help">暂无数据。</p>';
  return `<div class="admin-table-wrap"><table><thead><tr>${columns.map(item => `<th>${escapeHtml(item[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(item => `<td>${escapeHtml(item[1](row))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

async function loadAdminOverview() {
  const response = await fetch('/api/admin/overview');
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '无法读取管理指标');
  const metrics = [
    ['总查询', data.total || 0],
    ['成功率', data.success_rate == null ? '暂无' : `${(data.success_rate * 100).toFixed(1)}%`],
    ['平均耗时', `${Math.round(data.avg_latency_ms || 0)} ms`],
    ['Token', data.total_tokens || 0],
    ['低置信度', data.low_confidence || 0],
    ['已取消', data.cancelled_count || 0],
    ['已反馈', data.feedback_total || 0],
    ['反馈验证准确率', data.verified_accuracy == null ? '暂无' : `${(data.verified_accuracy * 100).toFixed(1)}%`],
  ];
  document.querySelector('#admin-metrics').innerHTML = metrics.map(([label, value]) => `<div><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></div>`).join('');
  const daily = data.daily || [];
  const maxQueries = Math.max(1, ...daily.map(item => item.queries || 0));
  document.querySelector('#admin-trend').innerHTML = daily.length ? `
    <div class="admin-trend-head"><b>最近 ${daily.length} 天查询趋势</b><span>柱高代表每日查询量</span></div>
    <div class="trend-bars">${daily.map(item => `<div class="trend-bar" style="height:${Math.max(8, Math.round((item.queries || 0) / maxQueries * 100))}%" title="${escapeHtml(item.day)}：${escapeHtml(item.queries)} 次，成功 ${escapeHtml(item.successful || 0)} 次"><span>${escapeHtml(String(item.day).slice(5))}</span></div>`).join('')}</div>` : '';
}

function renderAdminTraceDetail(item) {
  const detail = document.querySelector('#admin-trace-detail');
  if (!detail || !item) return;
  const timeline = item.timeline || [];
  detail.innerHTML = `<div class="admin-trace-detail"><h3>${escapeHtml(item.question || '未记录问题文本')}</h3>
    <div class="admin-trace-timeline">${timeline.length ? timeline.map(step => `<span>${escapeHtml(step.sequence || '')} ${escapeHtml(step.node || step.message || '')} · ${escapeHtml(step.duration_ms || 0)} ms</span>`).join('') : '<span>该记录没有节点时间线</span>'}</div>
    <div class="trace-grid"><div class="trace-item"><small>Request ID</small><b>${escapeHtml(item.request_id || '—')}</b></div><div class="trace-item"><small>Trace ID</small><b>${escapeHtml(item.trace_id || '未采样')}</b></div><div class="trace-item"><small>检索表</small><b>${escapeHtml((item.retrieved_tables || []).join(', ') || '—')}</b></div><div class="trace-item"><small>错误类型</small><b>${escapeHtml((item.error_types || []).join(', ') || '无')}</b></div></div></div>`;
  detail.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

async function loadAdminTab(tab) {
  const content = document.querySelector('#admin-content');
  content.innerHTML = '<p>正在加载…</p>';
  let response;
  if (tab === 'traces') {
    response = await fetch('/api/admin/traces?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '加载失败');
    adminTraceItems = data.items || [];
    content.innerHTML = adminTraceItems.length ? `<div class="admin-trace-list">${adminTraceItems.map((row, index) => `<button class="admin-trace-row" type="button" data-trace-index="${index}"><span><small>时间</small><b>${escapeHtml(new Date(row.created_at).toLocaleString())}</b></span><span><small>${escapeHtml(row.username || '未知用户')} · ${escapeHtml(row.model || '未知模型')}</small><b>${escapeHtml(row.question || row.request_id || '未记录问题')}</b></span><span><small>状态</small><b class="trace-row-status ${escapeHtml(row.status)}">${escapeHtml(row.status)}</b></span><span><small>耗时</small><b>${escapeHtml(row.latency_ms || 0)} ms</b></span><span><small>Token</small><b>${escapeHtml(row.total_tokens || 0)}</b></span><span><small>置信度 / 成本</small><b>${row.confidence_score == null ? '—' : `${Math.round(row.confidence_score * 100)}%`} · ${escapeHtml(row.cost_level || '—')}</b></span><span aria-hidden="true">›</span></button>`).join('')}</div><div id="admin-trace-detail"></div>` : '<p class="field-help">暂无查询记录。</p>';
  } else if (tab === 'resources') {
    response = await fetch('/api/audit/resources?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '加载失败');
    content.innerHTML = adminTable([
      ['时间', row => new Date(row.created_at).toLocaleString()], ['用户', row => row.username],
      ['动作', row => row.action], ['资源', row => `${row.resource_type} · ${row.resource_name || row.resource_id || ''}`],
      ['状态', row => row.status], ['说明', row => row.detail],
    ], data.items || []);
  } else if (tab === 'feedback') {
    response = await fetch('/api/admin/feedback?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '加载失败');
    content.innerHTML = adminTable([
      ['时间', row => new Date(row.updated_at).toLocaleString()], ['用户', row => row.username],
      ['结论', row => row.verdict === 'correct' ? '正确' : '错误'], ['问题', row => row.question],
      ['备注', row => row.comment], ['请求 ID', row => row.request_id],
    ], data.items || []);
  } else {
    response = await fetch('/api/admin/evaluation');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '加载失败');
    content.innerHTML = `<div class="evaluation-card"><h3>用户反馈验证集</h3><p class="evaluation-score">${data.verified_accuracy == null ? '暂无评分' : `${(data.verified_accuracy * 100).toFixed(1)}%`}</p><p>已评分 ${escapeHtml(data.rated_queries)} 条 · 正确 ${escapeHtml(data.correct)} · 错误 ${escapeHtml(data.incorrect)}</p><p class="field-help">${escapeHtml(data.note)}</p></div>`;
  }
}

document.querySelector('#show-admin-center').addEventListener('click', async () => {
  document.querySelector('#admin-center').classList.remove('hidden');
  try { await Promise.all([loadAdminOverview(), loadAdminTab('traces')]); }
  catch (error) { document.querySelector('#admin-content').textContent = error.message; }
});
document.querySelector('#mobile-admin-center').addEventListener('click', async () => {
  setSidebarOpen(false);
  document.querySelector('#admin-center').classList.remove('hidden');
  try { await Promise.all([loadAdminOverview(), loadAdminTab('traces')]); }
  catch (error) { document.querySelector('#admin-content').textContent = error.message; }
});
document.querySelector('#close-admin-center').addEventListener('click', () => document.querySelector('#admin-center').classList.add('hidden'));
document.querySelector('#admin-center').addEventListener('click', event => {
  if (event.target === event.currentTarget) event.currentTarget.classList.add('hidden');
});
document.querySelector('#admin-content').addEventListener('click', event => {
  const row = event.target.closest('[data-trace-index]');
  if (row) renderAdminTraceDetail(adminTraceItems[Number(row.dataset.traceIndex)]);
});
document.querySelector('.admin-tabs').addEventListener('click', async event => {
  const button = event.target.closest('[data-admin-tab]');
  if (!button) return;
  document.querySelectorAll('[data-admin-tab]').forEach(item => item.classList.toggle('selected', item === button));
  try { await loadAdminTab(button.dataset.adminTab); }
  catch (error) { document.querySelector('#admin-content').textContent = error.message; }
});

window.addEventListener('resize', () => {
  if (window.innerWidth > 1280) closeResponsivePanels();
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  closeResponsivePanels();
  closeManagementDrawers();
  document.querySelector('#admin-center').classList.add('hidden');
});

bootstrap();
