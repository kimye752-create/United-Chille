/**
 * UPharma Export AI — 우루과이 약가 수집 · 인도네시아 파트너 매칭 대시보드 스크립트
 * ═══════════════════════════════════════════════════════════════
 *
 * 기능 목록:
 *   §1  상수 & 전역 상태
 *   §2  탭 전환          goTab(id, el)
 *   §3  환율 로드        loadExchange()  → GET /api/exchange
 *   §4  To-Do 리스트     initTodo / toggleTodo / markTodoDone / addTodoItem
 *   §5  보고서 탭        renderReportTab / _addReportEntry
 *   §6  API 키 배지      loadKeyStatus() → GET /api/keys/status
 *   §7  진행 단계        setProgress / resetProgress
 *   §8  파이프라인       runPipeline / pollPipeline
 *   §9  신약 분석        runCustomPipeline / _pollCustomPipeline
 *   §10 결과 렌더링      renderResult
 *   §11 초기화
 *
 * 수정 이력 (원본 대비):
 *   B1  /api/sites 제거 → /api/datasource/status
 *   B2  크롤링 step → DB 조회 step (prog-db_load)
 *   B3  refreshOutlier → /api/analyze/result
 *   B4  논문 카드: refs 0건이면 숨김
 *   U1  API 키 상태 배지
 *   U2  진입 경로(entry_pathway) 표시
 *   U3  신뢰도(confidence_note) 표시
 *   U4  PDF 카드 3가지 상태
 *   U6  재분석 버튼
 *   N1  탭 전환 (AU 프론트 기반)
 *   N2  환율 카드 (yfinance SGD/KRW)
 *   N3  To-Do 리스트 (localStorage)
 *   N4  보고서 탭 자동 등록
 * ═══════════════════════════════════════════════════════════════
 */

'use strict';

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §1. 상수 & 전역 상태
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/** product_id → INN 표시명 (칠레 CL_ 품목) */
const INN_MAP = {
  CL_sereterol_activair:     'Fluticasone + Salmeterol',
  CL_hydrine_hydroxyurea_500:'Hydroxyurea 500mg',
  CL_gadvoa_gadobutrol_604:  'Gadobutrol 604mg',
  CL_ciloduo_cilosta_rosuva: 'Cilostazol SR + Rosuvastatin',
  CL_rosumeg_combigel:       'Rosuvastatin + Omega-3',
  CL_atmeg_combigel:         'Atorvastatin + Omega-3',
  CL_gastiin_cr_mosapride:   'Mosapride CR 15mg',
  CL_omethyl_omega3_2g:      'Omega-3 EE 2g',
};

/**
 * B2: 서버 step 이름 → 프론트 progress 단계 ID 매핑
 * 서버 step: init → db_load → analyze → refs → report → done
 */
const STEP_ORDER = ['db_load', 'analyze', 'refs', 'report'];

let _pollTimer  = null;   // 파이프라인 폴링 타이머
let _currentKey = null;   // 현재 선택된 product_key

// P2 3열 시나리오용 원본 데이터 (가격은 USD FOB 기준)
// sgd_usd / sgd_krw 필드명은 하위호환용 — 실제로는 usd_clp / usd_krw 비율을 저장
let _p2ScenarioRaw = { agg: 0, avg: 0, cons: 0, sgd_usd: 1, sgd_krw: 1393 };

