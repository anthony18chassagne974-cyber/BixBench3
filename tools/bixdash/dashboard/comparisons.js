/* Pure data helpers are exported for tests; no fabricated scores or reasoning double-count. */
(function (root) {
  const number = v => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
  function totalTokens(latest) {
    const t = latest.tokens;
    if (t && ['input', 'cached', 'output'].every(k => number(t[k]) !== null)) return t.input + t.cached + t.output;
    return number(latest.tokens_total);
  }
  function rowOf(run) {
    const m = run.meta || {}, l = run.latest || {};
    const rawScore = number(l.partial_score);
    return {id: run.run_id, name: m.description || m.condition || run.run_id,
      task: String(m.task_id ?? 'Unknown'), owner: m.owner || 'Unspecified',
      model: m.model || 'Unknown', condition: m.condition || '',
      score: rawScore !== null && rawScore <= 1 ? rawScore : null,
      final: l.score_is_final === true, cost: number(l.cost_usd),
      time: number(l.elapsed_s) === null ? null : l.elapsed_s / 60,
      calls: number(l.n_model_calls), tokens: totalTokens(l), status: l.status || l.phase || 'Unknown'};
  }
  function plottable(rows, metric, includePartial) {
    return rows.filter(r => r.score !== null && (r.final || includePartial) && r[metric] !== null);
  }
  const helpers = {totalTokens, rowOf, plottable};
  if (typeof module !== 'undefined') module.exports = helpers;
  root.BixComparisons = helpers;
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => v === null ? '—' : v.toLocaleString('en-US', {maximumFractionDigits: 2});
  const score = v => v === null ? 'Pending' : (v * 100).toFixed(1) + '%';
  const palette = ['#287bb8','#d56a34','#22966a','#a36ac4','#bd4e76','#8b7b30'];
  let rows = [], colors = {}, loading = false, initialized = false;
  function options(id, values, allLabel) {
    const old = $(id).value;
    $(id).innerHTML = `<option value="all">${allLabel}</option>` + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    $(id).value = values.includes(old) ? old : 'all';
  }
  function render() {
    const selected = rows.filter(r => ($('task').value === 'all' || r.task === $('task').value) && ($('owner').value === 'all' || r.owner === $('owner').value));
    const partial = $('partial').checked, metric = $('budget').value;
    $('budget-heading').textContent = metric === 'tokens' ? 'Score vs total tokens' : 'Score vs model calls';
    $('legend').innerHTML = selected.map(r => `<span><i class="dot" style="background:${colors[r.id]}"></i>${esc(r.name)}</span>`).join('');
    chart('cost-chart', plottable(selected, 'cost', partial), 'cost', 'Estimated analyst API cost (USD)');
    chart('time-chart', plottable(selected, 'time', partial), 'time', 'Elapsed time (minutes)');
    chart('budget-chart', plottable(selected, metric, partial), metric, metric === 'tokens' ? 'Total tokens (millions)' : 'Model calls');
    const finals = selected.filter(r => r.score !== null && r.final).length;
    const inter = selected.filter(r => r.score !== null && !r.final).length;
    $('coverage').textContent = `${selected.length} runs selected · ${finals} final scores · ${inter} intermediate scores · ${selected.filter(r => r.score === null).length} awaiting a score.`;
    $('run-table').innerHTML = selected.length ? selected.map(r => `<tr><td><span class="dot" style="background:${colors[r.id]}"></span> ${esc(r.name)}<div class="note">${esc(r.model)} · ${esc(r.condition)}</div></td><td>${esc(r.task)} / ${esc(r.owner)}</td><td>${score(r.score)}${r.score === null ? '' : `<br><span class="tag">${r.final ? 'Final' : 'Intermediate'}</span>`}</td><td class="num">${r.cost === null ? '—' : '$' + r.cost.toFixed(2)}</td><td class="num">${fmt(r.time)} min</td><td class="num">${fmt(r.calls)}</td><td class="num">${fmt(r.tokens)}</td><td>${esc(r.status)}</td></tr>`).join('') : '<tr><td colspan="8">No runs match these filters.</td></tr>';
  }
  function chart(id, points, metric, label) {
    const el = $(id);
    if (!points.length) { el.innerHTML = '<div class="empty">No scored runs with this budget value yet.<br>Unscored runs remain in the table below.</div>'; return; }
    const W = 540, H = 330, P = {l:58,r:22,t:16,b:54};
    const maximum = Math.max(...points.map(r => r[metric]));
    const xmax = Math.max(metric === 'cost' ? 0.1 : 1, maximum * 1.18);
    const X = x => P.l + x / xmax * (W-P.l-P.r), Y = y => H-P.b-y*(H-P.t-P.b);
    const tick = x => metric === 'tokens' ? (x / 1e6).toLocaleString('en-US',{maximumFractionDigits:2}) : fmt(x);
    let svg = '';
    for (let i=0;i<=5;i++) {
      const y=i/5, x=xmax*i/5;
      svg += `<line x1="${P.l}" x2="${W-P.r}" y1="${Y(y)}" y2="${Y(y)}" stroke="var(--grid)"/><text x="${P.l-9}" y="${Y(y)+4}" text-anchor="end" fill="var(--muted)" font-size="12">${Math.round(y*100)}%</text>`;
      svg += `<text x="${X(x)}" y="${H-P.b+22}" text-anchor="middle" fill="var(--muted)" font-size="11">${tick(x)}</text>`;
    }
    svg += `<text x="${(P.l+W-P.r)/2}" y="${H-7}" text-anchor="middle" fill="var(--muted)" font-size="12">${label}</text><text transform="translate(15 ${H/2}) rotate(-90)" text-anchor="middle" fill="var(--muted)" font-size="12">Scientific score</text>`;
    points.forEach((r,i) => {
      const title = `${r.name} | ${r.final ? 'Final' : 'Intermediate'} score: ${score(r.score)} | ${label}: ${metric === 'tokens' ? fmt(r.tokens) : fmt(r[metric])}`;
      svg += `<circle class="point" data-point="${i}" tabindex="0" role="img" aria-label="${esc(title)}" cx="${X(r[metric])}" cy="${Y(r.score)}" r="7" fill="${r.final ? colors[r.id] : 'var(--surface)'}" stroke="${colors[r.id]}" stroke-width="2.5" ${r.final ? '' : 'stroke-dasharray="3 2"'}><title>${esc(title)}</title></circle>`;
    });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Scientific score versus ${esc(label)}">${svg}</svg><div class="detail" aria-live="polite">Hover or focus a point to inspect it.</div>`;
    el.querySelectorAll('.point').forEach(point => {
      const show = () => { const r = points[Number(point.dataset.point)]; el.querySelector('.detail').textContent = `${r.name} · ${r.final ? 'Final' : 'Intermediate'} score ${score(r.score)} · $${fmt(r.cost)} API · ${fmt(r.time)} min · ${fmt(r.calls)} model calls · ${fmt(r.tokens)} tokens`; };
      point.addEventListener('mouseenter', show); point.addEventListener('focus', show); point.addEventListener('click', show);
    });
  }
  async function load() {
    if (loading) return;
    loading = true; $('refresh').disabled = true;
    try {
      const response = await fetch('/api/runs', {cache:'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json(); rows = data.map(rowOf).sort((a,b) => a.id.localeCompare(b.id));
      rows.forEach((r,i) => colors[r.id] = palette[i % palette.length]);
      options('task', [...new Set(rows.map(r => r.task))].sort((a,b) => Number(a)-Number(b)), 'All tasks');
      options('owner', [...new Set(rows.map(r => r.owner))].sort(), 'All owners');
      if (!initialized && rows.some(r => r.task === '11')) $('task').value = '11';
      initialized = true; render();
      $('refresh-status').textContent = `Updated ${new Date().toLocaleTimeString()} · refreshes every 60 s`;
    } catch (error) { $('refresh-status').textContent = `Could not refresh: ${error.message}. Showing last available data.`; }
    finally { loading = false; $('refresh').disabled = false; }
  }
  ['task','owner','budget','partial'].forEach(id => $(id).addEventListener('change', render));
  $('refresh').addEventListener('click', load); load(); setInterval(load, 60000);
})(typeof globalThis !== 'undefined' ? globalThis : this);
