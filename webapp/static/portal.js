// Both views use the same form, signed plans, authentication and task APIs.
if (location.pathname === '/portal') {
  document.title = 'AEF-GRiTS · 网页下载工作台';
  const switchLink = document.getElementById('view-switch');
  switchLink.href = '/';
  switchLink.textContent = '简洁界面';
  document.getElementById('auth-login').href = '/api/auth/login?next=/portal';
  document.getElementById('header-product').innerHTML = `
    <nav id="portal-nav" aria-label="数据产品">
      <details class="portal-products">
        <summary><strong id="portal-selected">AEF 年度嵌入</strong><span class="visually-hidden">产品</span></summary>
        <div class="portal-product-menu">
          <strong>目标数据（可多选）</strong>
          <label><input id="portal-aef" type="checkbox" checked><span>AlphaEarth Foundations<small>AEF 年度特征，64 维 / 10 m</small></span></label>
          <label class="unavailable"><input type="checkbox" disabled><span>Tessera<small>尚未接入，不会创建下载任务</small></span></label>
          <small>本期只开放 AEF。Tessera 0.1° 是可选存储格网，不表示下载 Tessera 产品。</small>
        </div>
      </details>
      <p class="header-product-meta">64 维 / 10 m</p>
    </nav>`;
  document.querySelector('.portal-products').addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.currentTarget.open = false;
      event.currentTarget.querySelector('summary').focus();
    }
  });
  document.getElementById('simple-btn-submit').insertAdjacentHTML('beforebegin', `
    <button id="portal-preview" class="portal-btn primary" type="button" disabled>检索 / 预检</button>`);
  document.getElementById('workspace').insertAdjacentHTML('beforeend', `
    <section id="portal-panel" class="collapsible-panel" aria-label="数据结果与下载管理">
      <div class="portal-panel-header">
        <div class="portal-tabs" role="tablist" aria-label="工作区">
          <button id="portal-plan-tab" class="portal-btn" role="tab" aria-selected="true" aria-controls="portal-plan-page">规划结果</button>
          <button id="portal-task-tab" class="portal-btn" role="tab" aria-selected="false" aria-controls="portal-task-page">下载管理 <span id="portal-task-count">0</span></button>
        </div>
        <div class="portal-panel-actions">
          <button id="portal-refresh" class="portal-btn" type="button">刷新任务</button>
          <button id="portal-panel-toggle" class="panel-toggle" type="button" aria-expanded="true" aria-controls="portal-panel-body">收起</button>
        </div>
      </div>
      <div id="portal-panel-body">
      <div id="portal-plan-page" class="portal-panel-page" role="tabpanel" aria-labelledby="portal-plan-tab">
        <div class="portal-toolbar"><p id="portal-plan-state" role="status">选好范围与年份后，点击预检。</p>
          <button id="portal-export" class="portal-btn" disabled>导出计划</button>
          <button id="portal-download" class="portal-btn primary" disabled>确认下载</button></div>
        <dl id="portal-plan-capacity" hidden></dl>
        <div class="portal-table-wrap"><table class="portal-table"><thead><tr><th>产品</th><th>目标 / 文件</th><th>年份</th><th>坐标说明</th><th>输出格式</th></tr></thead>
          <tbody id="portal-plan-rows"><tr><td colspan="5" class="portal-empty">预检结果将在这里显示</td></tr></tbody></table></div>
        <details id="portal-grid-notice"><summary>估算与采样说明</summary><p id="portal-sampling-note">预检不代表影像可用；实际覆盖以下载验证为准。</p><p>特征估算按 float32 计算，不是实际文件大小；最低可用空间包含安全预留及临时文件，磁盘可用量为预检时快照。</p></details>
      </div>
      <div id="portal-task-page" class="portal-panel-page" role="tabpanel" aria-labelledby="portal-task-tab" hidden>
        <div class="portal-toolbar"><label for="portal-task-filter">状态</label><select id="portal-task-filter">
          <option value="all">全部</option><option value="active">下载 / 验证中</option><option value="queued">排队</option>
          <option value="done">完成</option><option value="problem">异常 / 已取消</option></select>
          <p id="portal-task-summary" role="status">正在读取任务…</p></div>
        <div class="portal-table-wrap"><table class="portal-table"><thead><tr><th>任务 / 范围</th><th>年份</th><th>状态</th><th>进度</th><th>预计数据量</th><th>当前格网速率 / ETA</th><th>操作</th></tr></thead>
          <tbody id="portal-task-rows" tabindex="-1" aria-label="下载任务列表"></tbody></table></div>
      </div>
      </div>
    </section>`);

  document.getElementById('portal-panel-toggle').onclick = event => {
    setPanelCollapsed('portal-panel', event.currentTarget.getAttribute('aria-expanded') === 'true');
  };

  let pendingPlan = null;
  let plannedForm = '';
  const aef = document.getElementById('portal-aef');
  const preview = document.getElementById('portal-preview');
  const download = document.getElementById('portal-download');
  const exportButton = document.getElementById('portal-export');
  const planState = document.getElementById('portal-plan-state');
  document.addEventListener('aef:auth-change', () => {
    pendingPlan = null;
    planState.textContent = '认证已改变，请验证连接并重新预检。';
    download.disabled = true;
  });

  function formKey() {
    const files = Array.from(document.getElementById('simple-point-files').files)
      .map(file => [file.name, file.size, file.lastModified]);
    return JSON.stringify([buildPayload(), files, aef.checked]);
  }

  function showTab(tab) {
    setPanelCollapsed('portal-panel', false);
    for (const name of ['plan', 'task']) {
      const button = document.getElementById(`portal-${name}-tab`);
      button.setAttribute('aria-selected', String(name === tab));
      button.tabIndex = name === tab ? 0 : -1;
      document.getElementById(`portal-${name}-page`).hidden = name !== tab;
    }
  }
  for (const name of ['plan', 'task']) {
    const tab = document.getElementById(`portal-${name}-tab`);
    tab.onclick = () => showTab(name);
    tab.onkeydown = event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const next = event.key === 'Home' ? 'plan' : event.key === 'End' ? 'task' : name === 'plan' ? 'task' : 'plan';
        showTab(next);
        document.getElementById(`portal-${next}-tab`).focus();
      }
    };
  }
  showTab('plan');
  aef.onchange = () => {
    document.getElementById('portal-selected').textContent = aef.checked ? 'AEF 年度嵌入' : '请选择目标数据';
    validateForm();
  };
  document.addEventListener('aef:form', () => {
    preview.disabled = !aef.checked || document.getElementById('simple-btn-submit').disabled;
    preview.textContent = submittingTask ? '正在处理…' : '检索 / 预检';
    if (pendingPlan && formKey() !== plannedForm) {
      pendingPlan = null;
      planState.textContent = '条件已改变，请重新检索 / 预检。下方是旧规划，不可提交。';
    }
    download.disabled = !pendingPlan || !pendingPlan.can_start || submittingTask || !authReady;
    exportButton.disabled = !pendingPlan;
  });
  preview.onclick = () => {
    plannedForm = formKey();
    pendingPlan = null;
    submitTask({previewOnly: true});
  };
  document.getElementById('preview-grid-selection').onclick = () => {
    selectedGridIds = null;
    preview.onclick();
  };
  download.onclick = () => submitTask({preparedPlan: pendingPlan});
  document.addEventListener('aef:plan', ({detail: plan}) => {
    if (formKey() !== plannedForm) {
      planState.textContent = '预检期间条件已改变，请重新预检。';
      return;
    }
    pendingPlan = plan;
    const entries = plan.workflow === 'grid' ? plan.grid_features.features.map(feature => ({
      target: feature.properties.grid_id, crs: feature.properties.crs, format: {zarr:'Zarr', 'zarr+geotiff':'Zarr + GeoTIFF', geotiff:'GeoTIFF（仅 TIFF）'}[plan.output_format] || 'Zarr',
    })) : [{target: `${plan.source_name}，${plan.rows} 点`, crs: `点位经纬度 ${plan.normalized_crs}`, format: 'Parquet'}];
    document.getElementById('portal-plan-rows').innerHTML = entries.map((row, index) => `<tr>
      <td>AEF <small>64 维 · 10 m</small></td><td>${plan.workflow === 'grid' ? `<button class="portal-btn" data-grid-index="${index}">${escapeHtml(row.target)}</button>` : escapeHtml(row.target)}</td>
      <td>${plan.years.join(', ')}</td><td>${escapeHtml(row.crs)}</td><td>${row.format}</td></tr>`).join('');
    planState.textContent = plan.can_start ? '预检通过，请确认下载。' : '不可启动：' + plan.blocked_reasons.join('；');
    const capacity = document.getElementById('portal-plan-capacity');
    capacity.hidden = false;
    capacity.innerHTML = [['特征估算（float32）', plan.raw_bytes], ['最低可用空间（含预留）', plan.disk_required_bytes], ['磁盘可用（预检时）', plan.disk_free_bytes]]
      .map(([label, bytes]) => `<div><dt>${label}</dt><dd>${formatBytes(bytes)}</dd></div>`).join('');
    document.getElementById('portal-sampling-note').textContent = plan.workflow === 'points'
      ? 'WGS84 用于记录点的位置；下载时读取该点所在的 AEF 原生 UTM 像元，并保存源资产、像元行列号与中心坐标。'
      : '坐标说明列为输出格网 CRS；地图显示和上传矢量的坐标系不会替代它。实际覆盖以下载验证为准。';
    document.querySelectorAll('[data-grid-index]').forEach(button => {
      button.onclick = () => map.fitBounds(L.geoJSON(plan.grid_features.features[Number(button.dataset.gridIndex)]).getBounds(), {
        padding: [30, 30], animate: !matchMedia('(prefers-reduced-motion: reduce)').matches});
    });
    showTab('plan');
  });
  exportButton.onclick = () => {
    const {workflow, grid_scheme, grid_ids, grid_crs, rows, source_name, normalized_crs, years, raw_gib, raw_bytes, disk_required_bytes, disk_free_bytes} = pendingPlan;
    const url = URL.createObjectURL(new Blob([JSON.stringify({product: 'AEF', workflow, grid_scheme,
      grid_ids, grid_crs, rows, source_name, normalized_crs, years, raw_gib, raw_bytes, disk_required_bytes, disk_free_bytes,
      note: 'Local plan only; not a GEE availability check or executable authorization.'}, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'aef-plan.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  document.addEventListener('aef:submitted', () => {
    pendingPlan = null;
    planState.textContent = '任务已提交；再次下载请重新预检。';
    showTab('task');
  });

  function renderTaskTable() {
    const tableBody = document.getElementById('portal-task-rows');
    const focusedAction = rememberTaskFocus(tableBody);
    const openBudgets = new Set(Array.from(tableBody.querySelectorAll('details[open]'), item => item.dataset.run));
    const filter = document.getElementById('portal-task-filter').value;
    const active = ['running', 'validating_output', 'cancelling'];
    const problem = ['failed', 'error', 'interrupted', 'cancelled', 'auth_required'];
    const rows = tasks.filter(task => filter === 'all' || (filter === 'active' ? active.includes(task.status)
      : filter === 'problem' ? problem.includes(task.status) : task.status === filter));
    document.getElementById('portal-task-count').textContent = tasks.length;
    document.getElementById('portal-task-summary').textContent = `${tasks.length} 个任务，${tasks.filter(task => active.includes(task.status)).length} 个活动任务`;
    tableBody.innerHTML = rows.map(task => {
      const detail = task.progress_detail || {};
      const rate = task.workflow === 'grid' && Number.isFinite(detail.rate_per_second) ? `${detail.rate_per_second.toFixed(2)} 块/s` : '—';
      const eta = Number.isFinite(detail.eta_seconds) ? `${(detail.eta_seconds / 60).toFixed(1)} min` : '—';
      const scope = task.workflow === 'points' ? `${task.source_name || '点位'} · ${task.sample_count} 点` : task.grid_ids.join(', ');
      const progress = Math.round(task.progress * 100);
      const actions = ['log', ...(task.output_dir ? ['path'] : []), ...(task.status === 'done' ? ['results'] : []),
        ...(['queued', 'running'].includes(task.status) ? ['cancel'] : []), ...(task.status === 'auth_required' ? ['resume'] : [])];
      const labels = {log: '日志', path: '路径', results: '结果', cancel: '取消', resume: '认证后续传'};
      return `<tr><td class="portal-target"><strong>AEF · ${task.workflow === 'grid' ? task.grid_scheme : '点位'}</strong>
        <small>${escapeHtml(task.run_id)}</small><small>${escapeHtml(scope)}</small><small>${escapeHtml(task.output_dir)}</small></td>
        <td>${task.years.join(', ')}</td><td><span class="task-status ${escapeHtml(task.status)}">${escapeHtml(taskStatusLabel(task.status))}</span>
          ${task.validation ? `<small>产物验证：${task.validation.valid ? '通过' : '失败'}</small>` : ''}
          ${task.queue_position ? `<small>队列位置 ${task.queue_position}</small>` : ''}
          ${task.error ? `<small>${escapeHtml(task.error)}</small>` : ''}</td>
        <td>${progress}%<progress value="${progress}" max="100" aria-label="任务进度"></progress></td>
        <td>${formatBytes(task.raw_bytes ?? (Number.isFinite(task.raw_gib) ? task.raw_gib * 1024 ** 3 : null))}<small>float32 估算</small>
          <details data-run="${escapeHtml(task.run_id)}" ${openBudgets.has(task.run_id) ? 'open' : ''}><summary data-action="budget" data-run="${escapeHtml(task.run_id)}">空间预算</summary><small>最低可用 ${formatBytes(task.disk_required_bytes)}（含预留）</small><small>预检时可用 ${formatBytes(task.disk_free_bytes)}</small></details></td>
        <td title="块速率不是网络MB/s；未提供时不估算">${rate}<small>当前格网 ETA ${eta}</small></td>
        <td><div class="portal-actions">${actions.map(action => `<button class="portal-btn" data-action="${action}" data-run="${escapeHtml(task.run_id)}">${labels[action]}</button>`).join('')}</div></td></tr>`;
    }).join('') || '<tr><td colspan="7" class="portal-empty">该状态下没有任务</td></tr>';
    restoreTaskFocus(tableBody, focusedAction);
  }
  document.getElementById('portal-task-rows').onclick = event => {
    const button = event.target.closest('[data-action]');
    if (!button || button.dataset.action === 'budget') return;
    const run = button.dataset.run;
    const actions = {log: () => window.open(`/api/tasks/${encodeURIComponent(run)}/logfile`, '_blank', 'noopener'),
      path: () => copyOutputPath(run), results: () => showResults(run), cancel: () => cancelTask(run), resume: () => resumeTask(run)};
    actions[button.dataset.action]();
  };
  document.getElementById('portal-task-filter').onchange = renderTaskTable;
  document.getElementById('portal-refresh').onclick = loadTasks;
  document.addEventListener('aef:tasks', renderTaskTable);
  renderTaskTable();
  validateForm();
  map.invalidateSize();
}