// P2 컬럼별 커스텀 옵션 데이터
let _p2ColData = {
  agg:  { opts: [] },
  avg:  { opts: [] },
  cons: { opts: [] },
};

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §2. 탭 전환 (N1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * 탭 전환: 모든 .page / .tab 비활성 후 대상만 활성화.
 * @param {string} id  — 대상 페이지 element ID
 * @param {Element} el — 클릭된 탭 element
 */
function goTab(id, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
  const page = document.getElementById(id);
  if (page) page.classList.add('on');
  if (el)   el.classList.add('on');
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §2-b. 공정 섹션 토글
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const _processOpen = { p1: true, p2: true, p3: true };

function toggleProcess(id) {
  _processOpen[id] = !_processOpen[id];
  const body  = document.getElementById('pb-' + id);
  const arrow = document.getElementById('pa-' + id);
  if (body)  body.classList.toggle('hidden', !_processOpen[id]);
  if (arrow) arrow.classList.toggle('closed', !_processOpen[id]);
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §2-c. 거시 지표 로드 — GET /api/macro
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadMacro() {
  // 폴백 정적값 (API 실패 시 즉시 표시)
  const _FB = {
    gdp:    ['US$ 17,220', 'World Bank 2024'],
    pop:    ['1,848만 명',  'INE Chile'],
    pharma: ['$2.5B',      'IQVIA 2024'],
    import: ['80.4%',      'CEPAL 2024'],
  };
  const _apply = (d) => {
    const gdpPc  = d?.gdp_per_capita_usd;
    const gdpB   = d?.gdp_usd_b;
    const popN   = d?.population;
    const phaB   = d?.pharma_market_usd_b;
    const impPct = d?.pharma_import_pct;
    _setMacro('macro-gdp',
      gdpPc  ? `US$ ${Math.round(gdpPc).toLocaleString('ko-KR')}` :
      gdpB   ? `US$ ${Math.round((gdpB * 1e9) / 18480432).toLocaleString('ko-KR')}` : _FB.gdp[0],
      'macro-gdp-src',    d?.source?.gdp    || _FB.gdp[1]);
    _setMacro('macro-pop',
      popN   ? `${Math.round(popN / 10000).toLocaleString('ko-KR')}만 명`                 : _FB.pop[0],
      'macro-pop-src',    d?.source?.population || _FB.pop[1]);
    _setMacro('macro-pharma',
      phaB   ? `$${phaB.toFixed(1)}B`                                                     : _FB.pharma[0],
      'macro-pharma-src', d?.source?.pharma_market || _FB.pharma[1]);
    _setMacro('macro-import',
      impPct != null ? `${impPct}%`                                                        : _FB.import[0],
      'macro-import-src', d?.source?.pharma_import || _FB.import[1]);
  };

  // 즉시 폴백 표시 후 API로 갱신
  _apply(null);
  try {
    const res  = await fetch('/api/cl/macro');
    const data = await res.json();
    _apply(data);
  } catch (_) { /* 폴백 유지 */ }
}

function _setMacro(valId, val, srcId, src) {
  const ve = document.getElementById(valId);
  const se = document.getElementById(srcId);
  if (ve) ve.textContent = val;
  if (se) se.textContent = src;
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §3. 환율 로드 (N2) — GET /api/exchange
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadExchange() {
  const btn = document.getElementById('btn-exchange-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 조회 중…'; }

  try {
    const res  = await fetch('/api/exchange');
    const data = await res.json();

    // P2 환율 자동 채움용 전역 저장
    window._exchangeRates = data;
    if (typeof _p2FillExchangeRate === 'function') {
      _p2FillExchangeRate();
      if (typeof _renderP2Manual === 'function') _renderP2Manual();
    }

    // 메인 숫자 (USD/CLP — 1 USD = X CLP)
    const rateEl = document.getElementById('exchange-main-rate');
    if (rateEl) {
      const usdClp = data.usd_clp || data.clp_usd ? (1 / data.clp_usd) : 932;
      const fmt = Number(usdClp).toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      rateEl.innerHTML = `${fmt}<span style="font-size:14px;margin-left:4px;color:var(--muted);font-weight:700;">CLP</span>`;
    }

    // 서브 그리드 (USD/KRW + CLP 연관 환율)
    const subEl = document.getElementById('exchange-sub');
    if (subEl) {
      const fmtUsdKrw = (data.usd_krw || 1393).toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const fmtClpKrw = Number(data.clp_krw || 1.495).toFixed(4);
      const fmtClpUsd = Number(data.clp_usd || 0.001073).toFixed(6);
      const fmtUsdClp = Number(data.usd_clp || 932).toFixed(2);
      subEl.innerHTML = `
        <div class="irow" style="margin:0">
          <div style="font-size:10.5px;color:var(--muted);margin-bottom:3px;">USD / KRW</div>
          <div style="font-size:15px;font-weight:900;color:var(--navy);">${fmtUsdKrw}원</div>
        </div>
        <div class="irow" style="margin:0">
          <div style="font-size:10.5px;color:var(--muted);margin-bottom:3px;">CLP / KRW</div>
          <div style="font-size:15px;font-weight:900;color:var(--navy);">${fmtClpKrw}원</div>
        </div>
        <div class="irow" style="margin:0">
          <div style="font-size:10.5px;color:var(--muted);margin-bottom:3px;">CLP / USD</div>
          <div style="font-size:15px;font-weight:900;color:var(--navy);">${fmtClpUsd}</div>
        </div>
        <div class="irow" style="margin:0">
          <div style="font-size:10.5px;color:var(--muted);margin-bottom:3px;">USD / CLP</div>
          <div style="font-size:15px;font-weight:900;color:var(--navy);">${fmtUsdClp}</div>
        </div>
      `;
    }

    // 출처 + 조회 시각
    const srcEl = document.getElementById('exchange-source');
    if (srcEl) {
      const now = new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
      const fallbackNote = data.ok ? '' : ' · 폴백값';
      srcEl.textContent = `조회: ${now}${fallbackNote}`;
    }
  } catch (e) {
    const srcEl = document.getElementById('exchange-source');
    if (srcEl) srcEl.textContent = '환율 조회 실패 — 잠시 후 다시 시도해 주세요';
    console.warn('환율 로드 실패:', e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↺ 환율 새로고침'; }
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §4. To-Do 리스트 (N3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const TODO_FIXED_IDS = ['p1', 'rep', 'p2', 'p3'];
const TODO_LS_KEY    = 'cl_upharma_todos_v1';
let _lastTodoAddAt   = 0;

/** localStorage에서 todo 상태 읽기 */
function _loadTodoState() {
  try   { return JSON.parse(localStorage.getItem(TODO_LS_KEY) || '{}'); }
  catch { return {}; }
}

/** localStorage에 todo 상태 쓰기 */
function _saveTodoState(state) {
  localStorage.setItem(TODO_LS_KEY, JSON.stringify(state));
}

/** 페이지 로드 시 localStorage 상태 복원 */
function initTodo() {
  const state = _loadTodoState();

  // 고정 항목 상태 복원
  for (const id of TODO_FIXED_IDS) {
    const item = document.getElementById('todo-' + id);
    if (!item) continue;
    item.classList.toggle('done', !!state['fixed_' + id]);
  }

  // 커스텀 항목 렌더
  _renderCustomTodos(state);
}

/**
 * 고정 항목 수동 토글 (클릭 시 호출).
 * @param {string} id  'p1' | 'rep' | 'p2' | 'p3'
 */
function toggleTodo(id) {
  const state       = _loadTodoState();
  const key         = 'fixed_' + id;
  state[key]        = !state[key];
  _saveTodoState(state);

  const item = document.getElementById('todo-' + id);
  if (item) item.classList.toggle('done', state[key]);
}

/**
 * 자동 체크: 파이프라인·보고서 완료 시 호출 (N3).
 * @param {'p1'|'rep'} id
 */
function markTodoDone(id) {
  const state       = _loadTodoState();
  state['fixed_' + id] = true;
  _saveTodoState(state);

  const item = document.getElementById('todo-' + id);
  if (item) item.classList.add('done');
}

/** 사용자가 직접 항목 추가 */
function addTodoItem(evt) {
  if (evt) {
    if (evt.isComposing || evt.repeat) return;
    evt.preventDefault();
  }

  const now = Date.now();
  if (now - _lastTodoAddAt < 250) return;
  _lastTodoAddAt = now;

  const input = document.getElementById('todo-input');
  const text  = input ? input.value.trim() : '';
  if (!text) return;

  const state   = _loadTodoState();
  const customs = state.customs || [];
  customs.push({ id: now + Math.floor(Math.random() * 1000), text, done: false });
  state.customs = customs;
  _saveTodoState(state);
  _renderCustomTodos(state);
  if (input) input.value = '';
}

/** 커스텀 항목 토글 */
function toggleCustomTodo(id) {
  const state   = _loadTodoState();
  const customs = state.customs || [];
  const item    = customs.find(c => c.id === id);
  if (item) item.done = !item.done;
  state.customs = customs;
  _saveTodoState(state);
  _renderCustomTodos(state);
}

/** 커스텀 항목 삭제 */
function deleteCustomTodo(id) {
  const state   = _loadTodoState();
  state.customs = (state.customs || []).filter(c => c.id !== id);
  _saveTodoState(state);
  _renderCustomTodos(state);
}

/** 커스텀 항목 목록 DOM 갱신 */
function _renderCustomTodos(state) {
  const container = document.getElementById('todo-custom-list');
  if (!container) return;
  container.classList.add('todo-list');

  const customs = state.customs || [];
  if (!customs.length) { container.innerHTML = ''; return; }

  container.innerHTML = customs.map(c => `
    <div class="todo-item${c.done ? ' done' : ''}" onclick="toggleCustomTodo(${c.id})">
      <div class="todo-check"></div>
      <span class="todo-label">${_escHtml(c.text)}</span>
      <button
        onclick="event.stopPropagation();deleteCustomTodo(${c.id})"
        style="background:none;color:var(--muted);font-size:16px;cursor:pointer;
               border:none;outline:none;padding:0 4px;line-height:1;flex-shrink:0;"
        title="삭제"
      >×</button>
    </div>
  `).join('');
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §5. 보고서 탭 관리 (N4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const REPORTS_LS_KEY = 'cl_upharma_reports_v1';

function _loadReports() {
  try   { return JSON.parse(localStorage.getItem(REPORTS_LS_KEY) || '[]'); }
  catch { return []; }
}

/**
 * 시장조사 완료 후 renderResult()가 호출 → 보고서 탭에 항목 추가.
 * @param {object|null} result  분석 결과
 * @param {string|null} pdfName PDF 파일명
 */
function _addReportEntry(result, pdfName) {
  const reports = _loadReports();
  const productName = result ? (result.trade_name || result.product_id || '알 수 없음') : '알 수 없음';
  const entry   = {
    id:           Date.now(),
    product:      productName,
    product_key:  result ? (result.product_id || _currentKey || '') : (_currentKey || ''),
    stage_label:  '시장조사 보고서',
    report_title: `시장조사 보고서 · ${productName}`,
    inn:          result ? (INN_MAP[result.product_id] || result.inn || '') : '',
    verdict:      result ? (result.verdict || '—') : '—',
    price_hint:   result ? String(result.price_positioning_pbs || result.cenabast_max_price_clp || '').trim() : '',
    clp_price_hint: result ? (result.cenabast_max_price_clp ?? result.mercado_price_clp ?? null) : null,
    basis_trade:  result ? String(result.basis_trade || '').trim() : '',
    risks_conditions: result ? String(result.risks_conditions || '').trim() : '',
    timestamp: new Date().toLocaleString('ko-KR', {
      month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    }),
    hasPdf: !!pdfName,
    pdf_name: pdfName || '',
  };

  reports.unshift(entry);
  localStorage.setItem(REPORTS_LS_KEY, JSON.stringify(reports.slice(0, 30)));
  renderReportTab();
  _syncP2ReportsOptions();
}

function clearAllReports() {
  localStorage.setItem(REPORTS_LS_KEY, JSON.stringify([]));
  renderReportTab();
  _syncP2ReportsOptions();
}

function deleteReportEntry(id) {
  const reports = _loadReports().filter(r => r.id !== id);
  localStorage.setItem(REPORTS_LS_KEY, JSON.stringify(reports));
  renderReportTab();
  _syncP2ReportsOptions();
}

/** 보고서 탭 DOM 갱신 */
function renderReportTab() {
  const container = document.getElementById('report-tab-list');
  if (!container) return;

  const reports = _loadReports();
  if (!reports.length) {
    container.innerHTML = `
      <div class="rep-empty">
        아직 생성된 보고서가 없습니다.<br>
        시장조사를 실행하면 여기에 자동으로 등록됩니다.
      </div>`;
    return;
  }

  container.innerHTML = reports.map(r => {
    const vc = r.verdict === '적합'   ? 'green'
             : r.verdict === '부적합' ? 'red'
             : r.verdict !== '—'      ? 'orange'
             :                          'gray';
    const innSpan = r.inn
      ? ` <span style="font-weight:400;color:var(--muted);font-size:12px;">· ${_escHtml(r.inn)}</span>`
      : '';
    const dlBtn = r.hasPdf
      ? `<a class="btn-download"
            href="/api/report/download${r.pdf_name ? `?name=${encodeURIComponent(r.pdf_name)}` : ''}"
            target="_blank"
            style="padding:7px 14px;font-size:12px;flex-shrink:0;">📄 PDF</a>`
      : '';
    const delBtn = `<button class="btn-report-del" onclick="deleteReportEntry(${r.id})" title="보고서 삭제">×</button>`;

    return `
      <div class="rep-item">
        <div class="rep-item-info">
          <div class="rep-item-product">${_escHtml(r.report_title || r.product)}${innSpan}</div>
          <div class="rep-item-meta">${_escHtml(r.timestamp)}</div>
        </div>
        <div class="rep-item-verdict">
          <span class="bdg ${vc}">${_escHtml(r.verdict)}</span>
        </div>
        ${dlBtn}
        ${delBtn}
      </div>`;
  }).join('');
  _syncP2ReportsOptions();
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §6. 수출 가격 전략 (P2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

let _p2Ready = false;
let _p2Tab = 'ai';
let _p2ManualSeg = 'public';
let _p2AiSeg = 'public';          // 현재 탭 뷰 (public | private)
let _p2SelectedReportId = '';
let _p2AiSelectedReportId = '';
let _p2UploadedReportFilename = '';
let _p2AiPollTimer = null;
let _p2Manual = _makeP2Defaults();
let _p2LastScenarios = null;
let _p2ManualCalculated = false;

// 듀얼 마켓 결과 저장 (AI 분석 완료 후)
let _p2BothResults = null;  // { public: {...}, private: {...}, extracted: {}, exchange_rates: {} }

// FOB 모달 상태
let _fobModalState = null;  // { tier, seg, baseUsd, elements, aiDefaults, refClp, refUsd, usdClp, usdKrw }
const _FOB_TIER_LABELS = { agg: '저가 진입', avg: '기준가', cons: '프리미엄' };
const _FOB_TIER_IDX    = { agg: 0, avg: 1, cons: 2 };

function _makeP2Defaults() {
  return {
    public: [
      { key: 'base_price', label: 'Mercado Público 기준 낙찰가 (CLP)', value: 0, type: 'abs_input', unit: 'CLP', step: 100, min: 0, max: 9999999, enabled: true, fixed: false, expanded: false, hint: 'CENABAST 공급가 또는 Mercado Público 목표 낙찰가', rationale: '공공 채널 입찰 경쟁 및 CENABAST Ley 21.198 상한가 기준입니다.' },
      { key: 'exchange', label: 'USD→CLP 환율', value: 932.0, type: 'abs_input', unit: 'rate', step: 1, min: 1, max: 9999, enabled: true, fixed: false, expanded: false, hint: '1 USD당 CLP (실시간 자동 반영)', rationale: '실시간 환율을 반영해 환차 리스크를 줄입니다.' },
      { key: 'pub_ratio', label: 'FOB USD 역산 비율', value: 35, type: 'pct_mult', unit: '%', step: 1, min: 0, max: 99999, enabled: true, fixed: false, expanded: false, hint: 'CLP 낙찰가 대비 FOB 비율 (공공: 약 30-40%)', rationale: '관세 6%, 파트너마진, 물류비를 제외한 목표 FOB 비율입니다.' },
    ],
    private: [
      { key: 'base_het', label: '소매 기준가 (CLP · Cruz Verde 기준)', value: 0, type: 'abs_input', unit: 'CLP', step: 100, min: 0, max: 9999999, enabled: true, fixed: false, expanded: false, hint: 'Cruz Verde/Salcobrand 소매 CLP 가격', rationale: '민간 시장은 소매 가격 구조 역산이 중요합니다.' },
      { key: 'exchange', label: 'USD→CLP 환율', value: 932.0, type: 'abs_input', unit: 'rate', step: 1, min: 1, max: 9999, enabled: true, fixed: false, expanded: false, hint: '1 USD당 CLP (실시간 자동 반영)', rationale: '실시간 환율 반영으로 가격 정합성을 유지합니다.' },
      { key: 'iva', label: 'IVA 공제 (÷1.19)', value: 19, type: 'gst_fixed', unit: '%', step: 0, min: 19, max: 19, enabled: true, fixed: true, expanded: false, hint: '칠레 IVA 19% 고정 공제', rationale: '소비자 가격에서 부가세(IVA)를 분리합니다.' },
      { key: 'retail', label: '소매 마진율 (약국 체인)', value: 30, type: 'pct_deduct', unit: '%', step: 1, min: 0, max: 99999, enabled: true, fixed: false, expanded: false, hint: 'Cruz Verde/Salcobrand 마진 25-35%', rationale: '칠레 3대 약국 체인 마진 구조를 반영합니다.' },
      { key: 'partner', label: '파트너사 마진', value: 20, type: 'pct_deduct', unit: '%', step: 1, min: 0, max: 99999, enabled: true, fixed: false, expanded: false, hint: '현지 수입·유통 파트너 수수료 15-25%', rationale: '현지 등록 비용·영업 비용을 포함합니다.' },
      { key: 'duty', label: '수입 관세 (6%)', value: 6, type: 'pct_deduct', unit: '%', step: 0.5, min: 0, max: 30, enabled: true, fixed: false, expanded: false, hint: 'HS 3004 기준 관세 6% (FTA 적용 시 0%)', rationale: '칠레 의약품 수입 관세 HS 3004 기준입니다.' },
    ],
  };
}

function initP2Strategy() {
  if (!document.getElementById('p2-wrap')) return;
  _p2Ready = true;

  const aiSelect = document.getElementById('p2-ai-report-select');
  if (aiSelect) {
    aiSelect.addEventListener('change', (e) => {
      _p2AiSelectedReportId = e.target.value || '';
    });
  }

  _syncP2ReportsOptions();
  _p2FillExchangeRate();
}

function switchP2Tab(tab) {
  _p2Tab = tab === 'manual' ? 'manual' : 'ai';
  const aiBtn = document.getElementById('p2-tab-ai');
  const manualBtn = document.getElementById('p2-tab-manual');
  const aiTab = document.getElementById('p2-ai-tab');
  const manualTab = document.getElementById('p2-manual-tab');
  if (aiBtn && manualBtn) {
    aiBtn.classList.toggle('on', _p2Tab === 'ai');
    manualBtn.classList.toggle('on', _p2Tab === 'manual');
  }
  if (aiTab && manualTab) {
    aiTab.style.display = _p2Tab === 'ai' ? '' : 'none';
    manualTab.style.display = _p2Tab === 'manual' ? '' : 'none';
  }
  if (_p2Tab === 'ai') _showP2AiError('');
}

function setP2AiSeg(seg) {
  _p2AiSeg = seg === 'private' ? 'private' : 'public';
  document.getElementById('p2-ai-seg-public')?.classList.toggle('on', _p2AiSeg === 'public');
  document.getElementById('p2-ai-seg-private')?.classList.toggle('on', _p2AiSeg === 'private');

  const desc    = document.getElementById('p2-ai-seg-desc');
  const warning = document.getElementById('p2-seg-warning');
  const warnTxt = document.getElementById('p2-seg-warning-text');

  if (desc) {
    desc.textContent = _p2AiSeg === 'public'
      ? 'Mercado Público / CENABAST 채널 · Ley 21.198 소매 상한가 기준 역산'
      : 'Cruz Verde · Salcobrand · Farmacias Ahumada 소매 체인 · IVA 19% + 유통마진 기준 역산';
  }

  // 공공/민간 비교 주의사항
  if (warning && warnTxt) {
    if (_p2AiSeg === 'public') {
      warnTxt.textContent =
        'CENABAST 낙찰가는 민간 소매가보다 낮은 경우가 많지만, 정책 입찰 품목·공급 조건에 따라 민간 시장가보다 높게 형성될 수 있습니다. 두 채널을 모두 검토하세요.';
      warning.style.display = 'flex';
    } else {
      warnTxt.textContent =
        '민간 소매가는 약국 체인의 자체 마진 정책에 따라 편차가 크며, 공공 채널(CENABAST) 납품가와 별도로 협상됩니다. 약가 규제 이외의 추가 마진이 반영됩니다.';
      warning.style.display = 'flex';
    }
  }

  // 결과가 이미 로드돼 있으면 해당 세그먼트 뷰로 즉시 전환
  if (_p2BothResults) _renderP2ForSeg(_p2AiSeg);
}

async function handleP2FileSelect(inputEl) {
  const file = inputEl?.files?.[0];
  const statusEl = document.getElementById('p2-upload-status');
  const textEl = document.getElementById('p2-upload-text');
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    if (statusEl) {
      statusEl.style.display = 'block';
      statusEl.textContent = 'PDF 파일만 업로드 가능합니다.';
    }
    return;
  }

  if (statusEl) {
    statusEl.style.display = 'block';
    statusEl.textContent = '업로드 중…';
  }
  if (textEl) textEl.textContent = file.name;

  try {
    const arr = await file.arrayBuffer();
    const bytes = new Uint8Array(arr);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    const contentB64 = btoa(binary);

    const res = await fetch('/api/p2/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, content_b64: contentB64 }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.filename) throw new Error(data.detail || `HTTP ${res.status}`);

    _p2UploadedReportFilename = data.filename;
    _p2AiSelectedReportId = '';
    const aiSelect = document.getElementById('p2-ai-report-select');
    if (aiSelect) aiSelect.value = '';
    if (statusEl) statusEl.textContent = `업로드 완료: ${data.filename}`;
  } catch (err) {
    if (statusEl) statusEl.textContent = `업로드 실패: ${err.message}`;
  }
}

/* 수출 가격 전략 진행 단계 — 시장조사와 동일한 스타일 */
const P2_STEP_ORDER = ['extract', 'ai_extract', 'ai_analysis', 'report'];

function _setP2Progress(currentStep, status) {
  const row = document.getElementById('p2-progress-row');
  if (row) row.classList.add('visible');
  const idx = P2_STEP_ORDER.indexOf(currentStep);

  for (let i = 0; i < P2_STEP_ORDER.length; i++) {
    const el = document.getElementById('p2prog-' + P2_STEP_ORDER[i]);
    if (!el) continue;
    const dot = el.querySelector('.prog-dot');
    if (status === 'error' && i === idx) {
      el.className = 'prog-step error'; dot.textContent = '✕';
    } else if (i < idx || (i === idx && status === 'done')) {
      el.className = 'prog-step done'; dot.textContent = '✓';
    } else if (i === idx) {
      el.className = 'prog-step active'; dot.textContent = i + 1;
    } else {
      el.className = 'prog-step'; dot.textContent = i + 1;
    }
  }
}

function _resetP2Progress() {
  const row = document.getElementById('p2-progress-row');
  if (row) row.classList.remove('visible');
  for (let i = 0; i < P2_STEP_ORDER.length; i++) {
    const el = document.getElementById('p2prog-' + P2_STEP_ORDER[i]);
    if (!el) continue;
    el.className = 'prog-step';
    el.querySelector('.prog-dot').textContent = i + 1;
  }
}

function _showP2AiError(msg) {
  const el = document.getElementById('p2-ai-error-msg');
  if (!el) return;
  if (msg) { el.style.display = ''; el.textContent = msg; }
  else { el.style.display = 'none'; el.textContent = ''; }
}

function _resetP2AiResultView() {
  const resultSection = document.getElementById('p2-ai-result-section');
  if (resultSection) resultSection.style.display = 'none';
  const dlState = document.getElementById('p2-report-dl-state');
  if (dlState) dlState.innerHTML = '';
  _showP2AiError('');
}

function _resetP2ManualResultView() {
  _p2ManualCalculated = false;
  _p2LastScenarios = null;
  const card = document.getElementById('p2-manual-result-card');
  if (card) card.style.display = 'none';
}

function runP2ManualCalculation() {
  const icon = document.getElementById('p2-manual-calc-icon');
  if (icon) icon.textContent = '⏳';
  _p2ManualCalculated = true;
  _renderP2Manual();
  if (icon) icon.textContent = '▶';
}

async function runP2AiPipeline() {
  const runBtn = document.getElementById('btn-p2-ai-run');
  const runIcon = document.getElementById('p2-ai-run-icon');
  const selectedReport = _loadReports().find((r) => String(r.id) === String(_p2AiSelectedReportId));
  const reportFilename = _p2UploadedReportFilename || (selectedReport ? (selectedReport.pdf_name || '') : '');

  if (!reportFilename) {
    _showP2AiError('실행 전 PDF가 있는 보고서를 선택하거나 PDF를 직접 업로드해 주세요.');
    return;
  }

  if (_p2AiPollTimer) clearInterval(_p2AiPollTimer);
  _resetP2AiResultView();
  _resetP2Progress();
  _setP2Progress('extract', 'running');

  if (runBtn) runBtn.disabled = true;
  if (runIcon) runIcon.textContent = '⏳';

  try {
    const res = await fetch('/api/p2/pipeline', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_filename: reportFilename }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    _p2AiPollTimer = setInterval(_pollP2AiPipeline, 1800);
  } catch (err) {
    _setP2Progress('extract', 'error');
    _showP2AiError(`실행 실패: ${err.message}`);
    if (runBtn) runBtn.disabled = false;
    if (runIcon) runIcon.textContent = '▶';
  }
}

async function _pollP2AiPipeline() {
  try {
    const res = await fetch('/api/p2/pipeline/status');
    const data = await res.json();
    if (data.status === 'idle') return;

    // 서버 step → 프론트 진행 단계 매핑
    const stepMap = {
      extract:     () => _setP2Progress('extract',     'running'),
      ai_extract:  () => { _setP2Progress('extract', 'done'); _setP2Progress('ai_extract', 'running'); },
      exchange:    () => { _setP2Progress('ai_extract', 'done'); _setP2Progress('ai_analysis', 'running'); },
      ai_analysis: () => { _setP2Progress('ai_extract', 'done'); _setP2Progress('ai_analysis', 'running'); },
      report:      () => { _setP2Progress('ai_analysis', 'done'); _setP2Progress('report', 'running'); },
    };
    if (stepMap[data.step]) stepMap[data.step]();

    if (data.status === 'done') {
      clearInterval(_p2AiPollTimer);
      _p2AiPollTimer = null;
      for (const s of P2_STEP_ORDER) _setP2Progress(s, 'done');
      const rr = await fetch('/api/p2/pipeline/result');
      const result = await rr.json();
      _renderP2AiResult(result);
      document.getElementById('btn-p2-ai-run')?.removeAttribute('disabled');
      const runIcon = document.getElementById('p2-ai-run-icon');
      if (runIcon) runIcon.textContent = '▶';
    } else if (data.status === 'error') {
      clearInterval(_p2AiPollTimer);
      _p2AiPollTimer = null;
      const errStep = P2_STEP_ORDER.includes(data.step) ? data.step : 'extract';
      _setP2Progress(errStep, 'error');
      _showP2AiError(`오류: ${data.step_label || '파이프라인 실패'}`);
      document.getElementById('btn-p2-ai-run')?.removeAttribute('disabled');
      const runIcon = document.getElementById('p2-ai-run-icon');
      if (runIcon) runIcon.textContent = '▶';
    }
  } catch (_err) {
    // polling retry
  }
}

/* P2 3열 카드: 역산 섹션 토글 */
function toggleP2ColDetail(col) {
  const detail = document.getElementById('p2cd-' + col);
  const btn    = detail?.previousElementSibling?.querySelector('.p2-col-expand-btn');
  if (!detail) return;
  const open = detail.style.display === 'none';
  detail.style.display = open ? '' : 'none';
  if (btn) btn.textContent = (open ? '▾' : '▸') + ' 단계별 역산 보기';
}

/* P2 3열 카드: 기준가/수수료/운임/커스텀옵션 변경 시 가격 재계산 */
function recalcP2Col(col) {
  const base    = parseFloat(document.getElementById('p2ci-base-' + col)?.value || 0);
  const fee     = parseFloat(document.getElementById('p2ci-fee-' + col)?.value || 0);
  const freight = parseFloat(document.getElementById('p2ci-freight-' + col)?.value || 1);

  let price = base * (1 - fee / 100) * freight;

  const opts = _p2ColData[col]?.opts || [];
  for (const opt of opts) {
    if (opt.type === 'pct_add')   price *= (1 + opt.value / 100);
    else if (opt.type === 'pct_deduct') price *= (1 - opt.value / 100);
    else if (opt.type === 'abs_add')    price += opt.value;
  }
  price = Math.max(0, price);

  // price는 USD 기준 (base input이 USD)
  const usdClpCurr = window._exchangeRates?.usd_clp || 932;
  const clpDisp = usdClpCurr > 0 ? `CLP ${Math.round(price * usdClpCurr).toLocaleString('ko-KR')}` : '—';
  const krw = _p2ScenarioRaw.sgd_krw > 0 ? Math.round(price * _p2ScenarioRaw.sgd_krw).toLocaleString('ko-KR') : '—';

  const priceEl = document.getElementById('p2c-price-' + col);
  const subEl   = document.getElementById('p2c-sub-' + col);
  if (priceEl) priceEl.textContent = price.toFixed(2);
  if (subEl)   subEl.textContent   = `${clpDisp} · ${krw} KRW`;
}

/* P2 컬럼 커스텀 옵션 렌더링 */
function renderP2ColOptions(col, showAddForm) {
  const container = document.getElementById('p2co-' + col);
  if (!container) return;
  const opts = (_p2ColData[col] || { opts: [] }).opts;

  const typeLabel = { pct_add: '% 가산', pct_deduct: '% 차감', abs_add: 'USD 가산' };

  let html = opts.map(opt => `
    <div class="p2c-opt-row">
      <span class="p2c-opt-name">${_escHtml(opt.name)}</span>
      <span class="p2c-opt-type-label">${typeLabel[opt.type] || opt.type}</span>
      <input class="p2c-opt-val" type="number" value="${opt.value}" step="0.1" min="0"
        onchange="updateP2ColOption('${col}','${_escHtml(opt.id)}',this.value)">
      <button class="p2c-opt-del" onclick="removeP2ColOption('${col}','${_escHtml(opt.id)}')">×</button>
    </div>`).join('');

  if (showAddForm) {
    html += `
      <div class="p2c-opt-row p2c-add-row">
        <input class="p2c-opt-name-input" type="text" placeholder="옵션명" id="p2c-newname-${col}" maxlength="20">
        <select class="p2c-opt-type-select" id="p2c-newtype-${col}">
          <option value="pct_deduct">% 차감</option>
          <option value="pct_add">% 가산</option>
          <option value="abs_add">USD 가산</option>
        </select>
        <input class="p2c-opt-val" type="number" placeholder="값" id="p2c-newval-${col}" step="0.1" min="0">
        <button class="p2c-confirm-btn" onclick="confirmP2ColOption('${col}')">✓</button>
      </div>`;
  }

  container.innerHTML = html;
}

/* 옵션 추가 (버튼 클릭) */
function addP2ColOption(col) {
  renderP2ColOptions(col, true);
}

/* 입력 확정 */
function confirmP2ColOption(col) {
  const name = (document.getElementById('p2c-newname-' + col)?.value || '').trim();
  const type = document.getElementById('p2c-newtype-' + col)?.value || 'pct_deduct';
  const val  = parseFloat(document.getElementById('p2c-newval-' + col)?.value || '0');
  if (!name || Number.isNaN(val) || val < 0) return;
  _p2ColData[col] = _p2ColData[col] || { opts: [] };
  _p2ColData[col].opts.push({ id: 'o' + Date.now(), name, type, value: val });
  renderP2ColOptions(col, false);
  recalcP2Col(col);
}

/* 옵션 삭제 */
function removeP2ColOption(col, optId) {
  if (!_p2ColData[col]) return;
  _p2ColData[col].opts = _p2ColData[col].opts.filter(o => o.id !== optId);
  renderP2ColOptions(col, false);
  recalcP2Col(col);
}

/* 옵션 값 수정 */
function updateP2ColOption(col, optId, newVal) {
  if (!_p2ColData[col]) return;
  const opt = _p2ColData[col].opts.find(o => o.id === optId);
  if (opt) { opt.value = parseFloat(newVal) || 0; recalcP2Col(col); }
}

function _renderP2AiResult(data) {
  const extracted = data?.extracted || {};
  const rates = data?.exchange_rates || {};
  const resultSection = document.getElementById('p2-ai-result-section');
  if (resultSection) resultSection.style.display = '';

  // ── 듀얼 마켓 결과 저장 ──────────────────────────────────
  // 서버가 data.public / data.private 로 각 시장 결과를 전송.
  // 구버전 호환: data.analysis 폴백.
  _p2BothResults = {
    public:  data?.public  || data?.analysis || {},
    private: data?.private || data?.analysis || {},
    extracted,
    exchange_rates: rates,
  };

  // ── 공통 필드 업데이트 ────────────────────────────────────
  _setText('p2r-product-name', extracted.product_name || '미상');

  const verdictEl = document.getElementById('p2r-verdict-badge');
  if (verdictEl) {
    const v  = extracted.verdict || '미상';
    const vc = v === '적합' ? 'v-ok' : v === '부적합' ? 'v-err' : v !== '미상' ? 'v-warn' : 'v-none';
    verdictEl.className = `verdict-badge ${vc}`;
    verdictEl.textContent = v;
  }

  const refClpDisplay = extracted.ref_price_clp
    ? `CLP ${Number(extracted.ref_price_clp).toLocaleString('ko-KR')}`
    : (extracted.ref_price_usd ? `USD ${Number(extracted.ref_price_usd).toFixed(2)}` : '');
  _setText('p2r-ref-price-text', extracted.ref_price_text || refClpDisplay || '추출값 없음');

  const usdClpRate = rates.usd_clp || (rates.clp_usd ? 1 / rates.clp_usd : 0);
  const usdKrwRate = rates.usd_krw;
  let rateText = '환율 정보 없음';
  if (usdClpRate) {
    rateText = `1 USD = ${Number(usdClpRate).toFixed(2)} CLP`;
    if (usdKrwRate) rateText += ` / ${Number(usdKrwRate).toFixed(2)} KRW`;
  }
  _setText('p2r-exchange', rateText);

  // PDF 다운로드
  const dlState = document.getElementById('p2-report-dl-state');
  if (dlState) {
    if (data?.pdf) {
      dlState.innerHTML = `
        <a class="btn-download"
           href="/api/report/download?name=${encodeURIComponent(data.pdf)}"
           target="_blank">📄 수출가격전략 보고서 다운로드</a>`;
    } else {
      dlState.innerHTML = `<span style="font-size:13px;color:var(--red);">PDF 생성에 실패했습니다.</span>`;
    }
  }

  // 제품 목록
  const prodList = document.getElementById('p2-product-list');
  if (prodList && extracted.product_name) {
    prodList.innerHTML = `
      <table class="p2-prod-table">
        <thead><tr><th>제품</th><th>참조가 (원문)</th><th>출처</th></tr></thead>
        <tbody>
          <tr>
            <td>${_escHtml(extracted.product_name || '—')}</td>
            <td>${_escHtml(extracted.ref_price_text || '—')}</td>
            <td>report</td>
          </tr>
        </tbody>
      </table>`;
  }

  // ── 현재 탭 세그먼트 렌더 ────────────────────────────────
  _renderP2ForSeg(_p2AiSeg);
}

/**
 * 저장된 _p2BothResults 에서 특정 시장(seg)의 데이터를 읽어 UI를 갱신.
 * setP2AiSeg() 탭 전환 시에도 호출된다.
 * @param {'public'|'private'} seg
 */
function _renderP2ForSeg(seg) {
  if (!_p2BothResults) return;
  const marketData = _p2BothResults[seg] || _p2BothResults.public || {};
  const extracted  = _p2BothResults.extracted || {};
  const rates      = _p2BothResults.exchange_rates || {};
  const scenarios  = Array.isArray(marketData.scenarios) ? marketData.scenarios : [];

  const usdClp = rates.usd_clp ? Number(rates.usd_clp) : (rates.clp_usd ? 1 / Number(rates.clp_usd) : 932);
  const usdKrw = rates.usd_krw ? Number(rates.usd_krw) : 1393;

  // analysis 는 중첩 구조: marketData.analysis (Haiku 응답) 또는 marketData 직접 (폴백)
  const analysis = marketData.analysis || marketData;

  // 최종 권고가
  const finalClp = analysis.final_price_clp || marketData.final_price_clp || 0;
  const finalUsd = analysis.final_price_usd || marketData.final_price_usd || 0;
  _setText('p2r-final-price', finalClp > 0
    ? `CLP ${Number(finalClp).toLocaleString('ko-KR')} ≈ FOB USD ${Number(finalUsd).toFixed(2)}`
    : `FOB USD ${Number(finalUsd).toFixed(2)}`);

  // 산정 이유
  _setText('p2r-rationale', analysis.rationale || marketData.rationale || '산정 이유 없음');

  // 시나리오 목록 (#p2r-scenarios)
  const scenEl = document.getElementById('p2r-scenarios');
  if (scenEl) {
    const tierKeys = ['agg', 'avg', 'cons'];
    if (scenarios.length) {
      scenEl.innerHTML = scenarios.map((s, idx) => {
        const cls      = tierKeys[idx] || 'cons';
        const label    = _FOB_TIER_LABELS[cls] || String(s.name || `시나리오 ${idx + 1}`);
        const clpVal   = Number(s.price_clp || 0);
        const usdVal   = Number(s.price_usd || 0);
        const priceDisp = clpVal > 0
          ? `CLP ${clpVal.toLocaleString('ko-KR')} ≈ USD ${usdVal.toFixed(2)}`
          : `USD ${usdVal.toFixed(2)}`;
        return `
          <div class="p2-scenario p2-scenario--${cls}">
            <div class="p2-scenario-top">
              <span class="p2-scenario-name">${_escHtml(label)}</span>
              <span class="p2-scenario-price">${priceDisp}</span>
            </div>
          </div>`;
      }).join('');
    } else {
      scenEl.innerHTML = '<div class="p2-note">시나리오 데이터가 없습니다.</div>';
    }
  }

  // ── 3열 가격 카드 채우기 ──────────────────────────────────
  const cols = ['agg', 'avg', 'cons'];
  scenarios.forEach((s, i) => {
    const col = cols[i];
    if (!col) return;
    const priceUsd = Number(s.price_usd || 0);
    const priceClp = Number(s.price_clp || (priceUsd * usdClp));
    _p2ScenarioRaw[col]    = priceUsd;
    _p2ScenarioRaw.sgd_usd = 1;
    _p2ScenarioRaw.sgd_krw = usdKrw;

    const refBase  = extracted.ref_price_clp != null ? Number(extracted.ref_price_clp) : 0;
    const refLabel = refBase > 0
      ? `Retail base: CLP ${(refBase * (i === 0 ? 0.85 : i === 1 ? 1.0 : 1.15)).toLocaleString('ko-KR')}`
      : `Retail base: — CLP`;

    const priceEl   = document.getElementById('p2c-price-' + col);
    const subEl     = document.getElementById('p2c-sub-' + col);
    const refEl     = document.getElementById('p2c-ref-' + col);
    const baseInput = document.getElementById('p2ci-base-' + col);

    if (refEl)     refEl.textContent   = refLabel;
    if (priceEl)   priceEl.textContent = priceUsd.toFixed(2);
    if (baseInput) baseInput.value     = priceUsd.toFixed(2);
    if (subEl) {
      const krw     = usdKrw > 0 ? Math.round(priceUsd * usdKrw).toLocaleString('ko-KR') : '—';
      const clpDisp = priceClp > 0 ? `CLP ${Math.round(priceClp).toLocaleString('ko-KR')}` : '—';
      subEl.textContent = `${clpDisp} · ${krw} KRW`;
    }
    _p2ColData[col] = { opts: [] };
    renderP2ColOptions(col, false);
  });

  // ── 가격 요약 테이블 ──────────────────────────────────────
  const tableBody = document.getElementById('p2-price-table-body');
  if (tableBody) {
    const labels  = ['저가 진입', '기준가', '프리미엄'];
    const classes = ['badge-agg', 'badge-avg', 'badge-cons'];
    tableBody.innerHTML = scenarios.slice(0, 3).map((s, i) => {
      const pUsd = Number(s.price_usd || 0);
      const pClp = Number(s.price_clp || (pUsd * usdClp));
      const pKrw = usdKrw > 0 ? Math.round(pUsd * usdKrw) : 0;
      return `<tr>
        <td class="col-scenario ${classes[i]}">${labels[i]}</td>
        <td class="col-clp">CLP ${pClp > 0 ? Math.round(pClp).toLocaleString('ko-KR') : '—'}</td>
        <td class="col-usd">$ ${pUsd > 0 ? pUsd.toFixed(2) : '—'}</td>
        <td class="col-krw">${pKrw > 0 ? pKrw.toLocaleString('ko-KR') + '원' : '—'}</td>
      </tr>`;
    }).join('');
  }

  // ── FOB 분포 ──────────────────────────────────────────────
  if (scenarios.length >= 3) {
    const prices = scenarios.map(s => Number(s.price_usd || 0)).sort((a, b) => a - b);
    _setText('p2-dist-p25', `USD ${prices[0].toFixed(2)}`);
    _setText('p2-dist-med', `USD ${prices[1].toFixed(2)}`);
    _setText('p2-dist-p75', `USD ${prices[2].toFixed(2)}`);
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §P2-FOB. FOB 역산 계산기 모달
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/** AI 기본 공식 요소 (서버 _CL_FOB_ELEMENTS_* 와 동일) */
const _FOB_DEFAULTS = {
  public: [
    { key: 'agent_fee',       label: '에이전트 수수료',    value: 5,   unit: '%',   type: 'pct_deduct' },
    { key: 'freight',         label: '운임 배수',          value: 1.0, unit: '×배수', type: 'mult' },
    { key: 'procurement_fee', label: '조달청 입찰 수수료', value: 3,   unit: '%',   type: 'pct_deduct' },
    { key: 'gpo_discount',    label: 'GPO 물량 할인율',    value: 2,   unit: '%',   type: 'pct_deduct' },
  ],
  private: [
    { key: 'agent_fee',          label: '에이전트 수수료',      value: 5,   unit: '%',   type: 'pct_deduct' },
    { key: 'freight',            label: '운임 배수',            value: 1.0, unit: '×배수', type: 'mult' },
    { key: 'pharma_margin',      label: '병원·약국 유통 마진',  value: 15,  unit: '%',   type: 'pct_deduct' },
    { key: 'distributor_markup', label: '유통사 마크업',        value: 8,   unit: '%',   type: 'pct_add' },
  ],
};

/**
 * 공식 요소 목록의 combined factor 계산.
 * base_usd × product = final FOB USD
 */
function _elementsProduct(elements) {
  let p = 1.0;
  for (const e of elements) {
    const v = Number(e.value) || 0;
    if      (e.type === 'pct_deduct') p *= (1 - v / 100);
    else if (e.type === 'pct_add')   p *= (1 + v / 100);
    else if (e.type === 'mult')      p *= v;
  }
  return p;
}

/**
 * FOB 역산 모달 열기.
 * @param {'agg'|'avg'|'cons'} tier
 */
function openFobModal(tier) {
  if (!_p2BothResults) {
    alert('먼저 AI 가격 산출을 실행해 주세요.');
    return;
  }
  const seg        = _p2AiSeg;
  const marketData = _p2BothResults[seg] || {};
  const scenarios  = Array.isArray(marketData.scenarios) ? marketData.scenarios : [];
  const idx        = _FOB_TIER_IDX[tier];
  if (idx === undefined || idx >= scenarios.length) return;

  const s      = scenarios[idx];
  const rates  = _p2BothResults.exchange_rates || {};
  const extr   = _p2BothResults.extracted || {};
  const usdClp = rates.usd_clp ? Number(rates.usd_clp) : (rates.clp_usd ? 1 / Number(rates.clp_usd) : 932);
  const usdKrw = rates.usd_krw ? Number(rates.usd_krw) : 1393;

  // 공식 요소 딥카피
  const aiDefs   = (_FOB_DEFAULTS[seg] || _FOB_DEFAULTS.public).map(e => ({ ...e }));
  const elements = aiDefs.map(e => ({ ...e }));

  // base_usd: price_usd ÷ elements_product (역산)
  const priceUsd = Number(s.price_usd || 0);
  const product  = _elementsProduct(elements);
  const baseUsd  = product > 0 ? priceUsd / product : priceUsd;

  _fobModalState = {
    tier,
    seg,
    baseUsd,
    elements,
    aiDefaults: aiDefs,
    refClp:  extr.ref_price_clp  ? Number(extr.ref_price_clp)  : null,
    refUsd:  extr.ref_price_usd  ? Number(extr.ref_price_usd)  : null,
    usdClp,
    usdKrw,
  };

  // 타이틀: "기준가 — 역산 · 옵션 편집 [공공 시장]"
  const _segLabel = seg === 'public' ? '공공 시장' : '민간 시장';
  const titleEl = document.getElementById('fob-modal-title');
  if (titleEl) titleEl.textContent = `${_FOB_TIER_LABELS[tier] || tier} — 역산 · 옵션 편집 [${_segLabel}]`;

  // 보고서 참조가: "CLP 12,345 ≈ 13.27 USD" (현지 통화 선행)
  const refEl = document.getElementById('fob-ref-display');
  if (refEl) {
    const rc = _fobModalState.refClp;
    const ru = _fobModalState.refUsd;
    const _usdClp = _fobModalState.usdClp || 932;
    if (rc && rc > 0) {
      const usdEquiv = ru || (rc / _usdClp);
      refEl.textContent = `CLP ${Number(rc).toLocaleString('ko-KR')} ≈ ${usdEquiv.toFixed(2)} USD`;
    } else if (ru && ru > 0) {
      const clpEquiv = ru * _usdClp;
      refEl.textContent = `CLP ${Math.round(clpEquiv).toLocaleString('ko-KR')} ≈ ${Number(ru).toFixed(2)} USD`;
    } else {
      refEl.textContent = '—';
    }
  }

  // 기준가 입력
  const baseInput = document.getElementById('fob-base-usd');
  if (baseInput) baseInput.value = baseUsd.toFixed(4);

  renderFobElements();
  recalcFobModal();

  document.getElementById('fob-modal-overlay').style.display = 'flex';
  document.body.style.overflow = 'hidden';
}

/** FOB 모달 닫기 (오버레이 클릭 또는 ✕ 버튼) */
function closeFobModal(e) {
  if (e && e.target !== document.getElementById('fob-modal-overlay')) return;
  document.getElementById('fob-modal-overlay').style.display = 'none';
  document.body.style.overflow = '';
}

/** 공식 요소 목록을 #fob-elements-list 에 렌더링 */
function renderFobElements() {
  if (!_fobModalState) return;
  const container = document.getElementById('fob-elements-list');
  if (!container) return;

  container.innerHTML = _fobModalState.elements.map((e, i) => `
    <div class="fob-elem-row">
      <input class="fob-elem-name" type="text" value="${_escHtml(String(e.label))}"
        oninput="updateFobElement(${i},'label',this.value)">
      <select class="fob-elem-type" onchange="updateFobElement(${i},'type',this.value)">
        <option value="pct_deduct" ${e.type === 'pct_deduct' ? 'selected' : ''}>% 차감</option>
        <option value="pct_add"    ${e.type === 'pct_add'    ? 'selected' : ''}>% 가산</option>
        <option value="mult"       ${e.type === 'mult'       ? 'selected' : ''}>× 배수</option>
      </select>
      <input class="fob-elem-val" type="number" value="${e.value}" step="0.1" min="0"
        oninput="updateFobElement(${i},'value',this.value)">
      <button class="fob-elem-del" onclick="removeFobElement(${i})">×</button>
    </div>`).join('');
}

/**
 * 요소 필드 수정.
 * @param {number} idx
 * @param {'label'|'type'|'value'} field
 * @param {string} val
 */
function updateFobElement(idx, field, val) {
  if (!_fobModalState) return;
  const e = _fobModalState.elements[idx];
  if (!e) return;
  if (field === 'value') e.value = parseFloat(val) || 0;
  else e[field] = val;
  recalcFobModal();
}

/**
 * 요소 삭제.
 * @param {number} idx
 */
function removeFobElement(idx) {
  if (!_fobModalState) return;
  _fobModalState.elements.splice(idx, 1);
  renderFobElements();
  recalcFobModal();
}

/** 새 요소 추가 행 확정 */
function confirmFobElement() {
  if (!_fobModalState) return;
  const name = (document.getElementById('fob-new-name')?.value || '').trim();
  const type = document.getElementById('fob-new-type')?.value || 'pct_deduct';
  const val  = parseFloat(document.getElementById('fob-new-val')?.value || '0');
  if (!name || Number.isNaN(val) || val < 0) return;

  _fobModalState.elements.push({
    key:   'custom_' + Date.now(),
    label: name,
    value: val,
    unit:  type === 'mult' ? '×배수' : '%',
    type,
  });

  const nameEl = document.getElementById('fob-new-name');
  const valEl  = document.getElementById('fob-new-val');
  if (nameEl) nameEl.value = '';
  if (valEl)  valEl.value  = '';

  renderFobElements();
  recalcFobModal();
}

/** AI 추천 기본값으로 초기화 */
function resetFobToAi() {
  if (!_fobModalState) return;
  _fobModalState.elements = _fobModalState.aiDefaults.map(e => ({ ...e }));

  // base_usd 도 AI 역산값으로 복원
  const seg        = _fobModalState.seg;
  const tier       = _fobModalState.tier;
  const marketData = _p2BothResults?.[seg] || {};
  const scenarios  = Array.isArray(marketData.scenarios) ? marketData.scenarios : [];
  const s          = scenarios[_FOB_TIER_IDX[tier]];
  if (s) {
    const priceUsd = Number(s.price_usd || 0);
    const product  = _elementsProduct(_fobModalState.elements);
    _fobModalState.baseUsd = product > 0 ? priceUsd / product : priceUsd;
    const baseInput = document.getElementById('fob-base-usd');
    if (baseInput) baseInput.value = _fobModalState.baseUsd.toFixed(4);
  }

  renderFobElements();
  recalcFobModal();
}

/** 기준가 또는 요소 변경 시 결과 재계산 */
function recalcFobModal() {
  if (!_fobModalState) return;
  const baseUsd = parseFloat(document.getElementById('fob-base-usd')?.value || '0') || 0;
  _fobModalState.baseUsd = baseUsd;

  const product   = _elementsProduct(_fobModalState.elements);
  const resultUsd = baseUsd * product;
  const resultClp = resultUsd * (_fobModalState.usdClp || 932);

  // 모달 결과 라인 업데이트
  const resultEl = document.getElementById('fob-result-line');
  if (resultEl) {
    resultEl.textContent =
      `결과: USD ${resultUsd.toFixed(4)} · CLP ${Math.round(resultClp).toLocaleString('ko-KR')}`;
  }

  // 해당 가격 카드 실시간 반영
  const col     = _fobModalState.tier;
  const priceEl = document.getElementById('p2c-price-' + col);
  const subEl   = document.getElementById('p2c-sub-' + col);
  if (priceEl) priceEl.textContent = resultUsd.toFixed(2);
  if (subEl) {
    const krw     = _fobModalState.usdKrw > 0
      ? Math.round(resultUsd * _fobModalState.usdKrw).toLocaleString('ko-KR')
      : '—';
    subEl.textContent = `CLP ${Math.round(resultClp).toLocaleString('ko-KR')} · ${krw} KRW`;
  }
}

function _p2FillExchangeRate() {
  const rates = window._exchangeRates;
  if (!rates) return;
  const usdClp = Number(rates.usd_clp || (rates.clp_usd ? (1 / rates.clp_usd) : 0));
  if (!usdClp || usdClp <= 0) return;
  ['public', 'private'].forEach((seg) => {
    const opt = _p2Manual[seg].find((x) => x.key === 'exchange');
    if (opt) opt.value = Number(usdClp.toFixed(2));
  });
}

function _p2FillBaseFromReport() {
  const report = _getP2SelectedReport();
  if (!report) return;
  // 1순위: 저장된 CLP 힌트
  const clpHint = _extractClpHint(report.price_hint || '');
  if (!Number.isNaN(clpHint) && clpHint > 0) {
    const pub = _p2Manual.public.find((x) => x.key === 'base_price');
    const pri = _p2Manual.private.find((x) => x.key === 'base_het');
    if (pub) pub.value = clpHint;
    if (pri) pri.value = clpHint;
  }
}

function _syncP2ReportsOptions() {
  if (!_p2Ready) return;
  const reports = _loadReports();

  // 옵션 라벨: "시장조사 보고서 · ProductName · YYYY-MM-DD HH:MM"
  function _reportLabel(r) {
    const title  = r.stage_label || '시장조사 보고서';
    const prod   = r.product || '알 수 없음';
    const ts     = r.created_at
      ? new Date(r.created_at).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
      : '';
    return ts ? `${title} · ${prod} · ${ts}` : `${title} · ${prod}`;
  }

  const optionHtml = ['<option value="">시장조사 보고서를 선택하세요.</option>']
    .concat(reports.map((r) => `<option value="${r.id}">${_escHtml(_reportLabel(r))}</option>`))
    .join('');

  const manualSelect = document.getElementById('p2-report-select');
  if (manualSelect) {
    const curr = _p2SelectedReportId;
    manualSelect.innerHTML = optionHtml;
    _p2SelectedReportId = reports.some((r) => String(r.id) === String(curr)) ? curr : '';
    manualSelect.value = _p2SelectedReportId;
  }

  const aiSelect = document.getElementById('p2-ai-report-select');
  if (aiSelect) {
    const curr = _p2AiSelectedReportId;
    aiSelect.innerHTML = optionHtml;
    _p2AiSelectedReportId = reports.some((r) => String(r.id) === String(curr)) ? curr : '';
    aiSelect.value = _p2AiSelectedReportId;
  }

  // [02] 바이어 발굴 패널 — p3-report-select 동기화
  const p3Select = document.getElementById('p3-report-select');
  if (p3Select) {
    const curr = p3Select.value;
    p3Select.innerHTML = optionHtml;
    p3Select.value = reports.some((r) => String(r.id) === String(curr)) ? curr : '';
  }
}

function _getP2SelectedReport() {
  if (!_p2SelectedReportId) return null;
  return _loadReports().find((r) => String(r.id) === String(_p2SelectedReportId)) || null;
}

function _extractClpHint(text) {
  const src = String(text || '');
  // CLP 범위 (예: CLP 4,500~5,200 또는 CLP 4500-5200)
  const mRange = src.match(/CLP\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*[~\-–]\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i);
  if (mRange) return (Number(mRange[1].replace(/,/g, '')) + Number(mRange[2].replace(/,/g, ''))) / 2;
  // 단일 CLP 값
  const mSingle = src.match(/CLP\s*([0-9][0-9,]*(?:\.[0-9]+)?)/i);
  if (mSingle) return Number(mSingle[1].replace(/,/g, ''));
  // USD 값 → 폴백 (CLP 환산은 하지 않고 0 반환)
  return NaN;
}

// 하위 호환용 (SGD 패턴도 유지)
function _extractSgdHint(text) {
  return _extractClpHint(text);
}

function _calcP2Manual() {
  const seg = _p2ManualSeg;
  const options = _p2Manual[seg].filter((x) => x.enabled);
  const usdClp = Number(_p2Manual[seg].find((x) => x.key === 'exchange')?.value || 932);

  if (seg === 'public') {
    const base = Number(options.find((x) => x.key === 'base_price')?.value || 0);  // CLP
    const ratio = Number(options.find((x) => x.key === 'pub_ratio')?.value || 35);
    // CLP 낙찰가 × FOB비율 ÷ 환율 = FOB USD
    let priceClp = base * (ratio / 100);
    const parts = [`CLP ${base.toLocaleString('ko-KR')}`, `× ${ratio}%`];
    options.forEach((opt) => {
      if (opt.type === 'pct_add_custom') {
        priceClp *= (1 + Number(opt.value) / 100);
        parts.push(`× (1+${Number(opt.value).toFixed(1)}%)`);
      } else if (opt.type === 'abs_add_custom') {
        priceClp += Number(opt.value);
        parts.push(`+ CLP ${Number(opt.value).toLocaleString('ko-KR')}`);
      }
    });
    const priceUsd = usdClp > 0 ? Math.max(priceClp, 0) / usdClp : 0;
    const kupStr = `CLP ${Math.max(priceClp, 0).toLocaleString('ko-KR')} ≈ FOB USD ${priceUsd.toFixed(2)}`;
    return { kup: Math.max(priceUsd, 0), kupClp: Math.max(priceClp, 0), formulaStr: `${parts.join('  ')}  ÷ ${usdClp.toFixed(0)} CLP  =  ${kupStr}` };
  }

  // 민간 시장: CLP 소매가에서 역산 → FOB USD
  let priceClp = 0;
  const parts = [];
  options.forEach((opt) => {
    if (opt.key === 'base_het') {
      priceClp = Number(opt.value);
      parts.push(`CLP ${priceClp.toLocaleString('ko-KR')}`);
    } else if (opt.type === 'gst_fixed') {
      // IVA 19% 공제
      priceClp /= 1.19;
      parts.push('÷ 1.19 (IVA)');
    } else if (opt.type === 'pct_deduct') {
      priceClp *= (1 - Number(opt.value) / 100);
      parts.push(`× (1−${Number(opt.value).toFixed(1)}%)`);
    } else if (opt.type === 'pct_add_custom') {
      priceClp *= (1 + Number(opt.value) / 100);
      parts.push(`× (1+${Number(opt.value).toFixed(1)}%)`);
    } else if (opt.type === 'abs_add_custom') {
      priceClp += Number(opt.value);
      parts.push(`+ CLP ${Number(opt.value).toLocaleString('ko-KR')}`);
    }
    // exchange key는 표시용이므로 계산에서 제외 (usdClp으로 최종 변환)
  });
  const priceUsd = usdClp > 0 ? Math.max(priceClp, 0) / usdClp : 0;
  const kupStr = `CLP ${Math.max(priceClp, 0).toLocaleString('ko-KR')} ≈ FOB USD ${priceUsd.toFixed(2)}`;
  return { kup: Math.max(priceUsd, 0), kupClp: Math.max(priceClp, 0), formulaStr: `${(parts.join('  ') || 'CLP 0')}  ÷ ${usdClp.toFixed(0)} CLP  =  ${kupStr}` };
}

function _renderP2Manual() {
  const wrapEl    = document.getElementById('p2-manual-options');
  const removedEl = document.getElementById('p2-manual-removed');
  if (!wrapEl || !removedEl) return;

  const options = _p2Manual[_p2ManualSeg];
  const active  = options.filter((x) => x.enabled);
  const inactive = options.filter((x) => !x.enabled);
  wrapEl.innerHTML = active.map((opt) => _p2OptionCardHtml(opt)).join('');
  _bindP2OptionEvents(wrapEl, options);

  removedEl.innerHTML = inactive.length
    ? `<span class="p2-removed-label">복원:</span>${inactive.map((opt) => `<button class="p2-add-btn" data-p2-op="add" data-key="${_escHtml(opt.key)}" type="button">+ ${_escHtml(opt.label)}</button>`).join('')}`
    : '';
  removedEl.querySelectorAll('[data-p2-op="add"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const item = options.find((x) => x.key === btn.getAttribute('data-key'));
      if (item) { item.enabled = true; _renderP2Manual(); }
    });
  });

  _renderP2CustomAddSection();

  const calc = _calcP2Manual();
  const agg  = calc.kup * 0.9;
  const avg  = calc.kup;
  const cons = calc.kup * 1.1;
  const aggClp  = (calc.kupClp || 0) * 0.9;
  const avgClp  = (calc.kupClp || 0);
  const consClp = (calc.kupClp || 0) * 1.1;
  const aggReason  = _p2ManualScenarioReason('aggressive',   _p2ManualSeg);
  const avgReason  = _p2ManualScenarioReason('average',      _p2ManualSeg);
  const consReason = _p2ManualScenarioReason('conservative', _p2ManualSeg);
  const aggFormula  = `FOB USD ${calc.kup.toFixed(2)} × 0.90 = USD ${agg.toFixed(2)} (≈ CLP ${aggClp.toLocaleString('ko-KR')})`;
  const avgFormula  = `FOB USD ${avg.toFixed(2)} (기준가 그대로 ≈ CLP ${avgClp.toLocaleString('ko-KR')})`;
  const consFormula = `FOB USD ${calc.kup.toFixed(2)} × 1.10 = USD ${cons.toFixed(2)} (≈ CLP ${consClp.toLocaleString('ko-KR')})`;
  _p2LastScenarios = { mode: 'manual', seg: _p2ManualSeg, base: calc.kup, agg, avg, cons, formulaStr: calc.formulaStr, aggReason, avgReason, consReason, aggFormula, avgFormula, consFormula, rationaleLines: [] };
}

function _p2OptionCardHtml(opt) {
  const isFixed = opt.type === 'gst_fixed';

  // 입력 필드 값 포맷
  const inputVal = opt.unit === 'rate' ? Number(opt.value).toFixed(4)
                 : opt.unit === '%'    ? Number(opt.value).toFixed(0)
                 :                       Number(opt.value).toFixed(2);
  // 단위 표시
  const unitLabel = opt.unit === '%' ? '%' : opt.unit === 'rate' ? '' : (opt.unit === 'CLP' ? 'CLP' : opt.unit || '');

  return `
    <div class="p2-step-card">
      <div class="p2-step-header">
        <button class="p2-step-toggle" data-p2-op="toggle" data-key="${_escHtml(opt.key)}" type="button">
          <span class="p2-step-label-text">${_escHtml(opt.label)}</span>
          <span class="p2-step-arrow">${opt.expanded ? '▾' : '▸'}</span>
        </button>
        <div class="p2-step-controls">
          ${isFixed
            ? `<span class="p2-step-val-display">÷ 1.09 고정</span>`
            : `${unitLabel ? `<span class="p2-step-unit-label" style="font-size:12px;color:var(--muted);margin-right:2px;">${_escHtml(unitLabel)}</span>` : ''}
               <input class="p2-step-input" type="number" data-p2-op="input" data-key="${_escHtml(opt.key)}" value="${inputVal}" step="${opt.step}" min="${opt.min}">`
          }
          ${opt.fixed ? '' : `<button class="p2-del-btn" data-p2-op="del" data-key="${_escHtml(opt.key)}" type="button" title="옵션 제거">×</button>`}
        </div>
      </div>
      ${opt.expanded ? `<div class="p2-step-body"><div class="p2-step-hint">${_escHtml(opt.hint || '')}</div><div class="p2-step-rationale">${_escHtml(opt.rationale || '')}</div></div>` : ''}
    </div>`;
}

function _bindP2OptionEvents(wrap, options) {
  wrap.querySelectorAll('[data-p2-op]').forEach((el) => {
    const op = el.getAttribute('data-p2-op');
    const key = el.getAttribute('data-key');
    const item = options.find((x) => x.key === key);
    if (!item) return;

    if (op === 'toggle') {
      el.addEventListener('click', () => {
        item.expanded = !item.expanded;
        _renderP2Manual();
      });
    } else if (op === 'del') {
      el.addEventListener('click', () => {
        item.enabled = false;
        item.expanded = false;
        _renderP2Manual();
      });
    } else if (op === 'input') {
      el.addEventListener('input', () => {
        const v = parseFloat(el.value);
        if (!Number.isNaN(v)) item.value = Math.max(item.min, v);
        _renderP2Manual();
      });
    }
  });
}

function _renderP2CustomAddSection() {
  const section = document.getElementById('p2-custom-add-section');
  if (!section) return;
  section.innerHTML = `
    <div class="p2-custom-add-row">
      <input class="p2-custom-input" id="p2c-label" type="text" placeholder="옵션명" maxlength="30" style="flex:2">
      <select class="p2-custom-type-select" id="p2c-type">
        <option value="pct_deduct">% 차감</option>
        <option value="pct_add_custom">% 가산</option>
        <option value="abs_add_custom">CLP 가산</option>
      </select>
      <input class="p2-custom-input" id="p2c-val" type="number" placeholder="값" step="0.1" min="0" max="999" style="width:80px;flex:0 0 80px">
      <button class="p2-add-custom-btn" id="p2c-add" type="button">+ 추가</button>
    </div>`;
  document.getElementById('p2c-add')?.addEventListener('click', () => {
    const label = (document.getElementById('p2c-label')?.value || '').trim();
    const type = document.getElementById('p2c-type')?.value || 'pct_deduct';
    const val = parseFloat(document.getElementById('p2c-val')?.value || '0');
    if (!label || Number.isNaN(val) || val < 0) return;
    _p2Manual[_p2ManualSeg].push({
      key: `custom_${Date.now()}`,
      label,
      value: val,
      type,
      unit: type === 'abs_add_custom' ? 'CLP' : '%',
      step: type === 'abs_add_custom' ? 0.1 : 1,
      min: 0,
      max: type === 'abs_add_custom' ? 9999 : 100,
      enabled: true,
      fixed: false,
      expanded: false,
      hint: '사용자 추가 옵션',
      rationale: '',
    });
    _resetP2ManualResultView();
    _renderP2Manual();
  });
}

function _p2ManualScenarioReason(type, seg) {
  if (type === 'aggressive') {
    return seg === 'public'
      ? '저마진 포지셔닝 — Mercado Público 입찰 초기, 낮은 FOB 가격으로 낙찰 가능성을 높이고 CENABAST 공급 채널 진입을 우선시합니다.'
      : '저마진 포지셔닝 — Cruz Verde/Salcobrand 초기 입고를 위해 가격경쟁력을 앞세우며 처방 채널 확보와 빠른 시장 침투를 목표로 합니다.';
  }
  if (type === 'average') {
    return '중간 포지셔닝 — 현재 입력 옵션을 그대로 반영한 기본 FOB USD 산정가입니다. IVA 역산·관세·마진 균형을 유지하는 표준 전략입니다.';
  }
  return seg === 'public'
    ? '고마진 포지셔닝 — CENABAST 또는 Mercado Público 채널에 자리잡은 이후 마진율을 높여 지속 수익성을 확보하는 전략입니다.'
    : '고마진 포지셔닝 — 칠레 민간 약국 체인에 안착한 후 브랜드 프리미엄을 활용해 마진율을 높이는 단계적 가격 전략입니다.';
}

async function _generateP2Pdf() {
  const btn = document.getElementById('p2-pdf-btn-manual');
  const stateEl = document.getElementById('p2-pdf-state-manual');
  const sc = _p2LastScenarios;
  if (!sc) {
    if (stateEl) stateEl.textContent = '먼저 시나리오를 산정해 주세요.';
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = '생성 중…';
  }
  if (stateEl) stateEl.textContent = '';

  try {
    const report = _getP2SelectedReport();
    const body = {
      product_name: report ? (report.report_title || report.product || '제품명 미상') : '제품명 미상',
      verdict: report ? (report.verdict || '—') : '—',
      seg_label: sc.seg === 'public' ? '공공 시장' : '민간 시장',
      base_price: sc.base,
      formula_str: sc.formulaStr,
      mode_label: '직접 입력',
      scenarios: [
        { label: '공격', price: sc.agg,  reason: sc.aggReason  || '', formula: sc.aggFormula  || '' },
        { label: '평균', price: sc.avg,  reason: sc.avgReason  || '', formula: sc.avgFormula  || '' },
        { label: '보수', price: sc.cons, reason: sc.consReason || '', formula: sc.consFormula || '' },
      ],
      ai_rationale: [],
    };
    const res = await fetch('/api/p2/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.pdf) throw new Error(data.detail || `HTTP ${res.status}`);
    if (stateEl) {
      stateEl.innerHTML = `<a class="btn-download" href="/api/report/download?name=${encodeURIComponent(data.pdf)}" target="_blank" style="font-size:12px;padding:6px 14px;">다운로드</a>`;
    }
  } catch (err) {
    if (stateEl) stateEl.textContent = `생성 실패: ${err.message}`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'PDF 생성';
    }
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §7. API 키 상태 (U1) — GET /api/keys/status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadKeyStatus() {
  try {
    const res  = await fetch('/api/keys/status');
    const data = await res.json();
    _applyKeyBadge('key-claude',     data.claude,     'Claude',     'API 키 설정됨',  'API 키 미설정 — 분석 불가');
    _applyKeyBadge('key-perplexity', data.perplexity, 'Perplexity', 'API 키 설정됨',  '미설정 — 논문 검색 생략');
  } catch (_) { /* 조용히 실패 */ }
}

function _applyKeyBadge(id, active, label, okTitle, ngTitle) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'key-badge ' + (active ? 'active' : 'inactive');
  el.title     = active ? `${label} ${okTitle}` : `${label} ${ngTitle}`;
  const dot    = el.querySelector('.key-badge-dot');
  if (dot) dot.style.background = active ? 'var(--green)' : 'var(--muted)';
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §7. 진행 단계 표시 (B2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * @param {string} currentStep  STEP_ORDER 내 현재 단계
 * @param {'running'|'done'|'error'} status
 */
function setProgress(currentStep, status) {
  const row = document.getElementById('progress-row');
  if (row) row.classList.add('visible');
  const idx = STEP_ORDER.indexOf(currentStep);

  for (let i = 0; i < STEP_ORDER.length; i++) {
    const el  = document.getElementById('prog-' + STEP_ORDER[i]);
    if (!el) continue;
    const dot = el.querySelector('.prog-dot');

    if (status === 'error' && i === idx) {
      el.className    = 'prog-step error';
      dot.textContent = '✕';
    } else if (i < idx || (i === idx && status === 'done')) {
      el.className    = 'prog-step done';
      dot.textContent = '✓';
    } else if (i === idx) {
      el.className    = 'prog-step active';
      dot.textContent = i + 1;
    } else {
      el.className    = 'prog-step';
      dot.textContent = i + 1;
    }
  }
}

function resetProgress() {
  const row = document.getElementById('progress-row');
  if (row) row.classList.remove('visible');
  for (let i = 0; i < STEP_ORDER.length; i++) {
    const el = document.getElementById('prog-' + STEP_ORDER[i]);
    if (!el) continue;
    el.className = 'prog-step';
    el.querySelector('.prog-dot').textContent = i + 1;
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §8. 파이프라인 실행 & 폴링
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * 선택 품목 파이프라인 실행.
 * U6: 재분석 버튼도 이 함수를 호출.
 */
async function runPipeline() {
  const productKey = document.getElementById('product-select').value;
  _currentKey      = productKey;

  // UI 초기화
  resetProgress();
  _hideP1Note();
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('papers-card').classList.remove('visible');
  document.getElementById('report-card').classList.remove('visible');
  document.getElementById('btn-analyze').disabled = true;
  document.getElementById('btn-icon').textContent  = '⏳';

  const reBtn = document.getElementById('btn-reanalyze');
  if (reBtn) reBtn.style.display = 'none';

  // B2: db_load 단계 먼저 활성화
  setProgress('db_load', 'running');

  try {
    const res = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}`, { method: 'POST' });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      console.error('파이프라인 오류:', d.detail || res.status);
      setProgress('db_load', 'error');
      _resetBtn();
      return;
    }
    _pollTimer = setInterval(() => pollPipeline(productKey), 2500);
  } catch (e) {
    console.error('요청 실패:', e);
    setProgress('db_load', 'error');
    _resetBtn();
  }
}

function _resetBtn() {
  document.getElementById('btn-analyze').disabled = false;
  document.getElementById('btn-icon').textContent  = '▶';
}

/**
 * GET /api/pipeline/{product_key}/status 를 주기적으로 폴링.
 * 서버 step: init → db_load → analyze → refs → report → done
 */
async function pollPipeline(productKey) {
  try {
    const res = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}/status`);
    const d   = await res.json();

    if (d.status === 'idle') return;

    // B2: 서버 step → 프론트 STEP_ORDER 매핑
    if      (d.step === 'db_load')  { setProgress('db_load',  'running'); }
    else if (d.step === 'analyze')  { setProgress('db_load',  'done'); setProgress('analyze', 'running'); }
    else if (d.step === 'refs')     { setProgress('analyze',  'done'); setProgress('refs',    'running'); }
    else if (d.step === 'report')   {
      setProgress('refs', 'done'); setProgress('report', 'running');
      _showReportLoading();
    }

    if (d.status === 'done') {
      clearInterval(_pollTimer);
      for (const s of STEP_ORDER) setProgress(s, 'done');
      const r2   = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}/result`);
      const data = await r2.json();
      renderResult(data.result, data.refs, data.pdf);
      _resetBtn();
    }

    if (d.status === 'error') {
      clearInterval(_pollTimer);
      setProgress(STEP_ORDER.includes(d.step) ? d.step : 'analyze', 'error');
      _resetBtn();
    }
  } catch (_) { /* 조용히 재시도 */ }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §9. 신약 분석 파이프라인
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

let _customPollTimer = null;
const CUSTOM_STEP_ORDER = ['analyze', 'refs', 'report'];

function _setCustomProgress(step, status) {
  const row = document.getElementById('custom-progress-row');
  if (row) row.classList.add('visible');
  const idMap = { analyze: 'cprog-analyze', refs: 'cprog-refs', report: 'cprog-report' };
  const idx   = CUSTOM_STEP_ORDER.indexOf(step);

  CUSTOM_STEP_ORDER.forEach((s, i) => {
    const el  = document.getElementById(idMap[s]);
    if (!el) return;
    const dot = el.querySelector('.prog-dot');
    if (status === 'error' && i === idx) {
      el.className = 'prog-step error'; dot.textContent = '✕';
    } else if (i < idx || (i === idx && status === 'done')) {
      el.className = 'prog-step done';  dot.textContent = '✓';
    } else if (i === idx) {
      el.className = 'prog-step active'; dot.textContent = i + 1;
    } else {
      el.className = 'prog-step'; dot.textContent = i + 1;
    }
  });
}

function _resetCustomProgress() {
  const row = document.getElementById('custom-progress-row');
  if (row) row.classList.remove('visible');
  CUSTOM_STEP_ORDER.forEach((s, i) => {
    const el = document.getElementById('cprog-' + s);
    if (!el) return;
    el.className = 'prog-step';
    el.querySelector('.prog-dot').textContent = i + 1;
  });
}

function _resetCustomBtn() {
  document.getElementById('btn-custom').disabled = false;
  document.getElementById('custom-icon').textContent = '▶';
}

async function runCustomPipeline() {
  const tradeName = document.getElementById('custom-trade-name').value.trim();
  const inn       = document.getElementById('custom-inn').value.trim();
  const dosage    = document.getElementById('custom-dosage').value.trim();
  if (!tradeName || !inn) { alert('약품명과 성분명을 입력하세요.'); return; }

  _resetCustomProgress();
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('papers-card').classList.remove('visible');
  document.getElementById('report-card').classList.remove('visible');
  document.getElementById('btn-custom').disabled = true;
  document.getElementById('custom-icon').textContent = '⏳';

  if (_customPollTimer) clearInterval(_customPollTimer);
  _setCustomProgress('analyze', 'running');

  try {
    const res = await fetch('/api/pipeline/custom', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ trade_name: tradeName, inn, dosage_form: dosage }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      console.error('신약 분석 오류:', d.detail || res.status);
      _setCustomProgress('analyze', 'error');
      _resetCustomBtn();
      return;
    }
    _customPollTimer = setInterval(_pollCustomPipeline, 2500);
  } catch (e) {
    console.error('요청 실패:', e);
    _setCustomProgress('analyze', 'error');
    _resetCustomBtn();
  }
}

async function _pollCustomPipeline() {
  try {
    const res = await fetch('/api/pipeline/custom/status');
    const d   = await res.json();
    if (d.status === 'idle') return;

    if      (d.step === 'analyze') { _setCustomProgress('analyze', 'running'); }
    else if (d.step === 'refs')    { _setCustomProgress('analyze', 'done'); _setCustomProgress('refs', 'running'); }
    else if (d.step === 'report')  { _setCustomProgress('refs', 'done'); _setCustomProgress('report', 'running'); _showReportLoading(); }

    if (d.status === 'done') {
      clearInterval(_customPollTimer);
      for (const s of CUSTOM_STEP_ORDER) _setCustomProgress(s, 'done');
      const r2   = await fetch('/api/pipeline/custom/result');
      const data = await r2.json();
      renderResult(data.result, data.refs, data.pdf);
      _resetCustomBtn();
    }
    if (d.status === 'error') {
      clearInterval(_customPollTimer);
      _setCustomProgress(d.step || 'analyze', 'error');
      _resetCustomBtn();
    }
  } catch (_) { /* 조용히 재시도 */ }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §10. 결과 렌더링 (U2·U3·U4·U6·B4·N3·N4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * 분석 완료 후 결과·논문·PDF 카드를 화면에 렌더링.
 * @param {object|null} result  분석 결과
 * @param {Array}       refs    Perplexity 논문 목록
 * @param {string|null} pdfName PDF 파일명
 */
function renderResult(result, refs, pdfName) {

  /* ─ 분석 결과 카드 ─ */
  if (result) {
    if (result.error) {
      document.getElementById('verdict-badge').className   = 'verdict-badge v-err';
      document.getElementById('verdict-badge').textContent = '분석 데이터 오류';
      document.getElementById('verdict-name').textContent  = result.trade_name || result.product_id || '';
      document.getElementById('verdict-inn').textContent   = INN_MAP[result.product_id] || result.inn || '';
      _setText('basis-market-medical', String(result.error || '데이터 오류'));
      _setText('basis-regulatory',     '품목 메타/DB 매핑 확인 필요');
      _setText('basis-trade',          '재실행 후 동일하면 서버 로그 점검');
      _setText('basis-pbs-line',       '참고 가격 정보 없음');
      const pathEl = document.getElementById('entry-pathway');
      if (pathEl) {
        pathEl.textContent = '진입 채널 권고 데이터 확인 필요';
        pathEl.style.display = 'block';
        pathEl.classList.add('empty');
      }
      _setText('price-positioning-pbs', '가격 포지셔닝 데이터를 불러오지 못했습니다.');
      _setText('risks-conditions', '분석 데이터 소스 확인 후 재시도해 주세요.');
      _showP1Note('⚠️ 분석 데이터 오류 — 재시도하거나 서버 로그를 확인하세요.', true);
      _showReportError();
      return;
    }

    const verdict = result.verdict;
    const vc      = verdict === '적합'   ? 'v-ok'
                  : verdict === '부적합' ? 'v-err'
                  : verdict             ? 'v-warn'
                  :                       'v-none';
    const err    = result.analysis_error;
    const vLabel = verdict
      || (err === 'no_api_key'    ? 'API 키 미설정'
        : err === 'claude_failed' ? 'Claude 분석 실패'
        :                           '미분석');

    document.getElementById('verdict-badge').className   = `verdict-badge ${vc}`;
    document.getElementById('verdict-badge').textContent = vLabel;
    document.getElementById('verdict-name').textContent  = result.trade_name || result.product_id || '';
    document.getElementById('verdict-inn').textContent   = INN_MAP[result.product_id] || result.inn || '';

    // S2: 신호등
    ['tl-red', 'tl-yellow', 'tl-green'].forEach(id => {
      document.getElementById(id).classList.remove('on');
    });
    if (verdict === '적합')        document.getElementById('tl-green').classList.add('on');
    else if (verdict === '부적합') document.getElementById('tl-red').classList.add('on');
    else if (verdict)              document.getElementById('tl-yellow').classList.add('on');

    // S3: 판정 근거
    const basisFallback = _deriveBasisFromRationale(result.rationale);
    _setText('basis-market-medical', _formatDetailed(result.basis_market_medical || basisFallback.marketMedical));
    _setText('basis-regulatory',     _formatDetailed(result.basis_regulatory     || basisFallback.regulatory));
    _setText('basis-trade',          _formatDetailed(result.basis_trade          || basisFallback.trade));
    _setText('basis-pbs-line',       _pbsLineFromApi(result));

    // S4: 진입 채널
    const pathEl = document.getElementById('entry-pathway');
    if (pathEl) {
      const pathText = String(result.entry_pathway || '').trim();
      pathEl.textContent = pathText || '진입 채널 권고 데이터 확인 필요';
      pathEl.style.display = 'block';
      pathEl.classList.toggle('empty', !pathText);
    }

    const pbsPos = String(result.price_positioning_pbs || '').trim();
    _setText('price-positioning-pbs', _formatDetailed(pbsPos || _pbsLineFromApi(result)));

    const riskText = String(result.risks_conditions || '').trim()
      || (Array.isArray(result.key_factors) ? result.key_factors.join(' / ') : '');
    _setText('risks-conditions', _formatDetailed(riskText));

    // 완료 노트 표시 (result-card는 숨김 DOM이므로 visible 처리 안 함)
    _showP1Note(
      `✅ ${result.trade_name || '제품'} 분석 완료 — 판정: ${vLabel}. 상세 결과는 보고서 탭에서 확인하세요.`,
      false
    );
  }

  /* ─ B4: 논문 카드 ─ */
  const papersCard = document.getElementById('papers-card');
  const papersList = document.getElementById('papers-list');
  papersList.innerHTML = '';

  if (refs && refs.length > 0) {
    for (const ref of refs) {
      const item     = document.createElement('div');
      item.className = 'paper-item';
      const safeUrl  = /^https?:\/\//.test(ref.url || '') ? ref.url : '#';
      item.innerHTML = `
        <span class="paper-arrow">▸</span>
        <div>
          <div>
            <a class="paper-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer"></a>
            <span class="paper-src"></span>
          </div>
          <div class="paper-reason"></div>
        </div>`;
      item.querySelector('.paper-link').textContent   = ref.title || ref.url || '';
      item.querySelector('.paper-src').textContent    = ref.source ? `[${ref.source}]` : '';
      item.querySelector('.paper-reason').textContent = ref.reason || '';
      papersList.appendChild(item);
    }
    papersCard.classList.add('visible');
  } else {
    papersCard.classList.remove('visible');
  }

  /* ─ U4: PDF 보고서 카드 ─ */
  // N4: 보고서 탭에 자동 등록 (PDF 성공 여부 무관)
  _addReportEntry(result, pdfName);
  if (pdfName) {
    _showReportOk(pdfName);
    // N3: 보고서 완료 → Todo 자동 체크
    markTodoDone('rep');
  } else {
    _showReportError();
  }
}

/** U4: PDF 생성 중 */
function _showReportLoading() {
  const preview = document.getElementById('pdf-preview-frame');
  if (preview) preview.setAttribute('src', 'about:blank');
  document.getElementById('report-state-loading').style.display = 'flex';
  document.getElementById('report-state-ok').style.display      = 'none';
  document.getElementById('report-state-error').style.display   = 'none';
  document.getElementById('report-card').classList.add('visible');
}

/** 합본 보고서 (표지 + P1 + P2 + P3) 생성 */
async function generateCombinedReport() {
  const btn = document.getElementById('btn-combined-report');
  if (btn) { btn.disabled = true; btn.textContent = '생성 중…'; }
  try {
    const productSel = document.getElementById('product-select');
    const productKey = productSel?.value || _currentKey || '';
    const productName = productSel?.options[productSel.selectedIndex]?.text?.split('—')[0]?.trim()
                        || productKey || '칠레 수출 품목';
    const res = await fetch('/api/cl/report/combined', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        product_name:    productName,
        inn_label:       '',
        country:         '칠레',
        use_latest_pdfs: true,
      }),
    });
    const data = await res.json();
    if (data.ok && data.pdf) {
      const url = `/api/report/download?name=${encodeURIComponent(data.pdf)}`;
      window.open(url, '_blank');
    } else {
      alert('합본 보고서 생성 실패. 각 단계(1공정·2공정·3공정) 분석을 먼저 실행하세요.');
    }
  } catch (e) {
    console.error('합본 보고서 생성 오류:', e);
    alert('합본 보고서 생성 중 오류가 발생했습니다.');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📋 합본 보고서'; }
  }
}

/** U4: PDF 생성 완료 */
function _showReportOk(pdfName) {
  const dl = document.querySelector('#report-state-ok .btn-download');
  const baseQ = pdfName ? `name=${encodeURIComponent(pdfName)}` : '';
  const downloadUrl = `/api/report/download${baseQ ? `?${baseQ}` : ''}`;
  if (dl) dl.setAttribute('href', downloadUrl);
  // iframe 제거됨 — null-safe 처리
  const preview = document.getElementById('pdf-preview-frame');
  if (preview) {
    const previewUrl = `/api/report/download?${baseQ ? `${baseQ}&` : ''}inline=1`;
    preview.setAttribute('src', previewUrl);
  }
  document.getElementById('report-state-loading').style.display = 'none';
  document.getElementById('report-state-ok').style.display      = 'block';
  document.getElementById('report-state-error').style.display   = 'none';
  document.getElementById('report-card').classList.add('visible');
}

/** U4: PDF 생성 실패 */
function _showReportError() {
  const preview = document.getElementById('pdf-preview-frame');
  if (preview) preview.setAttribute('src', 'about:blank');
  document.getElementById('report-state-loading').style.display = 'none';
  document.getElementById('report-state-ok').style.display      = 'none';
  document.getElementById('report-state-error').style.display   = 'block';
  document.getElementById('report-card').classList.add('visible');
}

/* ─ 유틸 함수 ─ */

function _setText(id, value, fallback = '—') {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = String(value || '').trim() || fallback;
}

function _deriveBasisFromRationale(rationale) {
  const text  = String(rationale || '');
  const lines = text.split('\n').map(x => x.trim()).filter(Boolean);
  const out   = { marketMedical: '', regulatory: '', trade: '' };
  for (const line of lines) {
    const low = line.toLowerCase();
    if (!out.marketMedical && (low.includes('시장') || low.includes('의료'))) {
      out.marketMedical = line.replace(/^[\-\d\.\)\s]+/, ''); continue;
    }
    if (!out.regulatory && low.includes('규제')) {
      out.regulatory = line.replace(/^[\-\d\.\)\s]+/, ''); continue;
    }
    if (!out.trade && low.includes('무역')) {
      out.trade = line.replace(/^[\-\d\.\)\s]+/, ''); continue;
    }
  }
  if (!out.marketMedical && lines.length > 0) out.marketMedical = lines[0];
  if (!out.regulatory    && lines.length > 1) out.regulatory    = lines[1];
  if (!out.trade         && lines.length > 2) out.trade         = lines[2];
  return out;
}

function _formatDetailed(text) {
  const src = String(text || '').trim();
  if (!src) return '';
  const lines   = src.split('\n').map(x => x.trim()).filter(Boolean);
  const cleaned = lines.map(l =>
    l.replace(/^[\-\•\*\·]\s+/, '').replace(/^\d+[\.\)]\s+/, '')
  );
  let joined = '';
  for (const part of cleaned) {
    if (!joined) { joined = part; continue; }
    const prev = joined.trimEnd();
    const ends = prev.endsWith('.') || prev.endsWith('!') || prev.endsWith('?')
              || prev.endsWith('다') || prev.endsWith('음') || prev.endsWith('임');
    joined += ends ? ' ' + part : ', ' + part;
  }
  return joined;
}

function _pbsLineFromApi(result) {
  // 칠레 기준: CENABAST 상한가 또는 Mercado Público 낙찰가 우선
  const cenabast = result.cenabast_max_price_clp;
  const mercado  = result.mercado_price_clp;
  const clpNum   = cenabast != null ? Number(cenabast) : (mercado != null ? Number(mercado) : NaN);
  if (!Number.isNaN(clpNum) && clpNum > 0) {
    const label = cenabast != null ? 'CENABAST 소매상한가' : 'Mercado Público 낙찰가';
    return `${label}: CLP ${clpNum.toLocaleString('ko-KR')}`;
  }
  const haiku = String(result.pbs_haiku_estimate || result.price_positioning_pbs || '').trim();
  if (haiku) return haiku;
  return '참고 가격 정보 없음';
}

/** 시장조사 완료/오류 노트 표시 */
function _showP1Note(msg, isErr) {
  const el = document.getElementById('p1-result-note');
  if (!el) return;
  el.textContent = msg;
  el.className   = 'p1-result-note' + (isErr ? ' err' : '');
  el.style.display = '';
}

function _hideP1Note() {
  const el = document.getElementById('p1-result-note');
  if (el) el.style.display = 'none';
}

/** XSS 방지 HTML 이스케이프 */
function _escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}



/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §11. 시장 신호 · 뉴스 (Perplexity)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadNews() {
  const listEl = document.getElementById('news-list');
  const btn    = document.getElementById('btn-news-refresh');
  if (!listEl) return;

  if (btn) btn.disabled = true;
  listEl.innerHTML = '<div class="irow" style="color:var(--muted);font-size:12px;text-align:center;padding:20px 0;">뉴스 로드 중…</div>';

  try {
    const res  = await fetch('/api/cl/news');
    const data = await res.json();

    if (!data.ok || !data.items?.length) {
      listEl.innerHTML = `<div class="irow" style="color:var(--muted);font-size:12px;text-align:center;padding:16px 0;">${data.error || '뉴스를 불러올 수 없습니다.'}</div>`;
      return;
    }

    listEl.innerHTML = data.items.map(item => {
      const href = item.link ? `href="${_escHtml(item.link)}" target="_blank" rel="noopener"` : '';
      const tag  = item.link ? 'a' : 'div';
      return `<${tag} class="irow news-item" ${href} style="${item.link ? 'text-decoration:none;display:block;' : ''}"><span class="tit">${_escHtml(item.title)}</span></${tag}>`;
    }).join('');
  } catch (e) {
    listEl.innerHTML = '<div class="irow" style="color:var(--muted);font-size:12px;text-align:center;padding:16px 0;">뉴스 조회 실패 — 잠시 후 다시 시도해 주세요</div>';
    console.warn('뉴스 로드 실패:', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §11. 3공정 — 바이어 발굴 (P3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

let _p3PollTimer = null;
let _p3Buyers    = [];   // 현재 랭킹된 바이어 전체 (재랭킹용)
let _p3PdfName   = null;

// P1 product_key → 표시 레이블 (P3 연동용, 칠레 CL_ 품목)
const P3_PRODUCT_LABELS = {
  CL_sereterol_activair:     'Sereterol Activair · Fluticasone + Salmeterol',
  CL_hydrine_hydroxyurea_500:'Hydrine · Hydroxyurea 500mg',
  CL_gadvoa_gadobutrol_604:  'Gadvoa Inj. · Gadobutrol 604mg',
  CL_ciloduo_cilosta_rosuva: 'Ciloduo · Cilostazol + Rosuvastatin',
  CL_rosumeg_combigel:       'Rosumeg Combigel · Rosuvastatin + Omega-3',
  CL_atmeg_combigel:         'Atmeg Combigel · Atorvastatin + Omega-3',
  CL_gastiin_cr_mosapride:   'Gastiin CR · Mosapride Citrate 15mg',
  CL_omethyl_omega3_2g:      'Omethyl Cutielet · Omega-3 EE 2g',
};

/** P1 품목 선택 변경 시 P3 연동 레이블 갱신 */
function _syncP3ProductLabel() {
  const p1Select = document.getElementById('product-select');
  const labelEl  = document.getElementById('p3-product-label');
  if (!labelEl) return;
  const key = p1Select?.value || '';
  labelEl.textContent = P3_PRODUCT_LABELS[key] || '시장조사(01)를 먼저 실행해 주세요.';
  labelEl.classList.toggle('p3-product-label--ready', !!P3_PRODUCT_LABELS[key]);
}

const P3_STEP_MAP = {
  crawl:  'crawl',
  enrich: 'enrich',
  rank:   'rank',
  report: 'report',
};

function _setP3Progress(stepId, state) {
  const el = document.getElementById('p3prog-' + stepId);
  if (!el) return;
  el.classList.remove('running', 'done', 'error');
  if (state) el.classList.add(state);
}

function _resetP3Progress() {
  for (const s of ['crawl', 'enrich', 'rank', 'report']) _setP3Progress(s, '');
}

async function runP3Pipeline() {
  const btn   = document.getElementById('btn-p3-run');
  const icon  = document.getElementById('p3-run-icon');
  const errEl = document.getElementById('p3-error-msg');

  // p3-report-select 에서 선택된 보고서의 product_key 우선 사용
  const p3Select      = document.getElementById('p3-report-select');
  const p3ReportId    = p3Select?.value || '';
  const reports       = _loadReports();
  const selectedReport = p3ReportId ? reports.find((r) => String(r.id) === String(p3ReportId)) : null;
  const product = selectedReport?.product_key
    || document.getElementById('product-select')?.value
    || 'CL_cilostazol_cr_200';

  if (btn) btn.disabled = true;
  if (icon) icon.textContent = '…';
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  _resetP3Progress();
  _setP3Progress('crawl', 'running');
  _showP3Skeleton();  // 로딩 중 스켈레톤 표시

  try {
    const checked = [...document.querySelectorAll('.p3-cb:checked')].map(cb => cb.value);
    const res = await fetch('/api/buyers/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        product_key: product,
        active_criteria: checked.length ? checked : null,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (_p3PollTimer) clearInterval(_p3PollTimer);
    _p3PollTimer = setInterval(_pollP3, 2000);
  } catch (e) {
    if (errEl) { errEl.style.display = ''; errEl.textContent = `오류: ${e.message}`; }
    if (btn) btn.disabled = false;
    if (icon) icon.textContent = '▶';
    _resetP3Progress();
  }
}

async function _pollP3() {
  try {
    const res  = await fetch('/api/buyers/status');
    const data = await res.json();

    // 진행 단계 반영
    const stepOrder = ['crawl', 'enrich', 'rank', 'report'];
    const idx = stepOrder.indexOf(data.step);
    if (idx >= 0) {
      for (let i = 0; i < idx; i++)    _setP3Progress(stepOrder[i], 'done');
      _setP3Progress(stepOrder[idx], 'running');
    }

    if (data.status === 'done') {
      clearInterval(_p3PollTimer);
      _p3PollTimer = null;
      for (const s of stepOrder) _setP3Progress(s, 'done');

      const rr = await fetch('/api/buyers/result');
      const result = await rr.json();
      _p3Buyers  = result.buyers || [];
      _p3PdfName = result.pdf || null;
      _renderP3Cards(_p3Buyers);
      document.getElementById('p3-result-section').style.display = '';
      // 상단 고정 최종 보고서 버튼 활성화
      const dlBtnTop = document.getElementById('btn-p3-final-dl');
      if (dlBtnTop && _p3PdfName) dlBtnTop.disabled = false;

      const btn  = document.getElementById('btn-p3-run');
      const icon = document.getElementById('p3-run-icon');
      if (btn)  btn.disabled = false;
      if (icon) icon.textContent = '▶';

    } else if (data.status === 'error') {
      clearInterval(_p3PollTimer);
      _p3PollTimer = null;
      const errEl = document.getElementById('p3-error-msg');
      if (errEl) { errEl.style.display = ''; errEl.textContent = `오류: ${data.step_label || '파이프라인 실패'}`; }
      if (data.step && P3_STEP_MAP[data.step]) _setP3Progress(P3_STEP_MAP[data.step], 'error');
      const btn  = document.getElementById('btn-p3-run');
      const icon = document.getElementById('p3-run-icon');
      if (btn)  btn.disabled = false;
      if (icon) icon.textContent = '▶';
    }
  } catch (_) { /* retry */ }
}

/** 체크박스 변경 → 서버에 재랭킹 요청 */
/** 평가 기준 전체 선택 / 전체 해제 */
function p3CheckAll(checked) {
  document.querySelectorAll('.p3-cb').forEach(cb => { cb.checked = checked; });
  p3ReRank();
}

/** 바이어 발굴 중 스켈레톤 리스트 표시 */
function _showP3Skeleton() {
  const wrap = document.getElementById('p3-cards');
  if (!wrap) return;
  wrap.innerHTML = Array.from({ length: 10 }, (_, i) => `
    <div class="p3-list-row p3-list-skel">
      <span class="p3-list-rank">${i + 1}</span>
      <div class="p3-skel-bar" style="width:${55 + Math.random() * 30 | 0}%"></div>
    </div>`).join('');
  document.getElementById('p3-result-section').style.display = '';
}

async function p3ReRank() {
  if (!_p3Buyers.length) return;
  const checked = [...document.querySelectorAll('.p3-cb:checked')].map(cb => cb.value);
  try {
    const res = await fetch('/api/buyers/rerank', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ criteria: checked.length ? checked : null }),
    });
    const data = await res.json();
    _p3Buyers = data.buyers || _p3Buyers;
    _renderP3Cards(_p3Buyers);
  } catch (_) {
    // 폴백: 클라이언트사이드 정렬
    _renderP3Cards(_p3Buyers);
  }
}

/** Top 10 리스트 렌더링 (팀장 사이트 스타일 — 번호+이름 행) */
function _renderP3Cards(buyers) {
  const wrap = document.getElementById('p3-cards');
  if (!wrap) return;

  if (!buyers.length) {
    wrap.innerHTML = '<div class="p3-empty">발굴된 바이어가 없습니다.</div>';
    return;
  }

  wrap.innerHTML = buyers.map((b, i) => {
    const country  = b.country  || '';
    const category = b.category || '';
    const pri      = b.priority === 1 ? 1 : 2;
    const priLabel = pri === 1 ? '성분 일치' : 'Chile';
    const priClass = pri === 1 ? 'p3-tag-p1' : 'p3-tag-p2';
    const meta     = [country, category].filter(Boolean).join(' · ');
    return `
      <div class="p3-list-row" onclick="showBuyerDetail(${i})">
        <span class="p3-list-rank">${i + 1}</span>
        <span class="p3-list-name">${_escHtml(b.company_name || '-')}</span>
        ${meta ? `<span class="p3-list-meta">${_escHtml(meta)}</span>` : ''}
        <span class="p3-tag ${priClass} p3-list-tag">${priLabel}</span>
      </div>`;
  }).join('');

  // 체크박스 이벤트 (최초 1회만 바인딩)
  document.querySelectorAll('.p3-cb').forEach(cb => {
    cb.onchange = () => p3ReRank();
  });
}

/** 바이어 상세 모달 열기 */
function showBuyerDetail(idx) {
  const b = _p3Buyers[idx];
  if (!b) return;
  const e = b.enriched || {};
  const rankEmoji = ['🥇','🥈','🥉','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟'];
  const priLabel = b.priority === 1 ? '성분 일치' : 'Chile';
  const priClass = b.priority === 1 ? 'p3-tag-p1' : 'p3-tag-p2';

  function row(label, val) {
    if (!val || val === '-' || val === null || val === undefined) return '';
    return `<tr><th>${label}</th><td>${_escHtml(String(val))}</td></tr>`;
  }
  function yn(val) {
    if (val === true)  return '<span class="bm-yes">✓ 있음</span>';
    if (val === false) return '<span class="bm-no">✗ 없음</span>';
    return '-';
  }

  const sources = (e.source_urls || []).map(u =>
    `<a href="${_escHtml(u)}" target="_blank" rel="noopener" class="bm-link">${_escHtml(u)}</a>`
  ).join('');

  const matched = (b.matched_ingredients || []).join(' · ');
  const territories = (e.territories || []).join(', ');

  // 기업개요 · 채택이유 텍스트 준비
  const overview = e.company_overview_kr || e.summary || '';
  const reason   = e.recommendation_reason || '';

  // 인증 배지 목록
  const certs = (e.certifications || []);
  const certHtml = certs.length
    ? certs.map(c => `<span class="bm-cert">${_escHtml(c)}</span>`).join(' ')
    : '';

  document.getElementById('buyer-modal-body').innerHTML = `
    <div class="bm-header">
      <div class="bm-rank">${rankEmoji[idx] || (idx+1)+'위'}</div>
      <div class="bm-title">
        <div class="bm-name">${_escHtml(b.company_name || '-')}</div>
        <div class="bm-meta">
          ${_escHtml(b.country || '-')} · ${_escHtml(b.category || '-')}
          <span class="p3-tag ${priClass}" style="margin-left:6px;">${priLabel}</span>
          <span class="bm-score">${(b.composite_score||0).toFixed(1)}점</span>
        </div>
      </div>
    </div>

    ${overview && overview !== '-' ? `
    <div class="bm-section">기업 개요</div>
    <div class="bm-overview">${_escHtml(overview)}</div>
    ` : ''}

    ${reason && reason !== '-' ? `
    <div class="bm-section">채택 이유</div>
    <div class="bm-reason">${_escHtml(reason)}</div>
    ` : ''}

    <div class="bm-section">연락처</div>
    <table class="bm-table">
      ${row('주소', b.address)}
      ${row('전화', b.phone)}
      ${row('팩스', b.fax)}
      ${row('이메일', b.email)}
      ${row('웹사이트', b.website)}
      ${row('부스', b.booth)}
    </table>

    <div class="bm-section">기업 규모</div>
    <table class="bm-table">
      ${row('연 매출', e.revenue)}
      ${row('임직원 수', e.employees)}
      ${row('설립연도', e.founded)}
      ${territories ? `<tr><th>사업 지역</th><td>${_escHtml(territories)}</td></tr>` : ''}
      ${certHtml ? `<tr><th>인증</th><td>${certHtml}</td></tr>` : ''}
    </table>

    <div class="bm-section">역량 · 실적</div>
    <table class="bm-table">
      <tr><th>GMP 인증</th><td>${yn(e.has_gmp)}</td></tr>
      <tr><th>수입 이력</th><td>${yn(e.import_history)}</td></tr>
      <tr><th>공공조달 이력</th><td>${yn(e.procurement_history)}</td></tr>
    </table>

    <div class="bm-section">채널 · 파트너 적합성</div>
    <table class="bm-table">
      <tr><th>공공 채널</th><td>${yn(e.public_channel)}</td></tr>
      <tr><th>민간 채널</th><td>${yn(e.private_channel)}</td></tr>
      <tr><th>약국 체인</th><td>${yn(e.has_pharmacy_chain)}</td></tr>
      <tr><th>MAH 대행</th><td>${yn(e.mah_capable)}</td></tr>
      ${row('한국 거래 경험', e.korea_experience)}
    </table>

    ${matched ? `<div class="bm-section">성분 매칭</div><div class="bm-match">🧪 ${_escHtml(matched)}</div>` : ''}

    ${sources ? `
    <div class="bm-section">출처</div>
    <div class="bm-sources bm-sources-label">Perplexity 분석</div>
    <div class="bm-sources">${sources}</div>
    ` : ''}
  `;

  const overlay = document.getElementById('buyer-modal-overlay');
  overlay.style.display = 'flex';
  document.body.style.overflow = 'hidden';
}

function closeBuyerModal(e) {
  if (e && e.target !== document.getElementById('buyer-modal-overlay')) return;
  document.getElementById('buyer-modal-overlay').style.display = 'none';
  document.body.style.overflow = '';
}

function downloadBuyerReport() {
  const url = _p3PdfName
    ? `/api/buyers/report/download?name=${encodeURIComponent(_p3PdfName)}`
    : '/api/buyers/report/download';
  window.open(url, '_blank');
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §12. AHP 파트너 매칭 렌더러
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadAhpPartners() {
  const grid   = document.getElementById('ahp-partner-grid');
  const btn    = document.getElementById('btn-ahp-run');
  const msg    = document.getElementById('ahp-status-msg');
  const cntEl  = document.getElementById('ahp-candidate-count');
  if (!grid) return;

  if (btn) { btn.disabled = true; document.getElementById('ahp-run-icon').textContent = '⏳'; }
  if (msg) msg.textContent = 'AHP 점수 산출 중…';
  grid.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:20px;">로드 중…</div>';

  try {
    const res  = await fetch('/api/ahp/partners');
    const data = await res.json();
    if (!data.ok || !data.partners?.length) {
      grid.innerHTML = '<div style="color:var(--muted);padding:20px;">파트너 데이터를 불러올 수 없습니다.</div>';
      return;
    }

    if (cntEl) cntEl.textContent = `${data.count}개사`;
    if (msg) msg.textContent = `${data.count}개사 점수 산출 완료`;

    grid.innerHTML = data.partners.map(p => {
      const stratLabel = p.pitch_strategy === 'line_extension'
        ? '라인 익스텐션 전략' : '직접 파트너십 전략';
      const stratClass = p.pitch_strategy === 'line_extension'
        ? 'ahp-strategy-ext' : 'ahp-strategy-direct';
      const rankClass  = p.rank === 1 ? 'ahp-rank-1' : p.rank === 2 ? 'ahp-rank-2' : p.rank === 3 ? 'ahp-rank-3' : '';
      const products   = (p.key_products || []).map(pr => `<li>${_escHtml(pr)}</li>`).join('');

      return `
      <div class="ahp-card ${rankClass}">
        <div class="ahp-card-header">
          <span class="ahp-rank-badge">RANK ${p.rank}</span>
          <span class="ahp-psi">PSI ${p.psi_score.toFixed(3)}</span>
        </div>
        <h3 class="ahp-company">${_escHtml(p.company_name)}</h3>
        <span class="ahp-strategy ${stratClass}">${stratLabel}</span>
        <div class="ahp-scores">
          <div class="ahp-score-item"><span>심혈관 시너지</span><strong>${(p.cardio_score * 100).toFixed(0)}%</strong></div>
          <div class="ahp-score-item"><span>시장 지배력</span><strong>${(p.market_score * 100).toFixed(0)}%</strong></div>
          <div class="ahp-score-item"><span>글로벌 역량</span><strong>${(p.intl_score * 100).toFixed(0)}%</strong></div>
        </div>
        <ul class="ahp-products">${products}</ul>
        <div class="ahp-pitch">${_escHtml(p.pitch_memo || '')}</div>
        <div class="ahp-contact">
          <span>📧 ${_escHtml(p.email || '-')}</span>
          <span>📞 ${_escHtml(p.phone || '-')}</span>
        </div>
        <div class="ahp-hq" style="font-size:11px;color:var(--muted);margin-top:4px;">🏢 ${_escHtml(p.headquarters || '-')}</div>
      </div>`;
    }).join('');

  } catch (e) {
    grid.innerHTML = '<div style="color:var(--red);padding:20px;">AHP 점수 산출 실패 — 잠시 후 다시 시도하세요.</div>';
  } finally {
    if (btn) { btn.disabled = false; document.getElementById('ahp-run-icon').textContent = '▶'; }
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §14. 초기화
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

loadKeyStatus();        // API 키 배지
loadExchange();         // CLP/KRW 환율 즉시 로드
setInterval(() => { loadExchange(); }, 10000);
loadMacro();            // 칠레 거시 지표 로드
renderReportTab();      // 보고서 탭 초기 렌더
initP2Strategy();       // 수출 가격 전략 초기화

(function () {
  const p1Select = document.getElementById('product-select');
  if (p1Select) p1Select.addEventListener('change', _syncP3ProductLabel);
  _syncP3ProductLabel();
})();
loadNews();             // 칠레 시장 뉴스 즉시 로드
